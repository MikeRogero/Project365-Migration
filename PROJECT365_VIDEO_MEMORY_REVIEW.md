# Project365 Video Memory Review

## 1. Scope & assumptions

This is a standalone, local review tool for creating memory-triggering stills
from indexed videos. It does not modify source videos or write `canonical.db`.

The first pilot is intentionally limited to eight exact-hash-unique videos
spanning capture eras, durations, resolutions, codecs, and rotation metadata.
Each video receives sixteen evenly spaced 640-pixel JPEG previews.

## 2. Problem model

The stages are deliberately separate:

1. Discover eight videos from the existing sidecar indexes.
2. Extract sixteen lightweight JPEG previews per video.
3. Score all previews on-device with Apple Vision.
4. Select one key frame and up to three time-diverse keep frames from those
   scores, with a deterministic middle-frame fallback if Vision cannot score.
5. Review one video at a time in a pending/accepted/rejected queue, toggle
   suggestions, and choose a key frame from the player's current position.
6. Optionally capture more frames from the source player or rotate and
   reprocess a video in 90-degree steps.
7. Adjust and save the memory date and mark every frame or individual frames
   private.
8. Accepting a video queues its selected frames without rendering them;
   rejecting removes it from the pending queue and queues nothing.
9. Render full-resolution final JPEGs only after a separate trigger.

Preview state lives in `Project365Canonical/video_memory_review.sqlite`.
Preview files live beneath
`Project365Canonical/cache/video_memory_review`. Final rendered files live
beneath `Project365Canonical/media/video_memory_frames` and have JSON
provenance sidecars. They remain staged and are not canonical diary media.

## 3. Contracts

- Source videos are read-only.
- Review reads never change canonical data.
- Automated rating uses only Apple's on-device Vision framework. The rating
  path has no HTTP model client, cloud API, separate model runtime, or image
  serialization for transport.
- The page returns one video and its screenshot descriptors at a time.
- AI suggestions and user choices are separate fields.
- AI failure leaves every frame manually reviewable.
- `P All` sets or clears privacy for every screenshot from one video; each
  screenshot also has its own `P` control. Privacy is independent of selection.
- Privacy survives preview regeneration and is copied into final JPEG JSON
  provenance so a later canonical importer can preserve it.
- One selected frame is the key frame; selected linked frames are unlimited by
  the UI but AI suggestions are capped at four. The selected-frame strip is
  initialized from those AI suggestions and updates without a page reload.
- The source video uses custom controls, stays paused after screenshot seeks,
  and opens at the selected key-frame timestamp.
- `Enter` accepts the current video and advances. Previous/Next and queue
  filters allow accepted or rejected decisions to be revisited.
- Rotation is a persisted relative 0/90/180/270-degree override. It regenerates
  cached previews, preserves the user's selections and privacy, and is applied
  again during final rendering.
- Custom captures reject timestamps within 0.05 seconds of an existing frame
  and are selected by default.
- Queueing final JPEGs does not start rendering.
- Rendering verifies the source byte size and writes through an atomic
  temporary file.
- The web server binds only to loopback.

## 4. Tests

```bash
PYTHONPYCACHEPREFIX=/tmp/project365-pycache \
  python3 -m unittest tests.test_project365_video_memory_review
```

The focused suite covers sampling, the on-device-only AI invariant,
deterministic fallback, queue decisions, date validation, rotation, custom
capture, privacy, explicit render queueing, render processing, capture-era
pilot selection, byte ranges, and single-item lazy-page contracts.

## 5. Usage

Prepare the deterministic pilot. Apple Vision is the only automated rating
engine and does not send thumbnails to a separate model runtime.

```bash
python3 project365_video_memory_review.py prepare-pilot
```

Prepare a larger deterministic batch while excluding videos already ready for
review with Apple Vision results. Incomplete or fallback-scored items are
retried first, then preferred new candidates are spread across capture history.
Unreadable videos or frames without a usable on-device Vision score are
recorded as errors and replaced by later candidates until the requested number
succeeds.

