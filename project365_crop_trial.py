#!/usr/bin/env python3
"""Read-only 50-photo crop trial; writes only local previews and a report."""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import html
import json
import math
import os
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


QUEUE_URL = "http://127.0.0.1:8766/crop/api/crop-entries?crop_filter=missing"
OLLAMA_URL = "http://127.0.0.1:11434/api/chat"
VISION_BINARY = Path("/tmp/project365-crop-trial-vision")


def read_json(url: str, data: dict | None = None, timeout: int = 30) -> dict:
    payload = json.dumps(data).encode() if data is not None else None
    request = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def run(*args: str) -> str:
    result = subprocess.run(args, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def ensure_vision_binary() -> None:
    source = Path(__file__).with_name("project365_crop_trial_vision.swift")
    if VISION_BINARY.is_file() and VISION_BINARY.stat().st_mtime_ns >= source.stat().st_mtime_ns:
        return
    run("xcrun", "swiftc", "-O", str(source), "-o", str(VISION_BINARY))


def proxy_image(source: Path, output: Path) -> tuple[int, int]:
    run("magick", str(source) + "[0]", "-auto-orient", "-resize", "1200x1200>",
        "-strip", "-quality", "88", str(output))
    width, height = map(int, run("magick", "identify", "-format", "%w %h", str(output)).split())
    return width, height


def analyze(proxy: Path) -> dict:
    return json.loads(run(str(VISION_BINARY), str(proxy)))


def box_center(box: dict, width: int, height: int) -> tuple[float, float]:
    return ((box["x"] + box["width"] / 2) * width,
            (box["y"] + box["height"] / 2) * height)


def target_centers(vision: dict, width: int, height: int) -> list[tuple[str, float, float]]:
    centers = [("center", width / 2, height / 2)]
    for name, key in (("faces", "faces"), ("people", "humans"),
                      ("objects", "objects"), ("attention", "attention")):
        boxes = [box for box in vision.get(key, []) if box.get("confidence", 0) >= 0.25]
        if not boxes:
            continue
        weight = sum(box.get("confidence", 1) for box in boxes)
        x = sum(box_center(box, width, height)[0] * box.get("confidence", 1) for box in boxes) / weight
        y = sum(box_center(box, width, height)[1] * box.get("confidence", 1) for box in boxes) / weight
        centers.append((name, x, y))
    return centers


def rotated_point(x: float, y: float, width: int, height: int, angle: float) -> tuple[float, float]:
    radians = math.radians(angle)
    dx, dy = x - width / 2, y - height / 2
    return (width / 2 + dx * math.cos(radians) - dy * math.sin(radians),
            height / 2 + dx * math.sin(radians) + dy * math.cos(radians))


def crop_fits(x: int, y: int, size: int, width: int, height: int, angle: float) -> bool:
    if angle == 0:
        return 0 <= x and 0 <= y and x + size <= width and y + size <= height
    for px, py in ((x, y), (x + size, y), (x, y + size), (x + size, y + size)):
        sx, sy = rotated_point(px, py, width, height, -angle)
        if sx < 1 or sy < 1 or sx > width - 1 or sy > height - 1:
            return False
    return True


def crop_at(cent_x: float, cent_y: float, width: int, height: int, angle: float) -> tuple[int, int, int]:
    short = min(width, height)
    size = short if angle == 0 else math.floor(short * 0.93)
    max_x, max_y = width - size, height - size
    desired_x = min(max(round(cent_x - size / 2), 0), max_x)
    desired_y = min(max(round(cent_y - size / 2), 0), max_y)
    if crop_fits(desired_x, desired_y, size, width, height, angle):
        return desired_x, desired_y, size
    # Find the closest valid square on a coarse grid; no filled corners are allowed.
    steps_x = sorted(set([0, max_x, desired_x] + list(range(0, max_x + 1, 8))))
    steps_y = sorted(set([0, max_y, desired_y] + list(range(0, max_y + 1, 8))))
    valid = [(x, y) for y in steps_y for x in steps_x
             if crop_fits(x, y, size, width, height, angle)]
    if not valid:
        raise ValueError("No fill-free square at this angle")
    x, y = min(valid, key=lambda point: (point[0] - desired_x) ** 2 + (point[1] - desired_y) ** 2)
    return x, y, size


def render_crop(proxy: Path, output: Path, width: int, height: int,
                x: int, y: int, size: int, angle: float) -> None:
    command = ["magick", str(proxy), "-background", "black", "-virtual-pixel", "black"]
    if angle:
        command += ["-define", f"distort:viewport={width}x{height}+0+0",
                    "-distort", "SRT", str(angle), "+repage"]
    command += ["-crop", f"{size}x{size}+{x}+{y}", "+repage",
                "-resize", "512x512!", "-quality", "88", str(output)]
    run(*command)


def subject_penalty(candidate: dict, vision: dict, width: int, height: int) -> float:
    penalty = 0.0
    x, y, size, angle = (candidate[key] for key in ("x", "y", "size", "angle"))
    for key, weight in (("faces", 0.6), ("humans", 0.2)):
        for box in vision.get(key, []):
            bx = box["x"] * width
            by = box["y"] * height
            bw = box["width"] * width
            bh = box["height"] * height
            corners = [rotated_point(px, py, width, height, angle)
                       for px, py in ((bx, by), (bx + bw, by), (bx, by + bh), (bx + bw, by + bh))]
            if any(px < x + size * .02 or px > x + size * .98 or
                   py < y + size * .02 or py > y + size * .98 for px, py in corners):
                penalty += weight * box.get("confidence", 1)
    return penalty


def proposed_angles(vision: dict) -> list[float]:
    angles = [0.0, -1.5, 1.5]
    horizon = vision.get("horizon_degrees")
    if isinstance(horizon, (int, float)) and 0.4 <= abs(horizon) <= 3:
        # Vision's angle direction can vary with image orientation; score both signs.
        angles += [round(horizon, 2), round(-horizon, 2)]
    return list(dict.fromkeys(angles))


def candidates_for(proxy: Path, folder: Path, vision: dict,
                   width: int, height: int) -> list[dict]:
    candidates = []
    seen = set()
    for name, cx, cy in target_centers(vision, width, height):
        for angle in proposed_angles(vision):
            try:
                x, y, size = crop_at(cx, cy, width, height, angle)
            except ValueError:
                continue
            key = (x, y, size, angle)
            if key in seen:
                continue
            seen.add(key)
            output = folder / f"option_{len(candidates):02d}.jpg"
            render_crop(proxy, output, width, height, x, y, size, angle)
            candidates.append({"name": name, "x": x, "y": y, "size": size,
                               "angle": angle, "image": output.name})
    return candidates


def score_candidates(folder: Path, candidates: list[dict], vision: dict,
                     width: int, height: int) -> None:
    paths = [str(folder / candidate["image"]) for candidate in candidates]
    scores = json.loads(run(str(VISION_BINARY), "score", *paths))
    for candidate, score in zip(candidates, scores, strict=True):
        candidate["aesthetic_score"] = score.get("score")
        candidate["subject_penalty"] = subject_penalty(candidate, vision, width, height)
        raw = score.get("score")
        candidate["rank_score"] = round(raw - candidate["subject_penalty"], 4) if raw is not None else None


def shortlist(candidates: list[dict]) -> list[dict]:
    baseline = next(candidate for candidate in candidates if candidate["name"] == "center" and candidate["angle"] == 0)
    ranked = sorted((candidate for candidate in candidates if candidate["rank_score"] is not None),
                    key=lambda candidate: candidate["rank_score"], reverse=True)
    choices = [baseline]
    positioned = next((candidate for candidate in ranked
                       if candidate["angle"] == 0 and candidate != baseline), None)
    straightened = next((candidate for candidate in ranked if candidate["angle"] != 0), None)
    for candidate in (positioned, straightened, *ranked):
        if candidate is not None and candidate not in choices:
            choices.append(candidate)
        if len(choices) == 3:
            break
    return choices


def model_choice(folder: Path, choices: list[dict], model: str) -> dict:
    if len(choices) < 2:
        return {"status": "skipped", "reason": "Only one candidate"}
    montage = folder / "comparison.jpg"
    labels = "ABC"
    args = ["magick"]
    for choice in choices:
        args.append(str(folder / choice["image"]))
    args += ["+append", str(montage)]
    run(*args)
    prompt = ("You are receiving three separate images of the same photo. Image A is first, B is second, C is third. "
              "Choose the best finished composition. Prefer intact people and meaningful subjects, balanced framing, "
              "straight horizons or architecture, and no blank corners. A subtle 1-2 degree correction may help. "
              "Reply only as JSON with choice (A, B, or C) and a short reason.")
    images = [base64.b64encode((folder / choice["image"]).read_bytes()).decode() for choice in choices]
    request = {"model": model, "messages": [{"role": "user", "content": prompt,
              "images": images}],
              "stream": False, "format": "json", "think": False,
              "options": {"temperature": 0, "num_predict": 140}}
    started = time.monotonic()
    response = read_json(OLLAMA_URL, request, timeout=180)
    content = response.get("message", {}).get("content", "")
    try:
        answer = json.loads(content)
    except json.JSONDecodeError:
        # Ollama may truncate a long explanation after a valid choice field.
        match = re.search(r'"choice"\s*:\s*"([ABC])"', content)
        answer = {"choice": match.group(1) if match else "", "reason": "Truncated model response"}
    choice = str(answer.get("choice", "")).strip().upper()
    if choice not in labels[:len(choices)]:
        choice = ""
    return {"status": "ok" if choice else "invalid", "choice": choice,
            "reason": str(answer.get("reason", ""))[:160],
            "seconds": round(time.monotonic() - started, 2), "model": model,
            "presentation": "separate_images"}


def report_html(records: list[dict], model: str) -> str:
    cards = []
    for record in records:
        if record.get("error"):
            cards.append(f'<article><h2>#{record["index"]} — failed</h2><p>{html.escape(record["error"])}</p></article>')
            continue
        folder = f'{record["index"]:02d}'
        choice = record.get("model_review", {}).get("choice", "")
        vision_best = max(record["shortlist"], key=lambda item: item["rank_score"]
                          if item.get("rank_score") is not None else -10)
        options = []
        for label, candidate in zip("ABC", record["shortlist"], strict=False):
            selected = ' class="selected"' if label == choice else ""
            vision_label = " · Vision pick" if candidate is vision_best else ""
            options.append(f'<figure{selected}><img loading="lazy" src="{folder}/{candidate["image"]}">'
                           f'<figcaption>{label}: {html.escape(candidate["name"])} · {candidate["angle"]}° · '
                           f'Vision {candidate["aesthetic_score"]:.3f}{vision_label}</figcaption></figure>')
        model_text = record.get("model_review", {})
        verdict = (f'Experimental model: {html.escape(choice or "no choice")} · '
                   f'{html.escape(model_text.get("reason", model_text.get("status", "")))}')
        cards.append(f'<article data-index="{record["index"]}" data-entry="{html.escape(record["entry_id"], quote=True)}"><h2>#{record["index"]} · {html.escape(record["date"])} · '
                     f'{html.escape(record["orientation"])}</h2>'
                     f'<p>{verdict}</p><div class="grid"><figure><img loading="lazy" src="{folder}/original.jpg">'
                     f'<figcaption>Original preview</figcaption></figure>{"".join(options)}</div>'
                     '<div class="feedback"><label>Your verdict: <select><option value="">Unreviewed</option>'
                     '<option value="model_good">Model choice is good</option><option value="a_better">A is better</option>'
                     '<option value="b_better">B is better</option><option value="c_better">C is better</option>'
                     '<option value="needs_manual">Needs manual crop/straightening</option></select></label>'
                     '<input type="text" placeholder="Optional note" aria-label="Optional note"></div></article>')
    return ("<!doctype html><html><head><meta charset='utf-8'><title>Project365 crop trial</title>"
            "<style>body{font:16px system-ui;background:#181818;color:#eee;margin:2rem}article{border-top:1px solid #555;padding:1.5rem 0}"
            ".grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:1rem}figure{margin:0}img{width:100%;height:320px;object-fit:contain;background:#080808}"
            "figcaption{padding:.4rem}.selected{outline:3px solid #7ed18b}p{color:#ccc}.feedback{display:flex;gap:1rem;margin-top:1rem}"
            ".feedback select,.feedback input{font:inherit;padding:.4rem}.feedback input{flex:1}@media(max-width:900px){.grid{grid-template-columns:repeat(2,1fr)}}"
            "</style></head><body><h1>Project365: read-only 50-photo crop trial</h1>"
            f"<p>Local model: {html.escape(model)}. Green shows its experimental choice; this model showed position bias, so judge each result yourself. “Vision pick” uses the native score with subject protection. The Crop queue and database were not changed.</p>"
            '<button type="button" onclick="downloadFeedback()">Download my verdicts</button>'
            + "".join(cards) + """<script>
const storageKey = 'project365-crop-trial-feedback:' + location.pathname;
let feedback = JSON.parse(localStorage.getItem(storageKey) || '{}');
document.querySelectorAll('article[data-index]').forEach(article => {
  const key = article.dataset.index;
  const select = article.querySelector('select');
  const note = article.querySelector('input');
  select.value = feedback[key]?.verdict || '';
  note.value = feedback[key]?.note || '';
  const save = () => {
    feedback[key] = {entry_id: article.dataset.entry, verdict: select.value, note: note.value};
    localStorage.setItem(storageKey, JSON.stringify(feedback));
  };
  select.addEventListener('change', save);
  note.addEventListener('change', save);
});
function downloadFeedback() {
  const blob = new Blob([JSON.stringify(feedback, null, 2)], {type: 'application/json'});
  const link = document.createElement('a');
  link.href = URL.createObjectURL(blob);
  link.download = 'crop_trial_feedback.json';
  link.click();
  URL.revokeObjectURL(link.href);
}
</script></body></html>""")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=50)
    parser.add_argument("--model", default="qwen3.5:latest")
    parser.add_argument("--no-vlm", action="store_true", help="Score with local Apple Vision only")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--rerank-existing", type=Path,
                        help="Reassess the existing gallery using separate model images")
    args = parser.parse_args()
    if args.count < 1 or args.count > 50:
        parser.error("count must be between 1 and 50")
    if args.rerank_existing and args.no_vlm:
        parser.error("--rerank-existing requires a local vision model")
    if not args.no_vlm:
        try:
            tags = read_json("http://127.0.0.1:11434/api/tags", timeout=5)
        except (OSError, urllib.error.URLError) as exc:
            parser.error(f"start local Ollama first: {exc}")
        if args.model not in {item.get("name") for item in tags.get("models", [])}:
            parser.error(f"local Ollama model {args.model!r} is not installed")
    if args.rerank_existing:
        folder = args.rerank_existing
        records = json.loads((folder / "results.json").read_text())
        for record in records:
            if record.get("error") or not record.get("shortlist"):
                continue
            if "first_model_review" not in record:
                record["first_model_review"] = {
                    **record.get("model_review", {}), "presentation": "panorama"}
            try:
                record["model_review"] = model_choice(
                    folder / f'{record["index"]:02d}', record["shortlist"], args.model)
            except (OSError, urllib.error.URLError, subprocess.CalledProcessError, TimeoutError) as exc:
                record["model_review"] = {"status": "failed", "reason": str(exc)[:180],
                                          "model": args.model, "presentation": "separate_images"}
            (folder / "results.json").write_text(json.dumps(records, indent=2))
            (folder / "gallery.html").write_text(report_html(records, args.model))
            print(f'reranked {record["index"]}/{len(records)} '
                  f'{record["model_review"]["status"]}', flush=True)
        return 0
    ensure_vision_binary()
    output = args.output or Path("Project365Canonical/exports") / (
        "crop_trial_" + dt.datetime.now().strftime("%Y%m%d_%H%M%S"))
    output.mkdir(parents=True, exist_ok=False)
    os.chmod(output, 0o700)
    queue = read_json(QUEUE_URL)["entries"]
    eligible = [entry for entry in queue if entry.get("candidate_role") == "external_original_associated_photo"
                and Path(entry.get("candidate_path", "")).is_file()]
    sample = random.Random(20260922).sample(eligible, min(args.count, len(eligible)))
    records = []
    started = time.monotonic()
    for index, entry in enumerate(sample, 1):
        folder = output / f"{index:02d}"
        folder.mkdir()
        record = {"index": index, "entry_id": entry["entry_id"],
                  "candidate_path": entry["candidate_path"], "date": entry.get("entry_date", "")}
        try:
            proxy = folder / "original.jpg"
            width, height = proxy_image(Path(entry["candidate_path"]), proxy)
            record["proxy_dimensions"] = [width, height]
            record["orientation"] = "landscape" if width > height else "portrait" if height > width else "square"
            vision = analyze(proxy)
            record["vision"] = vision
            candidates = candidates_for(proxy, folder, vision, width, height)
            score_candidates(folder, candidates, vision, width, height)
            record["candidates"] = candidates
            record["shortlist"] = shortlist(candidates)
            random.Random(20260922 + index).shuffle(record["shortlist"])
            if not args.no_vlm:
                try:
                    record["model_review"] = model_choice(folder, record["shortlist"], args.model)
                except (OSError, urllib.error.URLError, subprocess.CalledProcessError, TimeoutError) as exc:
                    record["model_review"] = {"status": "failed", "reason": str(exc)[:180]}
            else:
                record["model_review"] = {"status": "skipped"}
        except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
            record["error"] = str(exc)[:300]
        records.append(record)
        (output / "results.json").write_text(json.dumps(records, indent=2))
        (output / "gallery.html").write_text(report_html(records, args.model))
        print(f'{index}/{len(sample)} {record.get("orientation", "error")} '
              f'{record.get("model_review", {}).get("status", "error")}', flush=True)
    print(json.dumps({"output": str(output.resolve()), "processed": len(records),
                      "succeeded": sum("error" not in record for record in records),
                      "seconds": round(time.monotonic() - started, 2)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
