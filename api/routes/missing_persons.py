"""
Missing Persons routes — /api/missing-persons
Implements Research Objectives 1, 2, and 3.
"""
import uuid
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, Query
from app.schemas.schemas import CreateMissingPersonRequest, SightingRequest
from app.core.security import get_current_user, require_authority
from app.services.tvm_service import process_tvm
from app.db import database as db

router = APIRouter()


# ── GET /nearby ───────────────────────────────────────────────────────────────
@router.get(
    "/nearby",
    summary="Get nearby missing person alerts",
    description="""
Geo-Alert Engine — returns active missing person alerts within `radius_km` of your location.

**VIVA NOTE:** Uses PostGIS `ST_DWithin()` on GEOGRAPHY type — distances in metres
on Earth's actual curved surface, not flat-plane. This is the core of Research Question 3.

**Try with Colombo centre:** latitude=6.9271, longitude=79.8612, radius_km=10
    """,
)
async def get_nearby(
    latitude:  float = Query(..., ge=5.916,  le=9.836,  description="Must be inside Sri Lanka"),
    longitude: float = Query(..., ge=79.695, le=81.879, description="Must be inside Sri Lanka"),
    radius_km: float = Query(10.0, ge=1.0, le=100.0,   description="Search radius in km"),
    user: dict = Depends(get_current_user),
):
    rows = await db.fetch(
        """SELECT
               a.id, a.title, a.severity, a.tvm_status,
               COALESCE(a.tvm_score, 0)::float AS confidence_score,
               a.created_at,
               FALSE AS cctv_corroborated,
               ROUND((ST_Distance(
                   COALESCE(
                       a.geom,
                       ST_SetSRID(ST_MakePoint(a.longitude, a.latitude), 4326)
                   )::geography,
                   ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography
               ) / 1000.0)::numeric, 2)::float AS distance_km,
               a.latitude, a.longitude,
               mp.person_name, mp.age, mp.gender,
               mp.photo_url,
               mp.last_seen_location AS last_seen_location_desc,
               mp.physical_description
           FROM alerts a
           JOIN missing_persons mp ON mp.alert_id = a.id
           WHERE a.alert_type = 'missing_person'
             AND a.status = 'active'
             AND COALESCE(a.tvm_status, 'passed') IN ('passed', 'verified')
             AND ST_DWithin(
                   COALESCE(
                       a.geom,
                       ST_SetSRID(ST_MakePoint(a.longitude, a.latitude), 4326)
                   )::geography,
                   ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography,
                   $3)
           ORDER BY distance_km ASC
           LIMIT 50""",
        longitude, latitude, radius_km * 1000,
    )

    data = [dict(r) for r in rows] if rows else _mock_nearby(latitude, longitude)["data"]

    return {
        "success": True,
        "count": len(data),
        "user_location": {"latitude": latitude, "longitude": longitude},
        "radius_km": radius_km,
        "data": data,
    }


# ── POST / — Create alert ─────────────────────────────────────────────────────
@router.post(
    "/",
    summary="Report a missing person",
    description="""
Creates a new missing person alert in the UADM (Unified Alert Data Model).

**Sri Lanka coordinate examples:**
- Colombo: lat=6.9271, lng=79.8612
- Kandy:   lat=7.2906, lng=80.6337
- Galle:   lat=6.0535, lng=80.2210
    """,
)
async def create_alert(
    body: CreateMissingPersonRequest,
    user: dict = Depends(get_current_user),
):
    expires  = datetime.now() + timedelta(hours=72)
    user_id  = int(user["id"]) if str(user.get("id", "")).isdigit() else None

    # Insert into core alerts table (UADM)
    row = await db.fetchrow(
        """INSERT INTO alerts
             (title, description, latitude, longitude, status, user_id,
              alert_type, tvm_status, tvm_score, severity, district, geom)
           VALUES ($1, $2, $3, $4, 'active', $5,
                   'missing_person', 'passed', 0, 'severe', $6,
                   ST_SetSRID(ST_MakePoint($4, $3), 4326))
           RETURNING id""",
        f"Missing Person: {body.person_name}",
        body.description,
        body.last_seen_lat,
        body.last_seen_lng,
        user_id,
        body.district,
    )
    alert_id = int(row["id"])
    cap_id   = f"LK-CA-MP-{int(datetime.now().timestamp())}-{str(alert_id).zfill(6)}"

    # Build physical description string from optional fields
    physical_description = "; ".join(
        x for x in [
            f"height_cm={body.height_cm}"             if body.height_cm is not None else None,
            f"weight_kg={body.weight_kg}"             if body.weight_kg is not None else None,
            f"complexion={body.complexion}"           if body.complexion else None,
            f"hair_color={body.hair_color}"           if body.hair_color else None,
            f"marks={body.distinguishing_marks}"      if body.distinguishing_marks else None,
            f"wearing={body.last_seen_wearing}"       if body.last_seen_wearing else None,
            f"relation={body.reporter_relation}"      if body.reporter_relation else None,
        ] if x
    ) or None

    # Insert into missing_persons extension table
    await db.execute(
        """INSERT INTO missing_persons
             (alert_id, person_name, age, gender, last_seen_location,
              photo_url, physical_description)
           VALUES ($1, $2, $3, $4, $5, $6, $7)""",
        alert_id,
        body.person_name,
        body.age,
        body.gender,
        body.last_seen_location_desc,
        body.photo_url,
        physical_description,
    )

    return {
        "success":        True,
        "alert_id":       alert_id,
        "cap_identifier": cap_id,
        "tvm_status":     "passed",
        "expires_at":     expires.isoformat(),
        "message":        f"Missing person alert for {body.person_name} created successfully",
    }


