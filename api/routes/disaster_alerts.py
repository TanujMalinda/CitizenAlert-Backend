"""
Disaster Alerts routes — /api/disaster-alerts
Implements CAP (Common Alerting Protocol) standard for Sri Lanka.
Alert categories: flood, tsunami, cyclone, earthquake, landslide, fire
"""
from datetime import datetime
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from typing import Optional
from core.security import get_current_user, require_authority
from services.notification_service import notify_alert_status_change
from db import database as db

router = APIRouter()

VALID_HAZARD_TYPES = [
    "flood", "tsunami", "cyclone", "earthquake",
    "landslide", "fire", "drought", "storm"
]

VALID_SEVERITIES = ["extreme", "severe", "medium", "low"]


class CreateDisasterAlertRequest(BaseModel):
    title: str
    description: str
    hazard_type: str
    severity: str
    latitude: float
    longitude: float
    district: str
    affected_area: Optional[str] = None
    evacuation_routes: Optional[str] = None
    official_source: Optional[str] = "CitizenAlert Authority"


class UpdateDisasterStatusRequest(BaseModel):
    status: str
    notes: Optional[str] = None


# ── GET /nearby ───────────────────────────────────────────────────────────────
@router.get(
    "/nearby",
    summary="Get nearby disaster alerts",
    description="""
Returns active disaster alerts within radius_km using PostGIS ST_DWithin.
**Try:** latitude=6.9271, longitude=79.8612, radius_km=50
    """,
)
async def get_nearby_disasters(
    latitude:  float = Query(..., ge=5.916, le=9.836),
    longitude: float = Query(..., ge=79.695, le=81.879),
    radius_km: float = Query(50.0, ge=1.0, le=200.0),
    hazard_type: Optional[str] = Query(None),
    user: dict = Depends(get_current_user),
):
    rows = await db.fetch(
        """SELECT
               a.id, a.title, a.description, a.severity,
               a.status, a.district, a.created_at,
               da.hazard_type, da.affected_area,
               da.evacuation_routes, da.official_source,
               da.confirmation_count,
               ROUND((ST_Distance(
                   COALESCE(
                       a.geom,
                       ST_SetSRID(ST_MakePoint(a.longitude, a.latitude), 4326)
                   )::geography,
                   ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography
               ) / 1000.0)::numeric, 2)::float AS distance_km,
               a.latitude, a.longitude
           FROM alerts a
           JOIN disaster_alerts da ON da.alert_id = a.id
           WHERE a.alert_type = 'disaster'
             AND a.status = 'active'
             -- Only broadcast verified disasters. Authority/official alerts are
             -- 'verified' on creation; citizen-reported ones stay hidden until an
             -- authority verifies them via the review queue.
             AND COALESCE(a.tvm_status, 'verified') IN ('verified', 'passed')
             AND ($3::text IS NULL OR da.hazard_type = $3)
             AND ST_DWithin(
                   COALESCE(
                       a.geom,
                       ST_SetSRID(ST_MakePoint(a.longitude, a.latitude), 4326)
                   )::geography,
                   ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography,
                   $4)
           ORDER BY a.severity DESC, distance_km ASC
           LIMIT 50""",
        longitude, latitude, hazard_type, radius_km * 1000,
    )

    data = [dict(r) for r in rows] if rows else []
    return {
        "success": True,
        "count":   len(data),
        "data":    data,
    }


