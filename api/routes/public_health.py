"""
Public Health Warnings routes — /api/public-health
District/province-level health alerts with mandatory medical authority verification.
Alert categories: dengue, leptospirosis, cholera, food_poisoning, respiratory, vector_borne
"""
from datetime import datetime
from fastapi import APIRouter, Depends, Query, HTTPException
from pydantic import BaseModel
from typing import Optional
from core.security import get_current_user, require_authority
from db import database as db

router = APIRouter()

VALID_DISEASE_TYPES = [
    "dengue", "leptospirosis", "cholera", "covid",
    "food_poisoning", "respiratory", "vector_borne", "other"
]

VALID_SEVERITIES = ["extreme", "severe", "medium", "low"]


class CreateHealthAlertRequest(BaseModel):
    title: str
    description: str
    disease_type: str
    severity: str = "medium"
    latitude: float
    longitude: float
    district: str
    case_count: Optional[int] = None           # confirmed case count
    prevention_protocols: Optional[str] = None  # WHO/MOH-sourced prevention steps
    health_facility: Optional[str] = None       # nearest treatment facility
    official_source: Optional[str] = None       # MOH / WHO / PHI reference


class ReviewHealthAlertRequest(BaseModel):
    action: str   # "verify" | "reject"
    notes: Optional[str] = None


class UpdateHealthStatusRequest(BaseModel):
    status: str
    notes: Optional[str] = None


# ── GET /nearby ───────────────────────────────────────────────────────────────
@router.get(
    "/nearby",
    summary="Get nearby public health warnings",
    description="""
Returns active health warnings within radius_km.
Uses a larger default radius (100km) as health outbreaks affect macro districts.
**Try:** latitude=6.9271, longitude=79.8612, radius_km=100
    """,
)
async def get_nearby_health(
    latitude:     float = Query(..., ge=5.916, le=9.836),
    longitude:    float = Query(..., ge=79.695, le=81.879),
    radius_km:    float = Query(100.0, ge=1.0, le=250.0),
    disease_type: Optional[str] = Query(None),
    user: dict = Depends(get_current_user),
):
    is_authority = user.get("role") == "authority"
    tvm_filter   = "AND a.tvm_status IN ('verified', 'passed')" \
                   if not is_authority else ""

    rows = await db.fetch(
        f"""SELECT
               a.id, a.title, a.description, a.severity,
               a.status, a.tvm_status, a.district, a.created_at,
               ph.disease_type, ph.case_count,
               ph.prevention_protocols, ph.health_facility,
               ph.official_source,
               ROUND((ST_Distance(
                   COALESCE(
                       a.geom,
                       ST_SetSRID(ST_MakePoint(a.longitude, a.latitude), 4326)
                   )::geography,
                   ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography
               ) / 1000.0)::numeric, 2)::float AS distance_km,
               a.latitude, a.longitude
           FROM alerts a
           JOIN public_health_alerts ph ON ph.alert_id = a.id
           WHERE a.alert_type = 'health'
             AND a.status = 'active'
             AND ($3::text IS NULL OR ph.disease_type = $3)
             {tvm_filter}
             AND ST_DWithin(
                   COALESCE(
                       a.geom,
                       ST_SetSRID(ST_MakePoint(a.longitude, a.latitude), 4326)
                   )::geography,
                   ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography,
                   $4)
           ORDER BY a.severity DESC, distance_km ASC
           LIMIT 50""",
        longitude, latitude, disease_type, radius_km * 1000,
    )

    data = [dict(r) for r in rows] if rows else _mock_nearby(latitude, longitude)["data"]
    return {
        "success": True,
        "count":   len(data),
        "note":    "Health warnings use wider radius (district/province scope)",
        "data":    data,
    }


