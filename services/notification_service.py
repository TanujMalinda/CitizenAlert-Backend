"""
Notification service
=====================
Single origin point for all user notifications.

Right now this persists a row to the `notifications` table, which the mobile app
polls. This is intentionally the ONLY place notifications are created, so adding
real push later (Firebase Cloud Messaging) means editing just this function:
register device tokens, then after the DB insert also call FCM here. No other
route code has to change.
"""
from db import database as db


async def create_notification(
    user_id: int | None,
    alert_id: int | None,
    type: str,
    title: str,
    body: str,
) -> None:
    """
    Persist a notification for a user. Safe to call with user_id=None
    (e.g. anonymous reporter) — it simply no-ops.

    type: machine-readable category, e.g.
          'alert_verified' | 'alert_rejected' | 'alert_resolved'
    """
    if user_id is None:
        return

    await db.execute(
        """INSERT INTO notifications (user_id, alert_id, type, title, body)
           VALUES ($1, $2, $3, $4, $5)""",
        user_id, alert_id, type, title, body,
    )

    # ── FCM hook (future) ─────────────────────────────────────────────
    # When push is added, fetch the user's device tokens here and send the
    # same title/body via FCM. The app already displays notifications through
    # a single NotificationService, so no client changes are needed either.
    # await _send_fcm(user_id, title, body, {"alert_id": str(alert_id)})


async def notify_alert_status_change(
    alert_id: int,
    new_status: str,
) -> None:
    """
    Convenience helper: looks up the alert's reporter and notifies them that
    their report's verification status changed. Called from review / resolve
    endpoints. `new_status` is the human outcome: 'verified' | 'rejected' |
    'resolved'.
    """
    row = await db.fetchrow(
        "SELECT user_id, title, alert_type FROM alerts WHERE id = $1", alert_id
    )
    if not row or row["user_id"] is None:
        return

    title_word = {
        "verified": "Report verified",
        "rejected": "Report not approved",
        "resolved": "Report resolved",
    }.get(new_status, "Report updated")

    body_word = {
        "verified": "Your report has been verified and is now visible to other citizens.",
        "rejected": "Your report was reviewed but not approved for broadcast.",
        "resolved": "Your report has been marked resolved. Thank you for helping keep your community safe.",
    }.get(new_status, "The status of your report has changed.")

    await create_notification(
        user_id=int(row["user_id"]),
        alert_id=alert_id,
        type=f"alert_{new_status}",
        title=title_word,
        body=f'“{row["title"]}” — {body_word}',
    )