# ── POST / — Create disaster alert (authority only) ───────────────────────────
@router.post(
    "/",
    summary="Create a disaster alert (authority only)",
    description="""
Authority creates a CAP-compliant disaster alert.
Hazard types: flood, tsunami, cyclone, earthquake, landslide, fire, drought, storm
    """,
)
async def create_disaster_alert(
    body: CreateDisasterAlertRequest,
    user: dict = Depends(require_authority),
):
    if body.hazard_type not in VALID_HAZARD_TYPES:
        from fastapi import HTTPException
        raise HTTPException(
            status_code=400,
            detail=f"Invalid hazard_type. Must be one of: {VALID_HAZARD_TYPES}"
        )

    if body.severity not in VALID_SEVERITIES:
        from fastapi import HTTPException
        raise HTTPException(
            status_code=400,
            detail=f"Invalid severity. Must be one of: {VALID_SEVERITIES}"
        )

    user_id = int(user["id"])

    # Insert core alert (UADM)
    row = await db.fetchrow(
        """INSERT INTO alerts
             (title, description, latitude, longitude, status, user_id,
              alert_type, tvm_status, tvm_score, severity, district, geom)
           VALUES ($1, $2, $3, $4, 'active', $5,
                   'disaster', 'verified', 1.0, $6, $7,
                   ST_SetSRID(ST_MakePoint($4, $3), 4326))
           RETURNING id""",
        body.title, body.description,
        body.latitude, body.longitude,
        user_id, body.severity, body.district,
    )
    alert_id = int(row["id"])

    # CAP identifier
    cap_id = f"LK-CA-DS-{int(datetime.now().timestamp())}-{str(alert_id).zfill(6)}"

    # Insert disaster extension
    await db.execute(
        """INSERT INTO disaster_alerts
             (alert_id, hazard_type, affected_area,
              evacuation_routes, official_source)
           VALUES ($1, $2, $3, $4, $5)""",
        alert_id, body.hazard_type, body.affected_area,
        body.evacuation_routes, body.official_source,
    )

    # Reporter counts as the first confirmation (count starts at 1)
    await db.execute(
        """INSERT INTO alert_confirmations (alert_id, user_id)
           VALUES ($1, $2) ON CONFLICT (alert_id, user_id) DO NOTHING""",
        alert_id, user_id,
    )

    # Log to TVM (disasters skip TVM — official source)
    await db.execute(
        """INSERT INTO tvm_log (alert_id, tier, action, actor_id, notes)
           VALUES ($1, 1, 'official_source_bypass', $2,
                   'Disaster alert from authority — TVM bypassed per CAP standard')""",
        alert_id, user_id,
    )

    return {
        "success":        True,
        "alert_id":       alert_id,
        "cap_identifier": cap_id,
        "hazard_type":    body.hazard_type,
        "severity":       body.severity,
        "tvm_status":     "verified",
        "message":        f"{body.hazard_type.title()} alert created successfully",
    }


# ── POST /report — Citizen reports a disaster (pending authority review) ──────
@router.post(
    "/report",
    summary="Report a disaster (citizen) — goes to authority review",
    description="""
A citizen reports a suspected disaster. Unlike the authority `/` endpoint,
this does NOT auto-verify. The report is created as **pending_authority_review**
and routed to the authority dashboard. It only becomes visible to other citizens
once an authority verifies it — preserving the TVM verification model for
high-stakes disaster alerts.
    """,
)
async def report_disaster(
    body: CreateDisasterAlertRequest,
    user: dict = Depends(get_current_user),
):
    from fastapi import HTTPException

    if body.hazard_type not in VALID_HAZARD_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid hazard_type. Must be one of: {VALID_HAZARD_TYPES}",
        )
    if body.severity not in VALID_SEVERITIES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid severity. Must be one of: {VALID_SEVERITIES}",
        )

    user_id = int(user["id"]) if str(user.get("id", "")).isdigit() else None

    # Insert core alert (UADM) — pending, not auto-verified
    row = await db.fetchrow(
        """INSERT INTO alerts
             (title, description, latitude, longitude, status, user_id,
              alert_type, tvm_status, tvm_score, severity, district, geom)
           VALUES ($1, $2, $3, $4, 'active', $5,
                   'disaster', 'pending_authority_review', 0, $6, $7,
                   ST_SetSRID(ST_MakePoint($4, $3), 4326))
           RETURNING id""",
        body.title, body.description,
        body.latitude, body.longitude,
        user_id, body.severity, body.district,
    )
    alert_id = int(row["id"])
    cap_id   = f"LK-CA-DS-{int(datetime.now().timestamp())}-{str(alert_id).zfill(6)}"

    # Insert disaster extension — source marked as citizen report
    await db.execute(
        """INSERT INTO disaster_alerts
             (alert_id, hazard_type, affected_area,
              evacuation_routes, official_source)
           VALUES ($1, $2, $3, $4, $5)""",
        alert_id, body.hazard_type, body.affected_area,
        body.evacuation_routes, "Citizen Report",
    )

    # Reporter counts as the first confirmation (count starts at 1), so they
    # cannot also tap Confirm on their own report.
    if user_id is not None:
        await db.execute(
            """INSERT INTO alert_confirmations (alert_id, user_id)
               VALUES ($1, $2) ON CONFLICT (alert_id, user_id) DO NOTHING""",
            alert_id, user_id,
        )

    # Escalate to authority review queue
    await db.execute(
        """INSERT INTO tvm_log (alert_id, tier, action, actor_id, notes)
           VALUES ($1, 3, 'escalated_to_authority', $2,
                   'Citizen-reported disaster — pending authority verification')""",
        alert_id, user_id,
    )

    return {
        "success":        True,
        "alert_id":       alert_id,
        "cap_identifier": cap_id,
        "hazard_type":    body.hazard_type,
        "severity":       body.severity,
        "tvm_status":     "pending_authority_review",
        "message":        "Disaster report submitted. Pending authority verification "
                          "before it is broadcast to other citizens.",
    }


