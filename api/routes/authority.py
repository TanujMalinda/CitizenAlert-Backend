"""
Authority Dashboard routes — /api/authority
Backing API for the React Authority Dashboard (TVM Tier-3 review console).
All endpoints require the 'authority' role.
"""
import json

from fastapi import APIRouter, Depends, Query, HTTPException
from pydantic import BaseModel
from typing import Optional
from core.security import require_authority, require_super_admin
from services.notification_service import notify_alert_status_change
from services import tvm_service as tvm
from db import database as db

router = APIRouter()

VALID_ALERT_TYPES = [
    "missing_person", "disaster", "crime", "traffic", "health"
]


class ReviewAlertRequest(BaseModel):
    action: str            # "verify" | "reject"
    notes: Optional[str] = None


class ReinstateAlertRequest(BaseModel):
    """Override a rejection — used when the system (or a reviewer) got it wrong."""
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
        "authority_verified" if body.action == "verify" else "authority_rejected",
        user_id,
        body.notes or f"Alert {body.action}ed by authority via unified dashboard",
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


# ── POST /alerts/{id}/reinstate — Override a rejection ───────────────────────
@router.post(
    "/alerts/{alert_id}/reinstate",
    summary="Accept a previously rejected alert (authority only)",
    description="""
Overturns a rejection when the decision was wrong — for example when the
automated scoring or a reviewer dismissed a genuine report.

Available to any authority account, in line with ordinary verify/reject rights.

The alert returns to `verified`, becoming publicly visible again, and the
override is written to the TVM audit log as `authority_reinstated` — naming the
reviewer who overturned it — so the original rejection and its reversal both
remain on record.
""",
)
async def reinstate_alert(
    alert_id: int,
    body: ReinstateAlertRequest,
    user: dict = Depends(require_authority),
):
    row = await db.fetchrow(
        "SELECT id, alert_type, title, tvm_status FROM alerts WHERE id = $1",
        alert_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Alert not found")
    if row["tvm_status"] != "rejected":
        raise HTTPException(
            status_code=400,
            detail=f"Only rejected alerts can be reinstated (this one is '{row['tvm_status']}')",
        )

    user_id = int(user["id"])

    await db.execute(
        "UPDATE alerts SET tvm_status = 'verified', resolved_by = $1 WHERE id = $2",
        user_id, alert_id,
    )

    await db.execute(
        """INSERT INTO tvm_log (alert_id, tier, action, actor_id, notes)
           VALUES ($1, 3, 'authority_reinstated', $2, $3)""",
        alert_id, user_id,
        body.notes or "Rejection overturned by authority — alert accepted",
    )

    # The reporter was told it was rejected; tell them it is live again.
    await notify_alert_status_change(alert_id, "verified")

    return {
        "success":    True,
        "alert_id":   alert_id,
        "alert_type": row["alert_type"],
        "tvm_status": "verified",
        "action":     "reinstate",
        "message":    f"Alert #{alert_id} reinstated and published to citizens",
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

    # Alerts per district — powers the "most affected area" chart
    district_rows = await db.fetch(
        """SELECT district, COUNT(*) AS total
           FROM alerts
           WHERE district IS NOT NULL AND district <> ''
           GROUP BY district
           ORDER BY total DESC
           LIMIT 10"""
    )
    by_district = [dict(r) for r in district_rows] if district_rows else []

    # Daily alert volume for the last 14 days — powers the trend line chart.
    # Gaps (days with no alerts) are back-filled on the client.
    daily_rows = await db.fetch(
        """SELECT to_char(created_at::date, 'YYYY-MM-DD') AS day,
                  COUNT(*) AS total
           FROM alerts
           WHERE created_at >= (CURRENT_DATE - INTERVAL '13 days')
           GROUP BY day
           ORDER BY day"""
    )
    daily = [dict(r) for r in daily_rows] if daily_rows else []

    return {
        "success": True,
        "totals": dict(totals) if totals else {},
        "by_type": by_type,
        "by_district": by_district,
        "daily": daily,
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
               ST_AsGeoJSON(a.affected_geom) AS affected_geojson,
               rev.action     AS review_action,
               rev.notes      AS review_notes,
               rev.tier       AS review_tier,
               rev.created_at AS reviewed_at,
               rev.reviewer_name
           FROM alerts a
           LEFT JOIN users u ON u.id = a.user_id
           LEFT JOIN missing_persons mp ON mp.alert_id = a.id
           -- Latest verification-log entry: explains WHY the alert is
           -- verified/rejected/flagged (authority review or automated TVM).
           LEFT JOIN LATERAL (
               SELECT l.action, l.notes, l.tier, l.created_at,
                      ru.full_name AS reviewer_name
               FROM tvm_log l
               LEFT JOIN users ru ON ru.id = l.actor_id
               WHERE l.alert_id = a.id
               ORDER BY l.created_at DESC
               LIMIT 1
           ) rev ON TRUE
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


# ── GET /reviewed — Accepted / Rejected decision history ─────────────────────
@router.get(
    "/reviewed",
    summary="List accepted or rejected alerts with the decision reason (authority only)",
    description="""
Decision history for the authority dashboard's Accepted / Rejected tabs.

- **accepted** — alerts that passed verification (`verified` or `passed`) and are
  publicly visible
- **rejected** — alerts removed from the public feed, each returned with the
  reason recorded when it was rejected

Every row carries the latest *decision* entry from the TVM audit log
(`decision_action`, `decision_reason`, `decided_by`, `decided_at`). Filtering to
decision entries means a rejection reason is never masked by a later unrelated
log entry such as an affected-area edit.
""",
)
async def reviewed_alerts(
    decision:   str = Query("rejected"),
    alert_type: Optional[str] = Query(None),
    limit:      int = Query(200, ge=1, le=500),
    offset:     int = Query(0, ge=0),
    user: dict = Depends(require_authority),
):
    if decision not in ("accepted", "rejected"):
        raise HTTPException(
            status_code=400, detail="decision must be 'accepted' or 'rejected'")
    if alert_type and alert_type not in VALID_ALERT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid alert_type. Must be one of: {VALID_ALERT_TYPES}")

    rows = await db.fetch(
        """SELECT
               a.id, a.title, a.description, a.alert_type,
               a.severity, a.status, a.tvm_status, a.tvm_score,
               a.district, a.latitude, a.longitude, a.created_at,
               u.full_name AS reporter_name,
               COALESCE(a.photo_url, mp.photo_url) AS photo_url,
               dec.action     AS decision_action,
               dec.notes      AS decision_reason,
               dec.tier       AS decision_tier,
               dec.created_at AS decided_at,
               dec.decided_by
           FROM alerts a
           LEFT JOIN users u ON u.id = a.user_id
           LEFT JOIN missing_persons mp ON mp.alert_id = a.id
           -- Latest *decision* entry only: verification / rejection / reinstatement.
           -- Non-decision entries (e.g. affected_area_updated) are skipped so the
           -- rejection reason survives later edits to the alert.
           LEFT JOIN LATERAL (
               SELECT l.action, l.notes, l.tier, l.created_at,
                      COALESCE(ru.full_name, 'Automated TVM') AS decided_by
               FROM tvm_log l
               LEFT JOIN users ru ON ru.id = l.actor_id
               WHERE l.alert_id = a.id
                 AND (l.action ILIKE '%reject%' OR l.action ILIKE '%verif%'
                   OR l.action ILIKE '%pass%'   OR l.action ILIKE '%fail%'
                   OR l.action ILIKE '%reinstat%')
               ORDER BY l.created_at DESC
               LIMIT 1
           ) dec ON TRUE
           WHERE (($1::text = 'accepted' AND a.tvm_status IN ('verified', 'passed'))
               OR ($1::text = 'rejected' AND a.tvm_status = 'rejected'))
             AND ($2::text IS NULL OR a.alert_type = $2)
           ORDER BY COALESCE(dec.created_at, a.created_at) DESC
           LIMIT $3 OFFSET $4""",
        decision, alert_type, limit, offset,
    )
    data = [dict(r) for r in rows] if rows else []

    # Tab counts, so the UI can label both tabs from a single request.
    counts = await db.fetchrow(
        """SELECT
               COUNT(*) FILTER (WHERE tvm_status IN ('verified','passed')) AS accepted,
               COUNT(*) FILTER (WHERE tvm_status = 'rejected')             AS rejected
           FROM alerts"""
    )

    return {
        "success":  True,
        "decision": decision,
        "count":    len(data),
        "counts":   dict(counts) if counts else {"accepted": 0, "rejected": 0},
        "data":     data,
    }


# ── GET /tvm-overview — How the TVM works, with live figures ─────────────────
#
# Hazard routing is declared here as data so the dashboard explainer and the
# thesis description stay in step with the behaviour implemented in the
# per-domain routers.
TVM_ROUTING = [
    {"hazard": "crime",          "tier": "Tier 3",
     "path": "Held for authority review — hidden from citizens until verified",
     "rationale": "A false accusation can harm a person"},
    {"hazard": "health",         "tier": "Tier 3",
     "path": "Held for authority review — hidden from citizens until verified",
     "rationale": "False outbreak news causes public panic"},
    {"hazard": "traffic",        "tier": "Tier 2",
     "path": "Visible at once; auto-verified after 3 independent citizen confirmations",
     "rationale": "Low harm, high volume — the crowd is reliable here"},
    {"hazard": "disaster",       "tier": "Tier 2",
     "path": "Published immediately; confirmations counted afterwards",
     "rationale": "In a flood, minutes matter"},
    {"hazard": "missing_person", "tier": "Tier 1",
     "path": "Published island-wide; every reported sighting is scored",
     "rationale": "The alert itself helps; false sightings are the real risk"},
]


@router.get(
    "/tvm-overview",
    summary="TVM configuration and live pipeline figures (authority only)",
    description="""
Explains the Tiered Verification Mechanism and shows how it is behaving on real
data — the backing data for the dashboard's "TVM Mechanism" page.

`config` is read directly from `services.tvm_service`, so the scoring weights and
thresholds shown in the dashboard are always the ones actually used to verify
reports; they cannot drift apart.
""",
)
async def tvm_overview(user: dict = Depends(require_authority)):
    # How many alerts sit at each verification outcome
    status_rows = await db.fetch(
        """SELECT tvm_status, COUNT(*) AS total
           FROM alerts GROUP BY tvm_status ORDER BY total DESC"""
    )

    # Pipeline activity per tier — how often each decision has actually fired
    tier_rows = await db.fetch(
        """SELECT tier, action, COUNT(*) AS total
           FROM tvm_log
           GROUP BY tier, action
           ORDER BY tier, total DESC"""
    )

    # Score distribution against the two routing thresholds
    band_row = await db.fetchrow(
        """SELECT
               COUNT(*) FILTER (WHERE tvm_score < $1)                     AS auto_reject_band,
               COUNT(*) FILTER (WHERE tvm_score >= $1 AND tvm_score < $2) AS review_band,
               COUNT(*) FILTER (WHERE tvm_score >= $2)                    AS auto_verify_band,
               COUNT(*) FILTER (WHERE tvm_score IS NULL)                  AS unscored,
               ROUND(AVG(tvm_score)::numeric, 3)                          AS mean_score
           FROM alerts""",
        tvm.TVM_AUTHORITY_REVIEW_THRESHOLD, tvm.TVM_AUTO_VERIFY_THRESHOLD,
    )

    return {
        "success": True,
        "config": {
            "weights":                    tvm.WEIGHTS,
            "auto_verify_threshold":      tvm.TVM_AUTO_VERIFY_THRESHOLD,
            "authority_review_threshold": tvm.TVM_AUTHORITY_REVIEW_THRESHOLD,
            "traffic_consensus_required": 3,
        },
        "tier1_checks": [
            "Location falls inside Sri Lanka",
            "Description is at least 10 characters",
            "Not a duplicate from the same reporter within 30 minutes",
            "Reported time is not in the future",
        ],
        "routing":     TVM_ROUTING,
        "by_status":   [dict(r) for r in status_rows] if status_rows else [],
        "tier_activity": [dict(r) for r in tier_rows] if tier_rows else [],
        "score_bands": dict(band_row) if band_row else {},
    }


# ── GET /alerts/{id}/tvm-explain — Per-alert scoring walkthrough ─────────────

# Plain-English meaning of each scoring factor, and of the neutral default it
# falls back to when there is nothing to compare a report against.
FACTOR_INFO = {
    "reporter_trust": {
        "label": "Reporter trust",
        "means": "How reliable this reporter has been on previous reports.",
        "default_note": "0.50 is the neutral starting value for an account with no history.",
    },
    "location_plausibility": {
        "label": "Location plausibility",
        "means": "Whether the reported place makes sense against what is already known.",
        "default_note": "0.70 is used when there is no earlier location to compare against.",
    },
    "time_plausibility": {
        "label": "Time plausibility",
        "means": "Whether the reported time fits the known timeline of the incident.",
        "default_note": "0.70 is used when there is no reference time to compare against.",
    },
    "report_corroboration": {
        "label": "Corroboration by others",
        "means": "How many independent nearby reports agree with this one.",
        "default_note": "0.50 means no other reports had come in yet.",
    },
}


def _parse_components(notes: str) -> dict | None:
    """Pull the stored per-factor values out of a score_calculated log note."""
    if not notes or "components=" not in notes:
        return None
    raw = notes.split("components=", 1)[1].strip()
    try:
        return json.loads(raw)
    except Exception:
        return None


def _parse_score(notes: str) -> float | None:
    if not notes or "score=" not in notes:
        return None
    try:
        return float(notes.split("score=", 1)[1].split()[0])
    except Exception:
        return None


@router.get(
    "/alerts/{alert_id}/tvm-explain",
    summary="Step-by-step explanation of how one alert was verified (authority only)",
    description="""
Rebuilds the verification story of a single alert from its audit log: which
Tier-1 checks it passed, how many points each scoring factor contributed at
Tier 2, which threshold band the total fell into, and what any human reviewer
then decided.

Where the per-factor values were not recorded (alerts scored before the
breakdown was stored), `breakdown_available` is false and only the total score
is reported — the individual values are not reconstructed, because the inputs
they depend on change over time.
""",
)
async def tvm_explain(alert_id: int, user: dict = Depends(require_authority)):
    alert = await db.fetchrow(
        """SELECT a.id, a.title, a.description, a.alert_type, a.severity,
                  a.status, a.tvm_status, a.tvm_score, a.district, a.created_at,
                  u.full_name AS reporter_name
           FROM alerts a
           LEFT JOIN users u ON u.id = a.user_id
           WHERE a.id = $1""",
        alert_id,
    )
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")

    logs = await db.fetch(
        """SELECT l.tier, l.action, l.notes, l.created_at,
                  COALESCE(u.full_name, 'Automated TVM') AS actor
           FROM tvm_log l
           LEFT JOIN users u ON u.id = l.actor_id
           WHERE l.alert_id = $1
           ORDER BY l.created_at ASC, l.id ASC""",
        alert_id,
    )
    logs = [dict(r) for r in logs] if logs else []

    stages: list[dict] = []

    # ── Tier 1 ───────────────────────────────────────────────────────────────
    t1 = next((l for l in logs if l["action"] in ("filter_pass", "filter_fail")), None)
    if t1:
        passed = t1["action"] == "filter_pass"
        stages.append({
            "tier": 1,
            "name": "Automated filter",
            "outcome": "passed" if passed else "failed",
            "headline": "Passed every basic check"
                        if passed else "Blocked before scoring",
            "detail": t1["notes"],
            "checks": [
                "Location falls inside Sri Lanka",
                "Description is at least 10 characters",
                "Not a duplicate from the same reporter within 30 minutes",
                "Reported time is not in the future",
            ],
            "at": t1["created_at"],
        })

    # ── Tier 2 ───────────────────────────────────────────────────────────────
    t2 = next((l for l in logs if l["action"] == "score_calculated"), None)
    if t2:
        comps = _parse_components(t2["notes"])
        score = _parse_score(t2["notes"])
        if score is None:
            score = float(alert["tvm_score"]) if alert["tvm_score"] is not None else None

        factors = []
        if comps:
            for key, weight in tvm.WEIGHTS.items():
                value = comps.get(key)
                if value is None:
                    continue
                info = FACTOR_INFO.get(key, {})
                points = round(float(value) * float(weight), 3)
                factors.append({
                    "factor":   key,
                    "label":    info.get("label", key.replace("_", " ").title()),
                    "means":    info.get("means", ""),
                    "value":    round(float(value), 3),
                    "weight":   weight,
                    "points":   points,
                    "max_points": weight,
                    "note":     info.get("default_note", "")
                                if abs(float(value) - 0.50) < 1e-9
                                or abs(float(value) - 0.70) < 1e-9 else "",
                })

        # Which routing band the total landed in
        if score is None:
            band = None
        elif score >= tvm.TVM_AUTO_VERIFY_THRESHOLD:
            band = {"key": "auto_verify",
                    "label": "High confidence — auto-verified",
                    "range": f"≥ {tvm.TVM_AUTO_VERIFY_THRESHOLD}",
                    "meaning": "Strong enough to publish without a human check."}
        elif score >= tvm.TVM_AUTHORITY_REVIEW_THRESHOLD:
            band = {"key": "review",
                    "label": "Borderline — sent to a human reviewer",
                    "range": f"{tvm.TVM_AUTHORITY_REVIEW_THRESHOLD} – {tvm.TVM_AUTO_VERIFY_THRESHOLD}",
                    "meaning": "Not confident enough to publish automatically, "
                               "but not weak enough to discard."}
        else:
            band = {"key": "auto_reject",
                    "label": "Low confidence — auto-rejected",
                    "range": f"< {tvm.TVM_AUTHORITY_REVIEW_THRESHOLD}",
                    "meaning": "Too weak to act on without further evidence."}

        stages.append({
            "tier": 2,
            "name": "Confidence scoring",
            "score": score,
            "breakdown_available": bool(factors),
            "factors": factors,
            "total_points": round(sum(f["points"] for f in factors), 3) if factors else score,
            "band": band,
            "at": t2["created_at"],
            "detail": None if factors else
                      "Per-factor values were not recorded for this alert, so only "
                      "the total score is shown.",
        })

    # ── Tier 3 ───────────────────────────────────────────────────────────────
    t3_actions = ("escalated_to_authority", "authority_verified", "authority_rejected",
                  "authority_reinstated", "authority_verifyd", "authority_rejectd",
                  "consensus_auto_verified", "citizen_confirmed", "auto_verified",
                  "official_source_bypass", "authority_resolved", "reporter_resolved")
    events = [
        {
            "action": l["action"],
            "label":  l["action"].replace("_", " "),
            "notes":  l["notes"],
            "actor":  l["actor"],
            "tier":   l["tier"],
            "at":     l["created_at"],
        }
        for l in logs if l["action"] in t3_actions
    ]
    if events:
        stages.append({
            "tier": 3,
            "name": "Human and community review",
            "events": events,
        })

    # ── Where it ended up ────────────────────────────────────────────────────
    status = alert["tvm_status"]
    public = status in ("verified", "passed")
    if public:
        plain = "Published — citizens in the affected area can see this alert."
    elif status == "rejected":
        plain = "Rejected — hidden from citizens. An authority can still accept it."
    elif status == "pending_consensus":
        plain = "Visible, but still gathering citizen confirmations before it counts as verified."
    elif status and status.startswith("pending"):
        plain = "Waiting for an authority to make a decision — not yet visible to citizens."
    else:
        plain = "No verification decision recorded."

    return {
        "success": True,
        "alert":   dict(alert),
        "stages":  stages,
        "outcome": {"tvm_status": status, "public": public, "plain": plain},
        "config":  {
            "weights": tvm.WEIGHTS,
            "auto_verify_threshold": tvm.TVM_AUTO_VERIFY_THRESHOLD,
            "authority_review_threshold": tvm.TVM_AUTHORITY_REVIEW_THRESHOLD,
        },
    }


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
