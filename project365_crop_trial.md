# Local crop trial

This standalone experiment samples up to 50 linked photos from the live Crop queue. It creates local JPEG previews, Apple Vision observations and aesthetic scores, optional local Ollama comparisons, `results.json`, and `gallery.html`. It never calls Crop save/commit endpoints or changes the canonical database, staging, or original files.

## Run

From the Project365 checkout, with the Control app and Ollama running:

```sh
python3 project365_crop_trial.py --count 50 --model qwen3.5:latest
```

The command compiles `project365_crop_trial_vision.swift` with Xcode's Swift compiler on first use. ImageMagick (`magick`) is also required. The private gallery is written under `Project365Canonical/exports/crop_trial_<timestamp>/gallery.html`; the directory is created with owner-only access. Progress is printed after each image. `--no-vlm` runs the Vision-only comparison when Ollama is unavailable. `--output /absolute/path` chooses another new output directory.

The sample is deterministic for a given queue state. Each gallery row shows the original preview, a centered square, a subject-positioned square, and a straightened square. Option positions are shuffled per photo to test for model position bias. The green outline indicates the local model's advisory choice. Mark your own verdict under each row; **Download my verdicts** saves your feedback as a local JSON file. Open `results.json` for every candidate's proxy-pixel crop geometry, angle, Vision score, subject-cut penalty, and model timing. These coordinates are trial data and are not ready for direct import into the Crop tool.

To compare another installed vision model on exactly the same previews without regenerating crops:

```sh
python3 project365_crop_trial.py --model qwen3.8:latest --rerank-existing Project365Canonical/exports/crop_trial_<timestamp>
```

The rerank preserves the first model response in `first_model_review` and updates the gallery's current model choice. It submits the three preview images separately, which avoids the extreme middle-panel bias seen when they were presented as one panorama. The model still shows order sensitivity; its choice is not an automatic crop decision.

## What this tests

- Apple Vision supplies face, person, object, attention, and horizon observations.
- The trial renders fit-within-bounds square crops at zero and small positive/negative angles, then scores the rendered previews with Apple's image aesthetics model.
- The installed Qwen model compares three rendered alternatives for composition and subject preservation. Its choice is advisory; no crop is saved.

The Vision aesthetics score measures general image appeal, not this archive's specific framing preferences. Horizon observations can be wrong or absent. The gallery is the quality test: note which photos need a manual crop, a different rotation, or no square crop at all before considering a bulk workflow.

## Import reviewed choices

`project365_import_crop_trial.py` converts approved gallery choices to full-resolution
crop coordinates and stages them as ordinary Crop estimates. It imports
`model_good` and explicit A/B/C choices, and leaves `needs_manual` photos alone.
It verifies each source photo against the trial proxy before changing staging.
Run it only while the Project365 estimate worker is stopped:

```sh
python3 project365_import_crop_trial.py Project365Canonical/exports/crop_trial_20260922_174458
python3 project365_import_crop_trial.py Project365Canonical/exports/crop_trial_20260922_174458 --apply
```

The first command checks the 50-photo result without writing. The second skips
photos that already have a saved crop. Imported crops appear in Crop under
**Saved estimates → Ready batch** and still require individual review and
explicit commit.

## Model choice

[ProCrop](https://huggingface.co/BWGZK/ProCrop) provides a downloadable crop-specific checkpoint, but its [single-image inference instructions](https://github.com/BWGZK-keke/ProCrop#step-4-single-image-inference) require retrieval tables and professional reference embeddings. It is worth evaluating later if this simple pilot misses enough cases to justify that setup.

[Qwen3.8 27B](https://ollama.com/library/qwen3.8) is image-capable and fits this Mac's 32 GB memory more plausibly than the 105 GB Qwen3.8-Flash-Next build. It is not verified to rank square crops better. The `--model` flag allows a same-sample comparison after it is downloaded locally.