```bash
python3 project365_video_memory_review.py prepare-batch --count 1000
```

Serve the independent review page:

```bash
python3 project365_video_memory_review.py serve
```

Open `http://127.0.0.1:8767/`.

Clicking a frame toggles it without rebuilding the page or changing the
screenshot-pane scroll position. The timestamp seeks the always-visible
left-side player to that moment and leaves it paused. The source fills its
player stage; the timestamp and draggable timeline sit above centered custom
play/pause and ±1/±0.1-second controls. Capture and key actions occupy their
own second control row. `Capture current frame` adds the playhead image to the
choices, while the adjacent `Make key` action makes the playhead frame the
single key frame (capturing it first when needed). The page opens paused at
that key frame. A compact key preview and a row of selected-frame thumbnails
sit below the player and update immediately when a choice changes.

Holding over a screenshot for one second opens a temporary enlarged preview.
The magnifying button pins the enlarged view until its close button, backdrop,
or Escape is used. Blue inner borders identify AI suggestions; wider green
outer borders identify kept frames, red inner borders identify frames captured
manually from the player, and gold remains the key frame. Captured frames are
inserted into both screenshot lists by video timestamp rather than appended at
the end. The small timestamp, zoom, and privacy controls remain beneath each
screenshot. The right screenshot pane has its own persistent vertical scroll.
`↺` and `↻` reprocess the current video with a persisted rotation.

`Accept & Next` (or `Enter`) saves the memory date, moves the video to Accepted,
and queues selected JPGs without processing them. `Reject & Next` moves the
video to Rejected and clears any unrendered jobs for it. Pending, Accepted,
Rejected, and All filters make decisions reversible.

`P All` marks every screenshot from that video private in one click. A purple
`P` on an individual screenshot marks only that frame private. `P Mixed` means
the video contains both private and non-private screenshots.

Rendering can be started from the page or explicitly from the command line:

```bash
python3 project365_video_memory_review.py render --limit 100
```

## 6. Verification notes

JPEG is the pilot default because the installed FFmpeg writes JPEG directly
but has no WebP encoder. WebP would add an ImageMagick encoding stage. WebP can
reduce compressed disk and HTTP bytes, but it does not reduce the decoded
browser bitmap for the same dimensions. Mixed preview formats remain possible
later; older low-resolution videos do not need to be reprocessed merely for
format uniformity.

The original 320-pixel eight-video pilot produced 128 JPEG previews totaling
1,254,186 bytes.
Converting the same previews to WebP quality 75 produced 942,994 bytes (75.2%
of the JPEG size) and added 2.17 seconds of encoding. This is an indicative
storage/timing comparison rather than an equal-perceptual-quality benchmark.
For a loopback, lazy-loaded page, it does not justify reprocessing older JPEG
previews.

The prepared pilot spans capture years 2001–2023, durations from 15 seconds to
91 minutes, and source dimensions from 320×240 through 4K. All 128 frames were
scored by Apple Vision, producing editable suggestions and one key frame per
video. No final JPEGs were queued or rendered at migration time.

## 7. Complexity

The pilot performs 128 sparse frame extractions and one on-device Vision score
per frame. Browser work is bounded to one video and normally sixteen cached
images per page. Candidate images are lazy, the inline video uses metadata
preload without autoplay, and only the right screenshot pane scrolls.

## 8. Adversarial review

- Apple Vision suggestions are advisory and can miss emotionally important but
  technically imperfect frames; the user remains authoritative.
- Vision aesthetics scores are quality-oriented rather than semantic memory
  understanding. Time-diversity avoids adjacent duplicates but cannot infer
  personal significance.
- The loopback web server serves only the local review UI and media. It is not
  part of AI inference.
- Review timestamps are provenance; variable-frame-rate sources may resolve to
  the closest decodable frame.
- Final rendered JPEGs are staged artifacts until a later explicit canonical
  import is designed and verified.
