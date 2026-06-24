"""
Notifications routes — /api/notifications
The mobile app polls these to display in-app + local notifications.
"""
from fastapi import APIRouter, Depends, Query, HTTPException
from core.security import get_current_user
from db import database as db

router = APIRouter()


def _uid(user: dict) -> int | None:
    return int(user["id"]) if str(user.get("id", "")).isdigit() else None


# ── GET / — list this user's notifications ────────────────────────────────────
@router.get("/", summary="List my notifications (newest first)")
async def list_notifications(
    unread_only: bool = Query(False),
    since_id:    int = Query(0, description="Only return notifications with id > this"),
    limit:       int = Query(50, ge=1, le=200),
    user: dict = Depends(get_current_user),
):
    uid = _uid(user)
    if uid is None:
        return {"success": True, "count": 0, "data": []}

    rows = await db.fetch(
        """SELECT id, alert_id, type, title, body, is_read, created_at
           FROM notifications
           WHERE user_id = $1
             AND ($2 = FALSE OR is_read = FALSE)
             AND id > $3
           ORDER BY created_at DESC
           LIMIT $4""",
        uid, unread_only, since_id, limit,
    )
    data = [dict(r) for r in rows] if rows else []
    return {"success": True, "count": len(data), "data": data}


# ── GET /unread-count — badge number ──────────────────────────────────────────
@router.get("/unread-count", summary="Number of unread notifications")
async def unread_count(user: dict = Depends(get_current_user)):
    uid = _uid(user)
    if uid is None:
        return {"success": True, "unread": 0}
    n = await db.fetchval(
        "SELECT COUNT(*) FROM notifications WHERE user_id = $1 AND is_read = FALSE",
        uid,
    )
    return {"success": True, "unread": int(n or 0)}


# ── POST /{id}/read — mark one read ───────────────────────────────────────────
@router.post("/{notification_id}/read", summary="Mark a notification as read")
async def mark_read(notification_id: int, user: dict = Depends(get_current_user)):
    uid = _uid(user)
    if uid is None:
        raise HTTPException(status_code=400, detail="Invalid user")
    await db.execute(
        "UPDATE notifications SET is_read = TRUE WHERE id = $1 AND user_id = $2",
        notification_id, uid,
    )
    return {"success": True}


# ── POST /read-all — mark everything read ─────────────────────────────────────
@router.post("/read-all", summary="Mark all my notifications as read")
async def mark_all_read(user: dict = Depends(get_current_user)):
    uid = _uid(user)
    if uid is None:
        raise HTTPException(status_code=400, detail="Invalid user")
    await db.execute(
        "UPDATE notifications SET is_read = TRUE WHERE user_id = $1 AND is_read = FALSE",
        uid,
    )
    return {"success": True}
