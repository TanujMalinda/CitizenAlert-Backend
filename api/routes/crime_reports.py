"""
Crime Reports routes — /api/crime-reports
Hyper-local incident reporting with high-sensitivity TVM.
Alert categories: theft, assault, robbery, vandalism, fraud, suspicious_activity
"""
from datetime import datetime
from fastapi import APIRouter, Depends, Query, HTTPException
from pydantic import BaseModel
from typing import Optional
from core.security import get_current_user, require_authority
from db import database as db

router = APIRouter()

VALID_INCIDENT_TYPES = [
    "theft", "assault", "robbery", "vandalism",
    "fraud", "suspicious_activity", "burglary", "other"
]


class CreateCrimeReportRequest(BaseModel):
    title: str
    description: str
    incident_type: str
    severity: str = "medium"
    latitude: float
    longitude: float
    district: str
    incident_time: str
    suspect_description: Optional[str] = None
    evidence_url: Optional[str] = None
    police_case_number: Optional[str] = None
    anonymous: bool = False


class ReviewCrimeReportRequest(BaseModel):
    action: str   # "verify" | "reject"
    notes: Optional[str] = None
    police_case_number: Optional[str] = None


# ── GET /nearby ───────────────────────────────────────────────────────────────
@router.get(
    "/nearby",
    summary="Get nearby crime reports",
    description="""
Returns active crime reports within radius_km using PostGIS.
High-sensitivity — only verified reports are shown to citizens.
**Try:** latitude=6.9271, longitude=79.8612, radius_km=5
    """,
)
async def get_nearby_crimes(
    latitude:      float = Query(..., ge=5.916, le=9.836),
    longitude:     float = Query(..., ge=79.695, le=81.879),
    radius_km:     float = Query(5.0, ge=0.5, le=50.0),
    incident_type: Optional[str] = Query(None),
    user: dict = Depends(get_current_user),
):
    # Citizens only see verified reports
    # Authorities see all including pending
    is_authority = user.get("role") == "authority"
    tvm_filter   = "AND a.tvm_status IN ('verified', 'passed')" \
                   if not is_authority else ""

    rows = await db.fetch(
        f"""SELECT
               a.id, a.title, a.description, a.severity,
               a.status, a.tvm_status, a.district, a.created_at,
               cr.incident_type, cr.suspect_description,
               cr.police_case_number,
               CASE WHEN $5 THEN cr.evidence_url ELSE NULL END AS evidence_url,
               ROUND((ST_Distance(
                   COALESCE(
                       a.geom,
                       ST_SetSRID(ST_MakePoint(a.longitude, a.latitude), 4326)
                   )::geography,
                   ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography
               ) / 1000.0)::numeric, 2)::float AS distance_km,
               a.latitude, a.longitude
           FROM alerts a
           JOIN crime_reports cr ON cr.alert_id = a.id
           WHERE a.alert_type = 'crime'
             AND a.status = 'active'
             AND ($3::text IS NULL OR cr.incident_type = $3)
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
        longitude, latitude, incident_type,
        radius_km * 1000, is_authority,
    )

    data = [dict(r) for r in rows] if rows else []
    return {
        "success": True,
        "count":   len(data),
        "note":    "Showing verified reports only" if not is_authority
                   else "Authority view — all reports including pending",
        "data":    data,
    }


# ── POST / — Submit crime report ──────────────────────────────────────────────
@router.post(
    "/",
    summary="Submit a crime report",
    description="""
Citizen submits a crime report. Goes through high-sensitivity TVM Tier 3
(mandatory authority review before public dissemination).
    """,
)
async def create_crime_report(
    body: CreateCrimeReportRequest,
    user: dict = Depends(get_current_user),
):
    if body.incident_type not in VALID_INCIDENT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid incident_type. Must be one of: {VALID_INCIDENT_TYPES}"
        )

    user_id = int(user["id"]) if str(user["id"]).isdigit() else None

    # Crime reports always start as pending_authority_review
    # High sensitivity — never auto-verified
    row = await db.fetchrow(
        """INSERT INTO alerts
             (title, description, latitude, longitude, status, user_id,
              alert_type, tvm_status, tvm_score, severity, district, geom)
           VALUES ($1, $2, $3, $4, 'active', $5,
                   'crime', 'pending_authority_review', 0, $6, $7,
                   ST_SetSRID(ST_MakePoint($4, $3), 4326))
           RETURNING id""",
        body.title, body.description,
        body.latitude, body.longitude,
        user_id, body.severity, body.district,
    )
    alert_id = int(row["id"])
    cap_id   = f"LK-CA-CR-{int(datetime.now().timestamp())}-{str(alert_id).zfill(6)}"

    # Insert crime extension
    await db.execute(
        """INSERT INTO crime_reports
             (alert_id, incident_type, suspect_description,
              evidence_url, police_case_number)
           VALUES ($1, $2, $3, $4, $5)""",
        alert_id, body.incident_type,
        body.suspect_description,
        body.evidence_url,
        body.police_case_number,
    )

    # Log TVM — crime always goes to Tier 3
    await db.execute(
        """INSERT INTO tvm_log (alert_id, tier, action, actor_id, notes)
           VALUES ($1, 3, 'escalated_to_authority', $2,
                   'Crime report — mandatory authority review (high sensitivity)')""",
        alert_id, user_id,
    )

    return {
        "success":        True,
        "alert_id":       alert_id,
        "cap_identifier": cap_id,
        "tvm_status":     "pending_authority_review",
        "message":        "Crime report submitted. Under authority review before public dissemination.",
        "note":           "High-sensitivity alert — TVM Tier 3 mandatory review applies",
    }


# ── POST /{alert_id}/review — Authority reviews crime report ──────────────────
@router.post(
    "/{alert_id}/review",
    summary="Authority reviews a crime report (authority only)",
    description="""
Authority verifies or rejects a pending crime report.
- verify → alert becomes public, citizens can see it
- reject → alert removed from public feed
    """,
)
async def review_crime_report(
    alert_id: int,
    body: ReviewCrimeReportRequest,
    user: dict = Depends(require_authority),
):
    if body.action not in ["verify", "reject"]:
        raise HTTPException(
            status_code=400,
            detail="Action must be 'verify' or 'reject'"
        )

    user_id    = int(user["id"])
    tvm_status = "verified" if body.action == "verify" else "rejected"

    row = await db.fetchrow(
        "SELECT id FROM alerts WHERE id = $1 AND alert_type = 'crime'",
        alert_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Crime report not found")

    await db.execute(
        """UPDATE alerts
           SET tvm_status  = $1,
               resolved_by = $2
           WHERE id = $3""",
        tvm_status, user_id, alert_id,
    )

    # Update police case number if provided
    if body.police_case_number:
        await db.execute(
            "UPDATE crime_reports SET police_case_number = $1 WHERE alert_id = $2",
            body.police_case_number, alert_id,
        )

    # Log authority decision
    await db.execute(
        """INSERT INTO tvm_log (alert_id, tier, action, actor_id, notes)
           VALUES ($1, 3, $2, $3, $4)""",
        alert_id,
        f"authority_{body.action}d",
        user_id,
        body.notes or f"Crime report {body.action}d by authority",
    )

    return {
        "success":    True,
        "alert_id":   alert_id,
        "tvm_status": tvm_status,
        "action":     body.action,
        "message":    f"Crime report {body.action}d successfully",
    }


# ── PATCH /{alert_id}/resolve ─────────────────────────────────────────────────
@router.patch(
    "/{alert_id}/resolve",
    summary="Resolve a crime report (authority only)",
)
async def resolve_crime_report(
    alert_id: int,
    resolution_notes: str = "Case closed",
    user: dict = Depends(require_authority),
):
    user_id = int(user["id"])

    row = await db.fetchrow(
        "SELECT id FROM alerts WHERE id = $1 AND alert_type = 'crime'",
        alert_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Crime report not found")

    await db.execute(
        """UPDATE alerts
           SET status      = 'resolved',
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
        "success":  True,
        "alert_id": alert_id,
        "status":   "resolved",
        "message":  "Crime report resolved",
    }