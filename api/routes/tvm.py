"""
TVM routes — /api/tvm
Exposes the Tiered Verification Mechanism pipeline for inspection and dry-run evaluation.
"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from typing import Optional
from core.security import get_current_user
from services.tvm_service import (
    process_tvm_for_alert,
    TVM_AUTO_VERIFY_THRESHOLD,
    TVM_AUTHORITY_REVIEW_THRESHOLD,
    WEIGHTS,
)

router = APIRouter()


class EvaluateTVMRequest(BaseModel):
    latitude: float
    longitude: float
    description: str


@router.post(
    "/evaluate",
    summary="Dry-run TVM evaluation (Tier 1 + Tier 2)",
    description="""
Runs the TVM pipeline on a hypothetical report without saving anything to the database.
Useful for testing and demonstrating the research contribution.

**Returns:** TVM score (0.0–1.0), tier reached, routing decision, and score breakdown.
    """,
)
async def evaluate_tvm(
    body: EvaluateTVMRequest,
    user: dict = Depends(get_current_user),
):
    user_id = int(user["id"]) if str(user.get("id", "")).isdigit() else None

    result = await process_tvm_for_alert(
        latitude=body.latitude,
        longitude=body.longitude,
        description=body.description,
        user_id=user_id,
        alert_id=None,   # dry-run — nothing persisted
    )

    routing = (
        "auto_verified"         if result.score >= TVM_AUTO_VERIFY_THRESHOLD else
        "authority_review"      if result.score >= TVM_AUTHORITY_REVIEW_THRESHOLD else
        "auto_rejected"
    )

    return {
        "success":    True,
        "tier":       result.tier,
        "status":     result.status,
        "score":      result.score,
        "routing":    routing,
        "thresholds": {
            "auto_verify":      TVM_AUTO_VERIFY_THRESHOLD,
            "authority_review": TVM_AUTHORITY_REVIEW_THRESHOLD,
        },
        "score_components": (
            result.components.__dict__ if result.components else None
        ),
        "message": result.message,
    }


@router.get(
    "/thresholds",
    summary="TVM threshold configuration",
    description="Returns the scoring thresholds used by the TVM pipeline.",
)
async def get_thresholds(user: dict = Depends(get_current_user)):
    return {
        "auto_verify_threshold":      TVM_AUTO_VERIFY_THRESHOLD,
        "authority_review_threshold": TVM_AUTHORITY_REVIEW_THRESHOLD,
        # Read from the scoring service so this can never drift from the
        # weights actually used to verify reports.
        "score_weights":             WEIGHTS,
        "routing_logic": {
            f">= {TVM_AUTO_VERIFY_THRESHOLD}":      "auto_verified",
            f">= {TVM_AUTHORITY_REVIEW_THRESHOLD}": "authority_review (Tier 3)",
            f"< {TVM_AUTHORITY_REVIEW_THRESHOLD}":  "auto_rejected",
        },
    }