# ── POST / — Submit health alert (citizens can report, authority verifies) ────
@router.post(
    "/",
    summary="Report a public health concern",
    description="""
Citizens can flag health concerns (suspected outbreak, food poisoning cluster, etc.).
**Critical sensitivity** — mandatory Tier 3 authority/medical review before public dissemination.
Authority-created alerts with official_source bypass TVM (pre-verified).
    """,
)
async def create_health_alert(
    body: CreateHealthAlertRequest,
    user: dict = Depends(get_current_user),
):
    if body.disease_type not in VALID_DISEASE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid disease_type. Must be one of: {VALID_DISEASE_TYPES}"
        )
    if body.severity not in VALID_SEVERITIES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid severity. Must be one of: {VALID_SEVERITIES}"
        )

    user_id      = int(user["id"]) if str(user["id"]).isdigit() else None
    is_authority = user.get("role") == "authority"

    # Authority with official source → bypass TVM (pre-verified like disaster alerts)
    # Citizen report → mandatory Tier 3 authority review (critical sensitivity)
    tvm_status = "verified" if is_authority and body.official_source else "pending_authority_review"

    row = await db.fetchrow(
        """INSERT INTO alerts
             (title, description, latitude, longitude, status, user_id,
              alert_type, tvm_status, tvm_score, severity, district, geom)
           VALUES ($1, $2, $3, $4, 'active', $5,
                   'health', $6, 0, $7, $8,
                   ST_SetSRID(ST_MakePoint($4, $3), 4326))
           RETURNING id""",
        body.title, body.description,
        body.latitude, body.longitude,
        user_id, tvm_status, body.severity, body.district,
    )
    alert_id = int(row["id"])
    cap_id   = f"LK-CA-PH-{int(datetime.now().timestamp())}-{str(alert_id).zfill(6)}"

    await db.execute(
        """INSERT INTO public_health_alerts
             (alert_id, disease_type, case_count,
              prevention_protocols, health_facility, official_source)
           VALUES ($1, $2, $3, $4, $5, $6)""",
        alert_id, body.disease_type, body.case_count,
        body.prevention_protocols, body.health_facility,
        body.official_source,
    )

    tvm_action = "official_source_bypass" if tvm_status == "verified" \
                 else "escalated_to_authority"
    tvm_note   = "Health alert from authority — TVM bypassed" if tvm_status == "verified" \
                 else "Health alert — critical sensitivity, mandatory medical authority review"

    await db.execute(
        """INSERT INTO tvm_log (alert_id, tier, action, actor_id, notes)
           VALUES ($1, $2, $3, $4, $5)""",
        alert_id,
        1 if tvm_status == "verified" else 3,
        tvm_action, user_id, tvm_note,
    )

    return {
        "success":        True,
        "alert_id":       alert_id,
        "cap_identifier": cap_id,
        "disease_type":   body.disease_type,
        "tvm_status":     tvm_status,
        "message":        "Health alert created and broadcast" if tvm_status == "verified"
                          else "Health concern submitted — pending medical authority review before public dissemination",
        "note":           "Critical sensitivity — TVM Tier 3 mandatory for citizen submissions",
    }


