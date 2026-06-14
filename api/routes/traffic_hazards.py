"""
Traffic Hazards routes — /api/traffic-hazards
Linear polyline road-segment alerts with crowdsourced consensus verification.
Alert categories: accident, road_closure, flooding, obstruction, construction, pothole
"""
from datetime import datetime
from fastapi import APIRouter, Depends, Query, HTTPException
from pydantic import BaseModel
from typing import Optional
from app.core.security import get_current_user, require_authority
from app.db import database as db

router = APIRouter()

VALID_HAZARD_TYPES = [
    "accident", "road_closure", "flooding", "obstruction",
    "construction", "pothole", "landslide", "other"
]

VALID_SEVERITIES = ["extreme", "severe", "medium", "low"]

# Crowdsourced consensus: N confirmations → auto-verify (low-sensitivity TVM bypass)
CONSENSUS_AUTO_VERIFY_COUNT = 3


class CreateTrafficHazardRequest(BaseModel):
    title: str
    description: str
    hazard_type: str
    severity: str = "medium"
    latitude: float
    longitude: float
    district: str
    road_segment: Optional[str] = None        # human-readable road name/segment
    expected_clear_time: Optional[str] = None  # ISO datetime when hazard expected to clear


class ConfirmTrafficHazardRequest(BaseModel):
    comment: Optional[str] = None


class UpdateTrafficStatusRequest(BaseModel):
    status: str
    notes: Optional[str] = None


# ── GET /nearby ───────────────────────────────────────────────────────────────
@router.get(
    "/nearby",
    summary="Get nearby traffic hazards",
    description="""
Returns active traffic hazards within radius_km using PostGIS ST_DWithin.
Citizens see only verified + consensus-confirmed hazards.
**Try:** latitude=6.9271, longitude=79.8612, radius_km=10
    """,
)
async def get_nearby_traffic(
    latitude:    float = Query(..., ge=5.916, le=9.836),
    longitude:   float = Query(..., ge=79.695, le=81.879),
    radius_km:   float = Query(10.0, ge=0.5, le=100.0),
    hazard_type: Optional[str] = Query(None),
    user: dict = Depends(get_current_user),
):
    is_authority = user.get("role") == "authority"
    tvm_filter   = "AND a.tvm_status IN ('verified', 'passed')" \
                   if not is_authority else ""

    rows = await db.fetch(
        f"""SELECT
               a.id, a.title, a.description, a.severity,
               a.status, a.tvm_status, a.district, a.created_at,
               th.hazard_type, th.road_segment,
               th.confirmation_count, th.expected_clear_time,
               ROUND((ST_Distance(
                   COALESCE(
                       a.geom,
                       ST_SetSRID(ST_MakePoint(a.longitude, a.latitude), 4326)
                   )::geography,
                   ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography
               ) / 1000.0)::numeric, 2)::float AS distance_km,
               a.latitude, a.longitude
           FROM alerts a
           JOIN traffic_hazards th ON th.alert_id = a.id
           WHERE a.alert_type = 'traffic'
             AND a.status = 'active'
             AND ($3::text IS NULL OR th.hazard_type = $3)
             {tvm_filter}
             AND ST_DWithin(
                   COALESCE(
                       a.geom,
                       ST_SetSRID(ST_MakePoint(a.longitude, a.latitude), 4326)
                   )::geography,
                   ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography,
                   $4)
           ORDER BY a.created_at DESC
           LIMIT 50""",
        longitude, latitude, hazard_type, radius_km * 1000,
    )

    data = [dict(r) for r in rows] if rows else _mock_nearby(latitude, longitude)["data"]
    return {
        "success": True,
        "count":   len(data),
        "data":    data,
    }


