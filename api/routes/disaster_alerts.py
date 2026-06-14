"""
Disaster Alerts routes — /api/disaster-alerts
Implements CAP (Common Alerting Protocol) standard for Sri Lanka.
Alert categories: flood, tsunami, cyclone, earthquake, landslide, fire
"""
from datetime import datetime
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from typing import Optional
from app.core.security import get_current_user, require_authority
from app.db import database as db

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
                   'disaster', 'verified', 100, $6, $7,
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

    return {
        "success":  True,
        "alert_id": alert_id,
        "status":   body.status,
        "message":  f"Disaster alert {body.status} successfully",
    }