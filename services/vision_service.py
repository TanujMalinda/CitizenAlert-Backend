"""
Vision Service — incident image classification
===============================================
Classifies an incident photo into one of the trained classes
(e.g. collapsed_building / fire / flood / normal / traffic_incident).

Serving path:
  - If  models/incident_model.onnx  +  models/labels.json  exist, inference
    runs locally through onnxruntime (the model trained in the Colab notebook).
  - Otherwise a PLACEHOLDER response is returned so the app pipeline can be
    built and demoed before training finishes. The response always carries
    `"engine": "onnx" | "placeholder"` so the client can tell them apart.

The exported model embeds MobileNetV2 preprocessing, so input here is raw
0-255 RGB float32 at the size recorded in labels.json (default 224).
"""
import base64
import io
import json
import os
from pathlib import Path

from PIL import Image

MODELS_DIR = Path(__file__).resolve().parent.parent / "models"
MODEL_PATH = MODELS_DIR / "incident_model.onnx"
LABELS_PATH = MODELS_DIR / "labels.json"

# Maps a model class to the app's alert category + the form's type value.
# Keys are compared case-insensitively (see _map_class), so AIDER folder names
# like "Fire" / "Collapsed_Building" / "traffic_incident" all resolve.
CLASS_TO_ALERT = {
    "fire":               {"alert_type": "disaster", "sub_type": "fire"},
    "flood":              {"alert_type": "disaster", "sub_type": "flood"},
    "flooded_areas":      {"alert_type": "disaster", "sub_type": "flood"},
    "flooding":           {"alert_type": "disaster", "sub_type": "flood"},
    "collapsed_building": {"alert_type": "disaster", "sub_type": "landslide"},
    "collapsed_buildings":{"alert_type": "disaster", "sub_type": "landslide"},
    "landslide":          {"alert_type": "disaster", "sub_type": "landslide"},
    "earthquake":         {"alert_type": "disaster", "sub_type": "earthquake"},
    "traffic_incident":   {"alert_type": "traffic",  "sub_type": "accident"},
    "traffic_accident":   {"alert_type": "traffic",  "sub_type": "accident"},
    "traffic":            {"alert_type": "traffic",  "sub_type": "accident"},
    "normal":             {"alert_type": None,       "sub_type": None},
    "normal_image":       {"alert_type": None,       "sub_type": None},
    "none":               {"alert_type": None,       "sub_type": None},
}


def _map_class(name: str) -> dict:
    """Case-insensitive lookup; unknown classes are treated as 'not an incident'."""
    return CLASS_TO_ALERT.get(name.strip().lower(),
                              {"alert_type": None, "sub_type": None})

# Confidence floor. A prediction below this is treated as "not confident":
# the app should ask the user to confirm/pick the type rather than auto-routing.
# Override with the VISION_CONFIDENCE_THRESHOLD env var.
CONFIDENCE_THRESHOLD = float(os.getenv("VISION_CONFIDENCE_THRESHOLD", "0.65"))

# The gap between the top two scores must be at least this much, otherwise the
# call is "ambiguous" (two classes almost tied) → also not confident. This
# catches confident-looking-but-borderline cases a single threshold misses.
MIN_MARGIN = float(os.getenv("VISION_MIN_MARGIN", "0.20"))

_session = None
_labels: list[str] = []
_img_size = 224


def _load_model():
    """Load the ONNX session once, on first use. Returns True if available."""
    global _session, _labels, _img_size
    if _session is not None:
        return True
    if not (MODEL_PATH.exists() and LABELS_PATH.exists()):
        return False
    import onnxruntime as ort
    meta = json.loads(LABELS_PATH.read_text())
    _labels = meta["labels"]
    _img_size = int(meta.get("img_size", 224))
    _session = ort.InferenceSession(str(MODEL_PATH),
                                    providers=["CPUExecutionProvider"])
    print(f"[VISION] Loaded {MODEL_PATH.name} ({len(_labels)} classes: {_labels})")
    return True


def _decode_image(image_data: str) -> Image.Image:
    """Accepts a base64 data URI (or bare base64) and returns an RGB PIL image."""
    if "," in image_data and image_data.strip().startswith("data:"):
        image_data = image_data.split(",", 1)[1]
    raw = base64.b64decode(image_data)
    return Image.open(io.BytesIO(raw)).convert("RGB")


def classify_image(image_data: str) -> dict:
    """
    Classify one incident photo.

    Returns:
      {
        "engine": "onnx" | "placeholder",
        "predicted": "<class>",
        "confidence": 0.0-1.0,
        "reliable": bool,               # confidence >= threshold
        "alert_type": "traffic" | "disaster" | None,   # None => not an incident
        "sub_type": "accident" | "fire" | ... | None,
        "all_scores": {class: score, ...},
      }
    """
    img = _decode_image(image_data)  # validates the payload in both paths

    if not _load_model():
        # Placeholder — lets the end-to-end flow work before training is done.
        return {
            "engine": "placeholder",
            "predicted": "traffic_incident",
            "confidence": 0.90,
            "reliable": True,
            "alert_type": "traffic",
            "sub_type": "accident",
            "all_scores": {"traffic_incident": 0.90, "normal": 0.10},
            "note": "No trained model installed — drop incident_model.onnx "
                    "and labels.json into backend/models/ to enable real inference.",
        }

    import numpy as np
    img = img.resize((_img_size, _img_size))
    # Raw 0-255 float32 — the exported graph contains its own preprocessing.
    x = np.asarray(img, dtype=np.float32)[None, ...]

    input_name = _session.get_inputs()[0].name
    scores = _session.run(None, {input_name: x})[0][0]

    order = list(np.argsort(scores)[::-1])
    best = int(order[0])
    predicted = _labels[best]
    confidence = float(scores[best])
    runner_up = float(scores[int(order[1])]) if len(order) > 1 else 0.0
    margin = confidence - runner_up
    mapping = _map_class(predicted)

    # Confidence floor: pass only if the top score clears the threshold AND is
    # clearly ahead of the second guess. Otherwise it's "not confident" and the
    # app routes the user to manual selection instead of auto-filling a report.
    confident = (confidence >= CONFIDENCE_THRESHOLD) and (margin >= MIN_MARGIN)

    return {
        "engine": "onnx",
        "predicted": predicted,
        "confidence": round(confidence, 4),
        "margin": round(margin, 4),
        "reliable": confident,           # kept for backward compatibility
        "confident": confident,
        # When not confident we suppress the routing target so the app won't
        # auto-open a (likely wrong) report; the guess is still returned as a hint.
        "alert_type": mapping["alert_type"] if confident else None,
        "sub_type": mapping["sub_type"] if confident else None,
        "suggested_type": mapping["alert_type"],  # the raw guess, for the hint
        "all_scores": {label: round(float(s), 4) for label, s in zip(_labels, scores)},
    }