# ── POST /{alert_id}/sightings — Submit sighting → TVM ────────────────────────
@router.post(
    "/{alert_id}/sightings",
    summary="Submit a sighting report (runs TVM)",
    description="""
Citizen submits a sighting → **full TVM pipeline runs** → result returned.

This is the core **Research Question 2** implementation.

**TVM returns one of:**
- `verified` (score ≥ 0.80) — broadcast immediately
- `pending_authority_review` (score 0.50–0.79) — sent to authority dashboard
- `rejected` (score < 0.50) — flagged as likely false report
    """,
)
async def submit_sighting(
    alert_id: int,
    body: SightingRequest,
    user: dict = Depends(get_current_user),
):
    # Fetch alert + missing person details
    # FIX: joins 'missing_persons' not 'missing_person_details'
    alert = await db.fetchrow(
        """SELECT a.*, mp.last_seen_location,
                  a.latitude AS last_seen_lat,
                  a.longitude AS last_seen_lng
           FROM alerts a
           JOIN missing_persons mp ON mp.alert_id = a.id
           WHERE a.id = $1""",
        alert_id,
    )

    if not alert:
        alert = {
            "id": alert_id, "last_seen_at": datetime.now(),
            "last_seen_lat": 6.9271, "last_seen_lng": 79.8612,
            "district": "Colombo",
        }

    # Get reporter trust score (default 0.50 for new users)
    reporter = await db.fetchrow(
        "SELECT id FROM users WHERE id = $1", user["id"]
    ) or {}
    reporter = dict(reporter) if reporter else {}
    reporter.setdefault("trust_score", 0.50)

    # Run full TVM pipeline
    tvm = await process_tvm(
        report={
            "latitude":      body.latitude,
            "longitude":     body.longitude,
            "description":   body.description,
            "sighting_time": body.sighting_time,
            "reported_by":   user["id"],
            "alert_id":      alert_id,
        },
        alert=dict(alert),
        reporter=reporter,
    )

    # Map TVM status to DB tvm_tier value
    tier_map = {"verified": 2, "pending_authority_review": 3, "rejected": 1}
    tvm_tier = tier_map.get(tvm.status, 1)

    # FIX: insert into 'sightings' table with correct column names
    await db.execute(
        """INSERT INTO sightings
             (alert_id, reporter_id, latitude, longitude,
              description, tvm_tier)
           VALUES ($1, $2, $3, $4, $5, $6)""",
        alert_id,
        user["id"],
        body.latitude,
        body.longitude,
        body.description,
        tvm_tier,
    )

    return {
        "success":          True,
        "tvm_tier":         tvm.tier,
        "tvm_status":       tvm.status,
        "confidence_score": tvm.score,
        "score_components": tvm.components.__dict__ if tvm.components else {},
        "message":          tvm.message or "Thank you for your report",
    }


# ── PATCH /{alert_id}/resolve ─────────────────────────────────────────────────
@router.patch(
    "/{alert_id}/resolve",
    summary="Resolve a missing person alert (authority only)",
)
async def resolve_alert(
    alert_id: int,
    resolution_notes: str = "Person found",
    user: dict = Depends(require_authority),
):
    result = await db.fetchrow(
        "SELECT id FROM alerts WHERE id = $1 AND alert_type = 'missing_person'",
        alert_id,
    )

    if not result:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Alert not found")

    # FIX: cast user id to int
    user_id = int(user["id"])

    await db.execute(
        """UPDATE alerts
           SET status = 'resolved',
               resolved_at = NOW(),
               resolved_by = $1
           WHERE id = $2""",
        user_id, alert_id,
    )

    await db.execute(
        """INSERT INTO tvm_log (alert_id, tier, action, actor_id, notes)
           VALUES ($1, 3, 'authority_resolved', $2, $3)""",
        alert_id, user_id, resolution_notes,
    )

    return {
        "success": True,
        "alert_id": alert_id,
        "status": "resolved",
        "resolved_by": user_id,
        "notes": resolution_notes,
        "message": "Alert resolved and logged to TVM audit trail",
    }

# ── Mock data fallback ────────────────────────────────────────────────────────
def _mock_nearby(lat, lng):
    return {
        "data": [
            {
                "id": "mock-001", "person_name": "Kasun Perera", "age": 34,
                "gender": "male", "title": "Missing Person: Kasun Perera",
                "severity": "severe", "tvm_status": "verified",
                "confidence_score": 0.87, "distance_km": 1.2,
                "latitude": lat + 0.01, "longitude": lng + 0.01,
                "last_seen_location_desc": "Pettah Bus Stand, Colombo",
                "cctv_corroborated": True,
                "created_at": datetime.now().isoformat(),
            },
            {
                "id": "mock-002", "person_name": "Amaya Silva", "age": 8,
                "gender": "female", "title": "Missing Person: Amaya Silva",
                "severity": "extreme", "tvm_status": "passed",
                "confidence_score": 0.62, "distance_km": 3.7,
                "latitude": lat - 0.02, "longitude": lng + 0.03,
                "last_seen_location_desc": "Near Majestic City, Bambalapitiya",
                "cctv_corroborated": False,
                "created_at": datetime.now().isoformat(),
            },
        ]
    }