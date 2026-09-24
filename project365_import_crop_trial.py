"""Import reviewed trial crops as draft estimates in the existing Crop staging."""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from pathlib import Path

from project365_crop_trial import crop_at, crop_fits, proxy_image, run
from project365_original_picker import PickerConfig, PickerState


APPROVED_OPTIONS = {"a_better": "A", "b_better": "B", "c_better": "C"}
BATCH_ID = "crop-trial-20260922-reviewed:1"


def reviewed_choice(record: dict, feedback: dict) -> dict | None:
    if feedback.get("entry_id") != record.get("entry_id"):
        raise ValueError("Trial feedback entry does not match its result")
    verdict = feedback.get("verdict")
    if verdict == "needs_manual":
        return None
    if verdict == "model_good":
        review = record.get("model_review", {})
        if review.get("status") != "ok":
            raise ValueError("Approved model choice is unavailable")
        label = review.get("choice")
    else:
        label = APPROVED_OPTIONS.get(verdict)
    choices = record.get("shortlist", [])
    if label not in ("A", "B", "C") or len(choices) != 3:
        raise ValueError("Trial feedback has no usable crop choice")
    return choices["ABC".index(label)]


def trial_crop(record: dict, choice: dict, folder: Path) -> dict:
    path = Path(record["candidate_path"])
    if not path.is_file():
        raise ValueError("Trial source photo is missing")
    proxy_width, proxy_height = map(int, record["proxy_dimensions"])
    trial_proxy = folder / f'{int(record["index"]):02d}' / "original.jpg"
    with tempfile.TemporaryDirectory(prefix="project365-trial-verify-") as temp_dir:
        current_proxy = Path(temp_dir) / "original.jpg"
        if proxy_image(path, current_proxy) != (proxy_width, proxy_height):
            raise ValueError("Trial source dimensions changed")
        if hashlib.sha256(current_proxy.read_bytes()).digest() != hashlib.sha256(trial_proxy.read_bytes()).digest():
            raise ValueError("Trial source photo changed")
    width, height = map(int, run(
        "magick", str(path) + "[0]", "-auto-orient", "-format", "%w %h", "info:"
    ).split())
    angle = float(choice["angle"])
    center_x = (int(choice["x"]) + int(choice["size"]) / 2) * width / proxy_width
    center_y = (int(choice["y"]) + int(choice["size"]) / 2) * height / proxy_height
    x, y, size = crop_at(center_x, center_y, width, height, angle)
    if not crop_fits(x, y, size, width, height, angle):
        raise ValueError("Trial crop does not fit its source")
    return {"x": x, "y": y, "size": size, "candidate_width": width,
            "candidate_height": height, "rotation_degrees": angle}


def prepare(folder: Path) -> tuple[list[tuple[str, str, dict]], int]:
    records = json.loads((folder / "results.json").read_text())
    feedback = json.loads((folder / "crop_trial_feedback.json").read_text())
    if len(records) != 50 or set(feedback) != {str(i) for i in range(1, 51)}:
        raise ValueError("Expected the complete reviewed 50-photo trial")
    prepared = []
    manual_count = 0
    for record in records:
        choice = reviewed_choice(record, feedback[str(record["index"])])
        if choice is None:
            manual_count += 1
            continue
        prepared.append((record["entry_id"], record["candidate_path"], trial_crop(record, choice, folder)))
    if len({(entry_id, path) for entry_id, path, _ in prepared}) != len(prepared):
        raise ValueError("Duplicate trial crop target")
    return prepared, manual_count


def import_prepared(state: PickerState, prepared: list[tuple[str, str, dict]]) -> tuple[int, int]:
    imported = skipped = 0
    for entry_id, path, crop in prepared:
        if state._stage_missing_crop_estimate(entry_id, path, crop, BATCH_ID, 1):
            imported += 1
        else:
            skipped += 1
    state._mark_estimate_batch_complete(BATCH_ID)
    return imported, skipped


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    parser.add_argument("--apply", action="store_true", help="Stage the reviewed crops after preflight")
    args = parser.parse_args()
    prepared, manual = prepare(args.folder)
    result = {"approved": len(prepared), "manual": manual}
    if args.apply:
        root = Path("Project365Canonical")
        state = PickerState(PickerConfig(
            canonical_root=root,
            queue_path=root / "exports/verification_reports/original_photo_external_search_queue.csv",
        ))
        result["imported"], result["skipped_existing"] = import_prepared(state, prepared)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
