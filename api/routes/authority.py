"""
Authority Dashboard routes — /api/authority
Backing API for the React Authority Dashboard (TVM Tier-3 review console).
All endpoints require the 'authority' role.
"""
from fastapi import APIRouter, Depends, Query, HTTPException
from pydantic import BaseModel
from typing import Optional
from core.security import require_authority, require_super_admin
from services.notification_service import notify_alert_status_change
from db import database as db

router = APIRouter()

VALID_ALERT_TYPES = [
    "missing_person", "disaster", "crime", "traffic", "health"
]


class ReviewAlertRequest(BaseModel):
    action: str            # "verify" | "reject"
    notes: Optional[str] = None


class ReviewRegistrationRequest(BaseModel):
    notes: Optional[str] = None


class SetAffectedAreaRequest(BaseModel):
    """CAP-style affected area editing.

    mode:
      - "polygon"     — coordinates trace the area outline (>= 3 points)
      - "line_buffer" — coordinates trace a line (e.g. a river course, >= 2
                        points); the stored polygon is that line buffered by
                        buffer_m metres on each side
      - "clear"       — remove the drawn polygon (falls back to radius circle)
    coordinates: [[lat, lng], ...]
    """
    mode: str
    coordinates: Optional[list[list[float]]] = None
    buffer_m: Optional[float] = 300.0
    radius_km: Optional[float] = None  # optionally update the circle radius too


# ── Authority Registration Management (super_admin only) ─────────────────────

@router.get(
    "/registrations",
    summary="List pending authority registration requests (super-admin only)",
)
async def list_registrations(
    status: Optional[str] = Query("pending_approval"),
    user: dict = Depends(require_super_admin),
):
    rows = await db.fetch(
        """SELECT id, full_name, email, district, designation, department,
                  employee_id, account_status, created_at
           FROM users
           WHERE role = 'authority'
             AND ($1::text IS NULL OR account_status = $1)
           ORDER BY created_at DESC""",
        status,
    )
    data = [dict(r) for r in rows] if rows else []
    return {"success": True, "count": len(data), "data": data}