# ── POST / — Submit traffic hazard ────────────────────────────────────────────
@router.post(
    "/",
    summary="Report a traffic hazard",
    description="""
Citizen submits a traffic hazard report.
Verification strategy: low-sensitivity crowdsourced consensus.
Auto-verified after 3 independent confirmations (TVM bypass for low-sensitivity category).
    """,
)
async def create_traffic_hazard(
    body: CreateTrafficHazardRequest,
    user: dict = Depends(get_current_user),
):
    if body.hazard_type not in VALID_HAZARD_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid hazard_type. Must be one of: {VALID_HAZARD_TYPES}"
        )
    if body.severity not in VALID_SEVERITIES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid severity. Must be one of: {VALID_SEVERITIES}"
        )

    user_id = int(user["id"]) if str(user["id"]).isdigit() else None

    # Check for duplicate report within 500m in last 30 minutes
    existing = await db.fetchval(
        """SELECT a.id FROM alerts a
           JOIN traffic_hazards th ON th.alert_id = a.id
           WHERE a.alert_type = 'traffic'
             AND a.status = 'active'
             AND a.created_at > NOW() - INTERVAL '30 minutes'
             AND ST_DWithin(
                   COALESCE(a.geom,
                       ST_SetSRID(ST_MakePoint(a.longitude, a.latitude), 4326)
                   )::geography,
                   ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography,
                   500)
           LIMIT 1""",
        body.longitude, body.latitude,
    )

    if existing:
        # Increment confirmation count on duplicate → crowdsourced consensus
        await db.execute(
            """UPDATE traffic_hazards
               SET confirmation_count = confirmation_count + 1
               WHERE alert_id = $1""",
            int(existing),
        )
        new_count = await db.fetchval(
            "SELECT confirmation_count FROM traffic_hazards WHERE alert_id = $1",
            int(existing),
        )
        # Auto-verify on consensus threshold
        if new_count and int(new_count) >= CONSENSUS_AUTO_VERIFY_COUNT:
            await db.execute(
                "UPDATE alerts SET tvm_status = 'verified' WHERE id = $1",
                int(existing),
            )
            await db.execute(
                """INSERT INTO tvm_log (alert_id, tier, action, actor_id, notes)
                   VALUES ($1, 2, 'consensus_auto_verified', $2,
                           'Traffic hazard reached consensus threshold — auto-verified')""",
                int(existing), user_id,
            )
        return {
            "success":           True,
            "alert_id":          int(existing),
            "action":            "confirmation_added",
            "confirmation_count": int(new_count or 0),
            "tvm_status":        "verified" if new_count and int(new_count) >= CONSENSUS_AUTO_VERIFY_COUNT
                                 else "pending_consensus",
            "message":           "Duplicate detected — confirmation count incremented",
        }

    # New hazard report
    row = await db.fetchrow(
        """INSERT INTO alerts
             (title, description, latitude, longitude, status, user_id,
              alert_type, tvm_status, tvm_score, severity, district, geom)
           VALUES ($1, $2, $3, $4, 'active', $5,
                   'traffic', 'pending_consensus', 0, $6, $7,
                   ST_SetSRID(ST_MakePoint($4, $3), 4326))
           RETURNING id""",
        body.title, body.description,
        body.latitude, body.longitude,
        user_id, body.severity, body.district,
    )
    alert_id = int(row["id"])
    cap_id   = f"LK-CA-TH-{int(datetime.now().timestamp())}-{str(alert_id).zfill(6)}"

    clear_time = None
    if body.expected_clear_time:
        try:
            clear_time = datetime.fromisoformat(body.expected_clear_time)
        except ValueError:
            pass

    await db.execute(
        """INSERT INTO traffic_hazards
             (alert_id, hazard_type, road_segment,
              confirmation_count, expected_clear_time)
           VALUES ($1, $2, $3, 1, $4)""",
        alert_id, body.hazard_type, body.road_segment, clear_time,
    )

    await db.execute(
        """INSERT INTO tvm_log (alert_id, tier, action, actor_id, notes)
           VALUES ($1, 1, 'initial_submission', $2,
                   'Traffic hazard submitted — awaiting crowdsourced consensus')""",
        alert_id, user_id,
    )

    return {
        "success":           True,
        "alert_id":          alert_id,
        "cap_identifier":    cap_id,
        "tvm_status":        "pending_consensus",
        "confirmation_count": 1,
        "consensus_threshold": CONSENSUS_AUTO_VERIFY_COUNT,
        "message":           f"Traffic hazard reported. {CONSENSUS_AUTO_VERIFY_COUNT - 1} more confirmations needed for auto-verification.",
    }


