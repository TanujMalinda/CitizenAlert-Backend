from datetime import datetime

from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
def cctv_health():
	return {
		"status": "ok",
		"service": "cctv-metadata",
		"timestamp": datetime.utcnow().isoformat(),
	}