@router.post(
    "/registrations/{user_id}/approve",
    summary="Approve an authority registration (super-admin only)",
)
async def approve_registration(
    user_id: int,
    body: ReviewRegistrationRequest,
    admin: dict = Depends(require_super_admin),
):
    row = await db.fetchrow(
        "SELECT id, full_name, email, account_status FROM users WHERE id = $1 AND role = 'authority'",
        user_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Authority registration not found")
    if row["account_status"] != "pending_approval":
        raise HTTPException(status_code=400, detail=f"Account is already '{row['account_status']}'")

    await db.execute(
        "UPDATE users SET account_status = 'active' WHERE id = $1",
        user_id,
    )
    return {
        "success":  True,
        "user_id":  user_id,
        "email":    row["email"],
        "status":   "active",
        "message":  f"Authority account for {row['full_name']} approved. They can now log in.",
        "notes":    body.notes,
    }


@router.post(
    "/registrations/{user_id}/reject",
    summary="Reject an authority registration (super-admin only)",
)
async def reject_registration(
    user_id: int,
    body: ReviewRegistrationRequest,
    admin: dict = Depends(require_super_admin),
):
    row = await db.fetchrow(
        "SELECT id, full_name, email, account_status FROM users WHERE id = $1 AND role = 'authority'",
        user_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Authority registration not found")
    if row["account_status"] == "rejected":
        raise HTTPException(status_code=400, detail="Account is already rejected")

    await db.execute(
        "UPDATE users SET account_status = 'rejected' WHERE id = $1",
        user_id,
    )
    return {
        "success":  True,
        "user_id":  user_id,
        "email":    row["email"],
        "status":   "rejected",
        "message":  f"Authority registration for {row['full_name']} rejected.",
        "notes":    body.notes,
    }


# ── POST /alerts/{id}/review — Unified Tier 3 review action ──────────────────
@router.post(
    "/alerts/{alert_id}/review",
    summary="Unified authority review — verify or reject any alert (authority only)",
    description="""
Tier 3 review action for the authority dashboard.
Works across all five alert categories from a single endpoint.
- **verify** → alert becomes publicly visible to citizens
- **reject** → alert removed from public feed
    """,
)
async def review_alert(
    alert_id: int,
    body: ReviewAlertRequest,
    user: dict = Depends(require_authority),
):
    if body.action not in ["verify", "reject"]:
        raise HTTPException(status_code=400, detail="Action must be 'verify' or 'reject'")

    row = await db.fetchrow(
        "SELECT id, alert_type, tvm_status FROM alerts WHERE id = $1", alert_id
    )
    if not row:
        raise HTTPException(status_code=404, detail="Alert not found")

    user_id    = int(user["id"])
    tvm_status = "verified" if body.action == "verify" else "rejected"

    await db.execute(
        "UPDATE alerts SET tvm_status = $1, resolved_by = $2 WHERE id = $3",
        tvm_status, user_id, alert_id,
    )

    await db.execute(
        """INSERT INTO tvm_log (alert_id, tier, action, actor_id, notes)
           VALUES ($1, 3, $2, $3, $4)""",
        alert_id,
        f"authority_{body.action}d",
        user_id,
        body.notes or f"Alert {body.action}d by authority via unified dashboard",
    )

    # Notify the citizen who reported this alert
    await notify_alert_status_change(
        alert_id, "verified" if body.action == "verify" else "rejected"
    )

    return {
        "success":    True,
        "alert_id":   alert_id,
        "alert_type": row["alert_type"],
        "tvm_status": tvm_status,
        "action":     body.action,
        "message":    f"{row['alert_type'].title()} alert {body.action}d successfully",
    }


# ── GET /stats — Dashboard summary cards ──────────────────────────────────────
@router.get("/stats", summary="Dashboard summary statistics (authority only)")
async def get_stats(user: dict = Depends(require_authority)):
    rows = await db.fetch(
        """SELECT
               alert_type,
               COUNT(*)                                                   AS total,
               COUNT(*) FILTER (WHERE status = 'active')                  AS active,
               COUNT(*) FILTER (WHERE tvm_status = 'pending_authority_review') AS pending_review,
               COUNT(*) FILTER (WHERE tvm_status IN ('verified','passed')) AS verified,
               COUNT(*) FILTER (WHERE tvm_status = 'rejected')            AS rejected
           FROM alerts
           GROUP BY alert_type"""
    )
    by_type = {r["alert_type"]: dict(r) for r in rows} if rows else {}

    totals = await db.fetchrow(
        """SELECT
               COUNT(*)                                                   AS total,
               COUNT(*) FILTER (WHERE status = 'active')                  AS active,
               COUNT(*) FILTER (WHERE tvm_status = 'pending_authority_review') AS pending_review,
               COUNT(*) FILTER (WHERE tvm_status IN ('verified','passed')) AS verified,
               COUNT(*) FILTER (WHERE tvm_status = 'rejected')            AS rejected
           FROM alerts"""
    )
    users_row = await db.fetchrow("SELECT COUNT(*) AS users FROM users")

    return {
        "success": True,
        "totals": dict(totals) if totals else {},
        "by_type": by_type,
        "registered_users": users_row["users"] if users_row else 0,
    }


# ── GET /alerts — Island-wide alert list with filters ─────────────────────────
@router.get("/alerts", summary="List all alerts with filters (authority only)")
async def list_alerts(
    alert_type: Optional[str] = Query(None),
    status:     Optional[str] = Query(None),
    tvm_status: Optional[str] = Query(None),
    district:   Optional[str] = Query(None),
    limit:      int = Query(100, ge=1, le=500),
    offset:     int = Query(0, ge=0),
    user: dict = Depends(require_authority),
):
    if alert_type and alert_type not in VALID_ALERT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid alert_type. Must be one of: {VALID_ALERT_TYPES}"
        )

    rows = await db.fetch(
        """SELECT
               a.id, a.title, a.description, a.alert_type,
               a.severity, a.status, a.tvm_status, a.tvm_score,
               a.district, a.latitude, a.longitude,
               a.created_at, a.resolved_at,
               u.full_name AS reporter_name, u.email AS reporter_email,
               COALESCE(a.photo_url, mp.photo_url) AS photo_url,
               a.affected_radius_km,
               ST_AsGeoJSON(a.affected_geom) AS affected_geojson
           FROM alerts a
           LEFT JOIN users u ON u.id = a.user_id
           LEFT JOIN missing_persons mp ON mp.alert_id = a.id
           WHERE ($1::text IS NULL OR a.alert_type = $1)
             AND ($2::text IS NULL OR a.status = $2)
             AND ($3::text IS NULL OR a.tvm_status = $3)
             AND ($4::text IS NULL OR a.district = $4)
           ORDER BY a.created_at DESC
           LIMIT $5 OFFSET $6""",
        alert_type, status, tvm_status, district, limit, offset,
    )
    data = [dict(r) for r in rows] if rows else []
    return {"success": True, "count": len(data), "data": data}


# ── POST /alerts/{id}/area — set/clear the CAP-style affected area ───────────
@router.post(
    "/alerts/{alert_id}/area",
    summary="Set the alert's affected area (authority only)",
    description="""
Stores a CAP-style affected-area polygon for an alert.

- **polygon** — trace the outline of the affected zone
- **line_buffer** — trace a line (e.g. a flooding river's course) and buffer it
  by `buffer_m` metres, producing a corridor polygon that follows the curve
- **clear** — remove the polygon (the severity-based radius circle applies again)
""",
)
async def set_affected_area(
    alert_id: int,
    body: SetAffectedAreaRequest,
    user: dict = Depends(require_authority),
):
    row = await db.fetchrow("SELECT id FROM alerts WHERE id = $1", alert_id)
    if not row:
        raise HTTPException(status_code=404, detail="Alert not found")

    if body.mode == "clear":
        await db.execute(
            "UPDATE alerts SET affected_geom = NULL WHERE id = $1", alert_id)
        geojson = None

    elif body.mode in ("polygon", "line_buffer"):
        coords = body.coordinates or []
        min_pts = 3 if body.mode == "polygon" else 2
        if len(coords) < min_pts:
            raise HTTPException(
                status_code=400,
                detail=f"'{body.mode}' needs at least {min_pts} points")
        # WKT wants "lng lat"; coordinates arrive as [lat, lng]
        pts = ", ".join(f"{float(lng)} {float(lat)}" for lat, lng in coords)
        try:
            if body.mode == "polygon":
                first = f"{float(coords[0][1])} {float(coords[0][0])}"
                wkt = f"POLYGON(({pts}, {first}))"  # close the ring
                await db.execute(
                    """UPDATE alerts
                       SET affected_geom = ST_MakeValid(ST_GeomFromText($1, 4326))
                       WHERE id = $2""",
                    wkt, alert_id)
            else:
                buffer_m = max(10.0, float(body.buffer_m or 300.0))
                wkt = f"LINESTRING({pts})"
                await db.execute(
                    """UPDATE alerts
                       SET affected_geom =
                           ST_Buffer(ST_GeomFromText($1, 4326)::geography, $2)::geometry
                       WHERE id = $3""",
                    wkt, buffer_m, alert_id)
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(
                status_code=400,
                detail="Could not build a valid area from those points")
        geojson = await db.fetchval(
            "SELECT ST_AsGeoJSON(affected_geom) FROM alerts WHERE id = $1",
            alert_id)
    else:
        raise HTTPException(
            status_code=400,
            detail="mode must be 'polygon', 'line_buffer' or 'clear'")

    if body.radius_km is not None:
        await db.execute(
            "UPDATE alerts SET affected_radius_km = $1 WHERE id = $2",
            max(0.05, float(body.radius_km)), alert_id)

    await db.execute(
        """INSERT INTO tvm_log (alert_id, tier, action, actor_id, notes)
           VALUES ($1, 3, 'affected_area_updated', $2, $3)""",
        alert_id, int(user["id"]),
        f"Affected area {body.mode} set by authority",
    )

    return {"success": True, "alert_id": alert_id,
            "mode": body.mode, "affected_geojson": geojson}


# ── GET /pending — TVM Tier-3 review queue ────────────────────────────────────
@router.get("/pending", summary="Alerts awaiting authority review (authority only)")
async def pending_queue(user: dict = Depends(require_authority)):
    rows = await db.fetch(
        """SELECT
               a.id, a.title, a.description, a.alert_type,
               a.severity, a.tvm_status, a.tvm_score,
               a.district, a.latitude, a.longitude, a.created_at,
               u.full_name AS reporter_name,
               COALESCE(a.photo_url, mp.photo_url) AS photo_url
           FROM alerts a
           LEFT JOIN users u ON u.id = a.user_id
           LEFT JOIN missing_persons mp ON mp.alert_id = a.id
           WHERE a.tvm_status IN ('pending', 'pending_authority_review')
             AND a.status = 'active'
           ORDER BY
               CASE a.severity
                   WHEN 'extreme' THEN 1 WHEN 'severe' THEN 2
                   WHEN 'medium'  THEN 3 ELSE 4
               END,
               a.created_at ASC"""
    )
    data = [dict(r) for r in rows] if rows else []
    return {"success": True, "count": len(data), "data": data}


# ── GET /tvm-log — Verification audit trail ───────────────────────────────────
@router.get("/tvm-log", summary="TVM audit log (authority only)")
async def tvm_log(
    alert_id: Optional[int] = Query(None),
    limit:    int = Query(100, ge=1, le=500),
    user: dict = Depends(require_authority),
):
    rows = await db.fetch(
        """SELECT
               l.id, l.alert_id, l.tier, l.action, l.notes, l.created_at,
               a.title AS alert_title, a.alert_type,
               u.full_name AS actor_name
           FROM tvm_log l
           LEFT JOIN alerts a ON a.id = l.alert_id
           LEFT JOIN users u ON u.id = l.actor_id
           WHERE ($1::int IS NULL OR l.alert_id = $1)
           ORDER BY l.created_at DESC
           LIMIT $2""",
        alert_id, limit,
    )
    data = [dict(r) for r in rows] if rows else []
    return {"success": True, "count": len(data), "data": data}
