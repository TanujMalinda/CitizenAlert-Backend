"""
Alert Responses (Tip Line) — /api/alerts/{alert_id}/responses
=============================================================
Lets any citizen send information about an existing alert — e.g. "I saw the
reported bicycle near Pettah market". Works for every alert category
(crime, missing person, traffic, health, disaster).

Tips are NOT public. They are visible to:
  - the authority / super-admin (for investigation), and
  - the original reporter of the alert (so they get leads).
"""
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core.security import get_current_user
from core.errors import http_error
from db import database as db

router = APIRouter()


class CreateResponseRequest(BaseModel):
    message: str
    latitude: Optional[float] = None      # where the responder saw it (optional)
    longitude: Optional[float] = None
    contact_info: Optional[str] = None    # phone/email so authority can follow up


# ── POST /{alert_id}/responses — submit a tip ────────────────────────────────
@router.post(
    "/{alert_id}/responses",
    summary="Send information / a tip about an alert",
    description="Any authenticated user can submit a tip on an existing alert.",
)
async def create_response(
    alert_id: int,
    body: CreateResponseRequest,
    user: dict = Depends(get_current_user),
):
    if len(body.message.strip()) < 5:
        http_error(400, "Your message is too short.",
                   "Please describe what you saw in at least 5 characters.")

    alert = await db.fetchrow(
        "SELECT id, title, alert_type FROM alerts WHERE id = $1", alert_id
    )
    if not alert:
        http_error(404, "That alert no longer exists.",
                   "It may have been resolved or removed.")

    user_id = int(user["id"]) if str(user["id"]).isdigit() else None

    row = await db.fetchrow(
        """INSERT INTO alert_responses
             (alert_id, responder_id, message, latitude, longitude, contact_info)
           VALUES ($1, $2, $3, $4, $5, $6)
           RETURNING id, created_at""",
        alert_id, user_id, body.message.strip(),
        body.latitude, body.longitude, body.contact_info,
    )

    # Log it in the TVM audit trail as a corroborating tip (Tier 2 signal)
    await db.execute(
        """INSERT INTO tvm_log (alert_id, tier, action, actor_id, notes)
           VALUES ($1, 2, 'citizen_tip_received', $2, $3)""",
        alert_id, user_id,
        f"Tip on '{alert['title']}': {body.message.strip()[:160]}",
    )

    return {
        "success": True,
        "response_id": int(row["id"]),
        "alert_id": alert_id,
        "message": "Thank you — your information has been sent to the authorities.",
        "created_at": row["created_at"].isoformat(),
    }


# ── GET /{alert_id}/responses — list tips (authority or alert owner) ─────────
@router.get(
    "/{alert_id}/responses",
    summary="List tips submitted on an alert",
    description="Visible to authorities and to the user who created the alert.",
)
async def list_responses(
    alert_id: int,
    user: dict = Depends(get_current_user),
):
    alert = await db.fetchrow(
        "SELECT id, user_id FROM alerts WHERE id = $1", alert_id
    )
    if not alert:
        http_error(404, "That alert no longer exists.")

    role    = user.get("role")
    user_id = int(user["id"]) if str(user["id"]).isdigit() else None
    is_privileged = role in ("authority", "super_admin")
    is_owner      = alert["user_id"] is not None and alert["user_id"] == user_id

    if not (is_privileged or is_owner):
        http_error(403, "You cannot view tips for this alert.",
                   "Only authorities and the original reporter can see submitted tips.")

    rows = await db.fetch(
        """SELECT ar.id, ar.message, ar.latitude, ar.longitude,
                  ar.contact_info, ar.created_at,
                  u.full_name AS responder_name
           FROM alert_responses ar
           LEFT JOIN users u ON u.id = ar.responder_id
           WHERE ar.alert_id = $1
           ORDER BY ar.created_at DESC""",
        alert_id,
    )

    data = [dict(r) for r in rows] if rows else []
    return {"success": True, "count": len(data), "data": data}


# ── GET /{alert_id}/responses/count — public lightweight count ───────────────
@router.get(
    "/{alert_id}/responses/count",
    summary="Number of tips on an alert",
)
async def count_responses(
    alert_id: int,
    user: dict = Depends(get_current_user),
):
    n = await db.fetchval(
        "SELECT COUNT(*) FROM alert_responses WHERE alert_id = $1", alert_id
    )
    return {"success": True, "alert_id": alert_id, "count": int(n or 0)}
