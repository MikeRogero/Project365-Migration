"""Local, targetless square-crop estimate for a confirmed linked photo."""

from __future__ import annotations

import base64
import hashlib
import json
import random
import re
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from project365_crop_trial import (
    analyze,
    candidates_for,
    crop_at,
    ensure_vision_binary,
    proxy_image,
    run,
    score_candidates,
    shortlist,
)


MODEL_KEY = "qwen/qwen3-vl-8b@4bit"
MODEL_BASE = "http://127.0.0.1:1234/v1"
MODEL_STATUS_URL = "http://127.0.0.1:1234/api/v1/models"


def loaded_model() -> str:
    """Return the loaded local Qwen model ID, or fail before a long batch starts."""
    try:
        with urllib.request.urlopen(MODEL_STATUS_URL, timeout=5) as response:
            models = json.load(response).get("models", [])
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise ValueError("Start LM Studio's local server and load qwen/qwen3-vl-8b@4bit.") from exc
    for model in models:
        if str(model.get("key", "")) == MODEL_KEY.split("@")[0] and model.get("capabilities", {}).get("vision"):
            for instance in model.get("loaded_instances", []):
                if instance.get("id"):
                    return str(instance["id"])
    raise ValueError("Load qwen/qwen3-vl-8b@4bit in LM Studio before estimating linked photos.")


def _model_choice(folder: Path, choices: list[dict], model: str) -> str:
    prompt = ("These are three alternative square crops of the SAME photo. Choose the most attractive "
              "and appropriate finished crop. Keep meaningful context and all important people or objects; "
              "prefer a level horizon and architecture. Small straightening is useful when it improves the photo. "
              "Reply only as JSON: {\"choice\":\"A\"}, {\"choice\":\"B\"}, or {\"choice\":\"C\"}.")
    parts = [{"type": "text", "text": prompt}]
    for label, choice in zip("ABC", choices, strict=True):
        encoded = base64.b64encode((folder / choice["image"]).read_bytes()).decode("ascii")
        parts.extend((
            {"type": "text", "text": f"Image {label}:"},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}},
        ))
    payload = {"model": model, "messages": [{"role": "user", "content": parts}],
               "temperature": 0, "max_tokens": 100, "stream": False}
    request = urllib.request.Request(
        MODEL_BASE + "/chat/completions", data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=240) as response:
        result = json.load(response)
    content = str(result["choices"][0]["message"].get("content") or "")
    try:
        choice = str(json.loads(content).get("choice", "")).upper()
    except (json.JSONDecodeError, AttributeError):
        match = re.search(r'"choice"\s*:\s*"([ABC])"', content, re.IGNORECASE)
        choice = match.group(1).upper() if match else ""
    return choice if choice in "ABC"[:len(choices)] else ""


def estimate_visual_crop(candidate_path: Path, model: str | None = None) -> dict:
    """Return source-pixel square geometry; no originals or project state are written."""
    if not candidate_path.is_file():
        raise ValueError("Missing linked photo")
    model = model or loaded_model()
    ensure_vision_binary()
    with tempfile.TemporaryDirectory(prefix="project365-visual-crop-") as temporary:
        folder = Path(temporary)
        proxy = folder / "original.jpg"
        width, height = proxy_image(candidate_path, proxy)
        full_width, full_height = map(int, run(
            "magick", str(candidate_path) + "[0]", "-auto-orient", "-format", "%w %h", "info:"
        ).split())
        vision = analyze(proxy)
        candidates = candidates_for(proxy, folder, vision, width, height)
        score_candidates(folder, candidates, vision, width, height)
        choices = shortlist(candidates)
        if not choices:
            raise ValueError("No valid square crop candidates")
        seed = int.from_bytes(hashlib.sha256(str(candidate_path).encode()).digest()[:8], "big")
        random.Random(seed).shuffle(choices)
        label = _model_choice(folder, choices, model) if len(choices) == 3 else ""
        selected = choices["ABC".index(label)] if label else max(
            choices, key=lambda item: item.get("rank_score") if item.get("rank_score") is not None else -10
        )
        center_x = (selected["x"] + selected["size"] / 2) * full_width / width
        center_y = (selected["y"] + selected["size"] / 2) * full_height / height
        x, y, size = crop_at(center_x, center_y, full_width, full_height, selected["angle"])
        return {"x": x, "y": y, "size": size, "candidate_width": full_width,
                "candidate_height": full_height, "rotation_degrees": selected["angle"],
                "_estimate_method": "qwen3-vl" if label else "vision_fallback"}
