"""
Vision routes — /api/vision
===========================
POST /classify — classify an incident photo (Snap Incident feature).

The mobile app sends a base64 data-URI photo; the trained MobileNetV2 model
(or a placeholder until it is installed) predicts the incident type, which the
app uses to pre-select the report category.
"""
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core.security import get_current_user
from services.vision_service import classify_image

router = APIRouter()


class ClassifyRequest(BaseModel):
    image: str                        # base64 data URI (data:image/jpeg;base64,...)
    reported_type: Optional[str] = None  # optional: what the user claims it is


@router.post(
    "/classify",
    summary="Classify an incident photo (trained model)",
    description="""
Runs the incident image classifier on a photo.

- **Identify mode** — send just `image`: the model predicts the incident type
  so the app can pre-select the right report form.
- **Verify mode** — also send `reported_type` (`traffic` / `disaster`):
  the response includes `matches_report`, a visual-corroboration signal for TVM.
""",
)
async def classify(body: ClassifyRequest, user: dict = Depends(get_current_user)):
    if not body.image or len(body.image) < 100:
        raise HTTPException(status_code=400, detail="A photo is required.")

    try:
        result = classify_image(body.image)
    except Exception:
        raise HTTPException(
            status_code=400,
            detail="Could not read that image — please send a valid JPEG/PNG photo.",
        )

    if body.reported_type:
        result["reported_type"] = body.reported_type
        result["matches_report"] = (
            result["reliable"] and result["alert_type"] == body.reported_type
        )

    return {"success": True, **result}
