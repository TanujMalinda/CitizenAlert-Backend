from fastapi import APIRouter

router = APIRouter()


@router.post("/evaluate")
def evaluate():
	return {
		"score": 0.85,
		"status": "processed",
		"message": "TVM evaluation complete (mock)",
	}