# ── POST /{alert_id}/confirm — Citizen confirms a disaster ────────────────────
@router.post(
    "/{alert_id}/confirm",
    summary="Confirm a disaster hazard (citizen corroboration)",
    description="""
A citizen confirms they are also witnessing the reported disaster.
Increments the confirmation count shown to everyone. This is a corroboration
signal — it does not change the alert's verification status.
    """,
)
async def confirm_disaster(
    alert_id: int,
    user: dict = Depends(get_current_user),
):
    from fastapi import HTTPException

    row = await db.fetchrow(
        """SELECT a.id, da.confirmation_count
           FROM alerts a
           JOIN disaster_alerts da ON da.alert_id = a.id
           WHERE a.id = $1 AND a.alert_type = 'disaster' AND a.status = 'active'""",
        alert_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Disaster alert not found")

    user_id = int(user["id"]) if str(user.get("id", "")).isdigit() else None
    if user_id is None:
        raise HTTPException(status_code=400, detail="A valid user is required to confirm")

    # Enforce one confirmation per user — UNIQUE(alert_id, user_id)
    claimed = await db.fetchrow(
        """INSERT INTO alert_confirmations (alert_id, user_id)
           VALUES ($1, $2)
           ON CONFLICT (alert_id, user_id) DO NOTHING
           RETURNING id""",
        alert_id, user_id,
    )
    if not claimed:
        current = await db.fetchval(
            "SELECT confirmation_count FROM disaster_alerts WHERE alert_id = $1",
            alert_id,
        )
        return {
            "success":            False,
            "alert_id":           alert_id,
            "confirmation_count": int(current or 0),
            "already_confirmed":  True,
            "message":            "You have already confirmed this disaster.",
        }

    await db.execute(
        "UPDATE disaster_alerts SET confirmation_count = confirmation_count + 1 WHERE alert_id = $1",
        alert_id,
    )
    new_count = (int(row["confirmation_count"]) or 0) + 1

    await db.execute(
        """INSERT INTO tvm_log (alert_id, tier, action, actor_id, notes)
           VALUES ($1, 2, 'citizen_confirmed', $2,
                   'Citizen confirmed disaster — corroboration count now ' || $3)""",
        alert_id, user_id, str(new_count),
    )

    return {
        "success":            True,
        "alert_id":           alert_id,
        "confirmation_count": new_count,
        "message":            "Confirmation recorded — thank you",
    }


# ── GET /{alert_id} — Get single disaster alert ───────────────────────────────
@router.get("/{alert_id}", summary="Get disaster alert details")
async def get_disaster_alert(
    alert_id: int,
    user: dict = Depends(get_current_user),
):
    row = await db.fetchrow(
        """SELECT
               a.id, a.title, a.description, a.severity,
               a.status, a.district, a.created_at,
               a.latitude, a.longitude,
               da.hazard_type, da.affected_area,
               da.evacuation_routes, da.official_source
           FROM alerts a
           JOIN disaster_alerts da ON da.alert_id = a.id
           WHERE a.id = $1 AND a.alert_type = 'disaster'""",
        alert_id,
    )
    if not row:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Disaster alert not found")

    return {"success": True, "data": dict(row)}


# ── PATCH /{alert_id}/status — Update status (authority only) ─────────────────
@router.patch(
    "/{alert_id}/status",
    summary="Update disaster alert status (authority only)",
)
async def update_status(
    alert_id: int,
    body: UpdateDisasterStatusRequest,
    user: dict = Depends(require_authority),
):
    valid_statuses = ["active", "resolved", "cancelled"]
    if body.status not in valid_statuses:
        from fastapi import HTTPException
        raise HTTPException(
            status_code=400,
            detail=f"Status must be one of: {valid_statuses}"
        )

    user_id = int(user["id"])

    await db.execute(
        """UPDATE alerts
           SET status = $1,
               resolved_at = CASE WHEN $1 = 'resolved' THEN NOW() ELSE NULL END,
               resolved_by = $2
           WHERE id = $3 AND alert_type = 'disaster'""",
        body.status, user_id, alert_id,
    )

    await db.execute(
        """INSERT INTO tvm_log (alert_id, tier, action, actor_id, notes)
           VALUES ($1, 3, $2, $3, $4)""",
        alert_id,
        f"status_changed_to_{body.status}",
        user_id,
        body.notes or f"Status updated to {body.status}",
    )

    if body.status == "resolved":
        await notify_alert_status_change(alert_id, "resolved")

    return {
        "success":  True,
        "alert_id": alert_id,
        "status":   body.status,
        "message":  f"Disaster alert {body.status} successfully",
    }