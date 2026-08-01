"""
CLIP Service — zero-shot second opinion on incident photos
===========================================================
Uses OpenAI CLIP (ViT-B/32 via open_clip) to score how well a photo matches
text descriptions of each incident class. Needs NO training — it acts as an
independent cross-check ("second witness") for the fine-tuned incident_model,
and doubles as the zero-shot baseline for the thesis evaluation.

Loaded lazily on first use; if open_clip/torch are not installed the service
reports unavailable and the classifier simply runs without fusion.
"""
from PIL import Image

# Prompt ensembles per class — multiple phrasings are averaged, which is the
# standard CLIP zero-shot technique and noticeably more stable than one prompt.
CLASS_PROMPTS: dict[str, list[str]] = {
    "traffic_incident": [
        "a photo of a car accident on a road",
        "a crashed car with visible damage",
        "a traffic collision scene with damaged vehicles",
    ],
    "fire": [
        "a photo of a fire burning with flames and smoke",
        "a burning building on fire",
        "a wildfire with smoke",
    ],
    "flooded_areas": [
        "a photo of a flooded area with water covering the ground",
        "a flooded street with high water",
        "flood water covering roads and land",
    ],
    "collapsed_building": [
        "a photo of a collapsed building with rubble",
        "earthquake damage to a destroyed building",
        "a demolished structure with debris",
    ],
    "normal": [
        "a normal street scene with nothing unusual",
        "a selfie of a person",
        "an ordinary photo of everyday life",
        "an indoor room",
        "a screenshot of text on a screen",
    ],
}

_model = None
_preprocess = None
_text_features = None
_class_names: list[str] = []
_available: bool | None = None


def is_available() -> bool:
    """True if open_clip imports and the model can be loaded."""
    global _available
    if _available is None:
        try:
            import open_clip  # noqa: F401
            _available = True
        except Exception:
            _available = False
    return _available


def _load():
    """Load CLIP once and pre-compute the text embeddings."""
    global _model, _preprocess, _text_features, _class_names
    if _model is not None:
        return
    import torch
    import open_clip

    # "-quickgelu" variant matches the original OpenAI weights' activation —
    # without it open_clip warns of a QuickGELU mismatch and accuracy drops.
    _model, _, _preprocess = open_clip.create_model_and_transforms(
        "ViT-B-32-quickgelu", pretrained="openai")
    _model.eval()
    tokenizer = open_clip.get_tokenizer("ViT-B-32-quickgelu")

    feats = []
    _class_names = list(CLASS_PROMPTS.keys())
    with torch.no_grad():
        for cls in _class_names:
            tokens = tokenizer(CLASS_PROMPTS[cls])
            emb = _model.encode_text(tokens)
            emb = emb / emb.norm(dim=-1, keepdim=True)
            feats.append(emb.mean(dim=0))          # prompt-ensemble average
    _text_features = torch.stack(feats)
    _text_features = _text_features / _text_features.norm(dim=-1, keepdim=True)
    print(f"[CLIP] ViT-B/32 loaded — zero-shot classes: {_class_names}")


def classify(img: Image.Image) -> dict | None:
    """
    Zero-shot classify a PIL image. Returns
      {"predicted": cls, "confidence": float, "all_scores": {...}}
    or None when CLIP is unavailable.
    """
    if not is_available():
        return None
    _load()
    import torch

    x = _preprocess(img.convert("RGB")).unsqueeze(0)
    with torch.no_grad():
        img_feat = _model.encode_image(x)
        img_feat = img_feat / img_feat.norm(dim=-1, keepdim=True)
        # standard CLIP temperature (100) softmax over class similarities
        probs = (100.0 * img_feat @ _text_features.T).softmax(dim=-1)[0]

    scores = {cls: round(float(p), 4) for cls, p in zip(_class_names, probs)}
    best = max(scores, key=scores.get)
    return {"predicted": best, "confidence": scores[best], "all_scores": scores}