# ── POST /{alert_id}/review — Authority reviews citizen health report ─────────
@router.post(
    "/{alert_id}/review",
    summary="Medical authority reviews a health report (authority only)",
)
async def review_health_alert(
    alert_id: int,
    body: ReviewHealthAlertRequest,
    user: dict = Depends(require_authority),
):
    if body.action not in ["verify", "reject"]:
        raise HTTPException(status_code=400, detail="Action must be 'verify' or 'reject'")

    row = await db.fetchrow(
        "SELECT id FROM alerts WHERE id = $1 AND alert_type = 'health'",
        alert_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Health alert not found")

    user_id    = int(user["id"])
    tvm_status = "verified" if body.action == "verify" else "rejected"

    await db.execute(
        """UPDATE alerts SET tvm_status = $1, resolved_by = $2 WHERE id = $3""",
        tvm_status, user_id, alert_id,
    )

    await db.execute(
        """INSERT INTO tvm_log (alert_id, tier, action, actor_id, notes)
           VALUES ($1, 3, $2, $3, $4)""",
        alert_id,
        f"authority_{body.action}d",
        user_id,
        body.notes or f"Health alert {body.action}d by medical authority",
    )

    return {
        "success":    True,
        "alert_id":   alert_id,
        "tvm_status": tvm_status,
        "action":     body.action,
        "message":    f"Health alert {body.action}d successfully",
    }


# ── PATCH /{alert_id}/update-count — Update confirmed case count ──────────────
@router.patch(
    "/{alert_id}/update-count",
    summary="Update confirmed case count (authority only)",
)
async def update_case_count(
    alert_id:  int,
    case_count: int,
    user: dict = Depends(require_authority),
):
    row = await db.fetchrow(
        "SELECT id FROM alerts WHERE id = $1 AND alert_type = 'health'",
        alert_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Health alert not found")

    await db.execute(
        "UPDATE public_health_alerts SET case_count = $1 WHERE alert_id = $2",
        case_count, alert_id,
    )

    return {
        "success":    True,
        "alert_id":   alert_id,
        "case_count": case_count,
        "message":    "Case count updated",
    }


# ── PATCH /{alert_id}/status ───────────────────────────────────────────────────
@router.patch(
    "/{alert_id}/status",
    summary="Update health alert status (authority only)",
)
async def update_status(
    alert_id: int,
    body: UpdateHealthStatusRequest,
    user: dict = Depends(require_authority),
):
    valid_statuses = ["active", "resolved", "cancelled"]
    if body.status not in valid_statuses:
        raise HTTPException(
            status_code=400,
            detail=f"Status must be one of: {valid_statuses}"
        )

    user_id = int(user["id"])

    await db.execute(
        """UPDATE alerts
           SET status      = $1,
               resolved_at = CASE WHEN $1 = 'resolved' THEN NOW() ELSE NULL END,
               resolved_by = $2
           WHERE id = $3 AND alert_type = 'health'""",
        body.status, user_id, alert_id,
    )

    await db.execute(
        """INSERT INTO tvm_log (alert_id, tier, action, actor_id, notes)
           VALUES ($1, 3, $2, $3, $4)""",
        alert_id,
        f"status_changed_to_{body.status}",
        user_id,
        body.notes or f"Health alert status updated to {body.status}",
    )

    return {
        "success":  True,
        "alert_id": alert_id,
        "status":   body.status,
        "message":  f"Health alert {body.status} successfully",
    }


# ── Mock fallback ─────────────────────────────────────────────────────────────
def _mock_nearby(lat, lng):
    return {
        "data": [
            {
                "id": "mock-ph-001",
                "title": "Dengue Fever Outbreak — Colombo District",
                "description": "Elevated dengue cases reported in Colombo and surrounding areas. Residents advised to eliminate stagnant water.",
                "severity": "severe",
                "status": "active",
                "tvm_status": "verified",
                "district": "Colombo",
                "disease_type": "dengue",
                "case_count": 47,
                "prevention_protocols": "Eliminate stagnant water sources. Use mosquito repellent. Seek medical attention if fever persists.",
                "health_facility": "National Hospital Colombo — Fever Clinic",
                "official_source": "Ministry of Health Sri Lanka — Epidemiology Unit",
                "distance_km": 5.2,
                "latitude": lat + 0.02,
                "longitude": lng - 0.01,
                "created_at": datetime.now().isoformat(),
            },
            {
                "id": "mock-ph-002",
                "title": "Leptospirosis Advisory — Western Province",
                "description": "Post-flood leptospirosis risk in flood-affected areas. Avoid wading in floodwater.",
                "severity": "medium",
                "status": "active",
                "tvm_status": "verified",
                "district": "Gampaha",
                "disease_type": "leptospirosis",
                "case_count": 12,
                "prevention_protocols": "Avoid contact with floodwater. Wear protective footwear. Prophylactic treatment available at PHI.",
                "health_facility": "Gampaha General Hospital",
                "official_source": "PHI Gampaha — Post-Flood Health Advisory",
                "distance_km": 18.7,
                "latitude": lat + 0.05,
                "longitude": lng + 0.08,
                "created_at": datetime.now().isoformat(),
            },
        ]
    }
