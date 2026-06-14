"""
Authority Dashboard routes — /api/authority
Backing API for the React Authority Dashboard (TVM Tier-3 review console).
All endpoints require the 'authority' role.
"""
from fastapi import APIRouter, Depends, Query, HTTPException
from typing import Optional
from app.core.security import require_authority
from app.db import database as db

router = APIRouter()

VALID_ALERT_TYPES = [
    "missing_person", "disaster", "crime", "traffic", "health"
]


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
