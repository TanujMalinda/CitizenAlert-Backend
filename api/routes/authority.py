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
               u.full_name AS reporter_name, u.email AS reporter_email
           FROM alerts a
           LEFT JOIN users u ON u.id = a.user_id
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


# ── GET /pending — TVM Tier-3 review queue ────────────────────────────────────
@router.get("/pending", summary="Alerts awaiting authority review (authority only)")
async def pending_queue(user: dict = Depends(require_authority)):
    rows = await db.fetch(
        """SELECT
               a.id, a.title, a.description, a.alert_type,
               a.severity, a.tvm_status, a.tvm_score,
               a.district, a.latitude, a.longitude, a.created_at,
               u.full_name AS reporter_name
           FROM alerts a
           LEFT JOIN users u ON u.id = a.user_id
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