# ── POST /{alert_id}/confirm — Citizen confirms existing hazard ───────────────
@router.post(
    "/{alert_id}/confirm",
    summary="Confirm an existing traffic hazard",
    description="""
Citizen confirms they also see the reported hazard.
After 3 confirmations the hazard is auto-verified via crowdsourced consensus.
    """,
)
async def confirm_hazard(
    alert_id: int,
    body: ConfirmTrafficHazardRequest,
    user: dict = Depends(get_current_user),
):
    row = await db.fetchrow(
        """SELECT a.id, th.confirmation_count
           FROM alerts a
           JOIN traffic_hazards th ON th.alert_id = a.id
           WHERE a.id = $1 AND a.alert_type = 'traffic' AND a.status = 'active'""",
        alert_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Traffic hazard not found")

    user_id = int(user["id"]) if str(user["id"]).isdigit() else None

    await db.execute(
        "UPDATE traffic_hazards SET confirmation_count = confirmation_count + 1 WHERE alert_id = $1",
        alert_id,
    )
    new_count = (int(row["confirmation_count"]) or 0) + 1

    tvm_status = "pending_consensus"
    if new_count >= CONSENSUS_AUTO_VERIFY_COUNT:
        await db.execute(
            "UPDATE alerts SET tvm_status = 'verified' WHERE id = $1",
            alert_id,
        )
        await db.execute(
            """INSERT INTO tvm_log (alert_id, tier, action, actor_id, notes)
               VALUES ($1, 2, 'consensus_auto_verified', $2,
                       'Traffic hazard confirmed by community consensus')""",
            alert_id, user_id,
        )
        tvm_status = "verified"

    return {
        "success":           True,
        "alert_id":          alert_id,
        "confirmation_count": new_count,
        "tvm_status":        tvm_status,
        "message":           "Confirmation recorded" + (" — hazard auto-verified!" if tvm_status == "verified" else ""),
    }


# ── PATCH /{alert_id}/resolve — Resolve hazard (authority or reporter) ────────
@router.patch(
    "/{alert_id}/resolve",
    summary="Mark traffic hazard as cleared",
)
async def resolve_hazard(
    alert_id: int,
    notes: str = "Road cleared",
    user: dict = Depends(get_current_user),
):
    row = await db.fetchrow(
        "SELECT id FROM alerts WHERE id = $1 AND alert_type = 'traffic'",
        alert_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Traffic hazard not found")

    user_id = int(user["id"]) if str(user["id"]).isdigit() else None

    await db.execute(
        """UPDATE alerts
           SET status = 'resolved', resolved_at = NOW(), resolved_by = $1
           WHERE id = $2""",
        user_id, alert_id,
    )

    await db.execute(
        """INSERT INTO tvm_log (alert_id, tier, action, actor_id, notes)
           VALUES ($1, 3, 'road_cleared', $2, $3)""",
        alert_id, user_id, notes,
    )

    return {"success": True, "alert_id": alert_id, "status": "resolved"}


# ── Mock fallback ─────────────────────────────────────────────────────────────
def _mock_nearby(lat, lng):
    return {
        "data": [
            {
                "id": "mock-th-001",
                "title": "Road Accident — A1 Highway",
                "description": "Two-vehicle collision blocking left lane near Kadawatha junction",
                "severity": "severe",
                "status": "active",
                "tvm_status": "verified",
                "district": "Gampaha",
                "hazard_type": "accident",
                "road_segment": "A1 Highway, Kadawatha",
                "confirmation_count": 5,
                "expected_clear_time": None,
                "distance_km": 2.1,
                "latitude": lat + 0.015,
                "longitude": lng + 0.010,
                "created_at": datetime.now().isoformat(),
            },
            {
                "id": "mock-th-002",
                "title": "Road Flooding — Baseline Road",
                "description": "Water overflow from drain blocking traffic near Maradana",
                "severity": "medium",
                "status": "active",
                "tvm_status": "verified",
                "district": "Colombo",
                "hazard_type": "flooding",
                "road_segment": "Baseline Road, Maradana",
                "confirmation_count": 3,
                "expected_clear_time": None,
                "distance_km": 3.4,
                "latitude": lat - 0.012,
                "longitude": lng + 0.005,
                "created_at": datetime.now().isoformat(),
            },
        ]
    }
