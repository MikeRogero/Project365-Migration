# Project365 Video Memory Review

## 1. Scope & assumptions

This is a standalone, local triage and detailed-review tool for indexed videos.
It does not modify source videos or write `canonical.db`.

The first pilot is intentionally limited to eight exact-hash-unique videos
spanning capture eras, durations, resolutions, codecs, and rotation metadata.
Each video receives sixteen evenly spaced 640-pixel JPEG previews.

## 2. Problem model

The stages are deliberately separate:

1. Rapidly triage prepared videos from existing timestamped previews at
   `/triage`, recording a 0–5 rating and whether detailed review is worthwhile.
2. Discover videos from the existing sidecar indexes.
3. Extract sixteen lightweight JPEG previews per video.
4. Score all previews on-device with Apple Vision.
5. Select one key frame and up to three time-diverse keep frames from those
   scores, with a deterministic middle-frame fallback if Vision cannot score.
6. Review one video at a time in a pending/accepted/rejected queue, toggle
   suggestions, and choose a key frame from the player's current position.
7. Optionally capture more frames from the source player or rotate and
   reprocess a video in 90-degree steps.
8. Adjust and save the memory date and mark every frame or individual frames
   private.
9. Accepting a video queues its selected frames without rendering them;
   rejecting removes it from the pending queue and queues nothing.
10. Render full-resolution final JPEGs only after a separate trigger.
11. Optionally generate an H.264 synopsis on demand. Synopsis generation
    is never part of initial page load or batch preparation.

Preview state lives in `Project365Canonical/video_memory_review.sqlite`.
Preview files live beneath
`Project365Canonical/cache/video_memory_review`. Final rendered files live
beneath `Project365Canonical/media/video_memory_frames` and have JSON
provenance sidecars. They remain staged and are not canonical diary media.
Synopsis derivatives live beneath
`Project365Canonical/media/video_synopses`; each MP4 has a JSON provenance
sidecar recording source identity and sampled moving segments.
Each source video has a Finder-readable folder named from the extensionless
source filename. If that name is duplicated, the source extension is retained;
an immediate-parent label and short stable ID are used only for further
collisions. Frames are numbered densely by video time from `1` through the
selected count, with only the padding needed for that count (`1`, `01`, `001`,
or `0001`). The requested millisecond timestamp, rotation, and `main`/`linked`
role remain visible in each filename.

Discovery shares the archive index's FFmpeg-compatible video allowlist. In
addition to MOV, MP4, and M4V, this includes common legacy containers such as
AVI, MPG/MPEG, WMV/ASF, 3GP, DV, FLV, Matroska/WebM, MTS/M2TS, VOB, RealMedia,
and related QuickTime, MPEG transport/program-stream, and professional formats.
Browser playback still uses the generated H.264 MP4 proxy, so Safari does not
need native support for the source container or codec.

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
  filters allow decisions to be revisited. `Reviewed` contains every video with
  recorded user interaction, regardless of whether it remains pending or was
  accepted or rejected, and initially opens the memory month containing the
  most recently worked item.
- A document reload resets the list to Queued, its default month window, and
  the first video in that window instead of restoring the previously viewed
  video. Ordinary in-page navigation remains continuous.
- Rotation is a persisted relative 0/90/180/270-degree override. It regenerates
  cached previews, preserves the user's selections and privacy, and is applied
  again during final rendering.
- Custom captures reject timestamps within 0.05 seconds of an existing frame,
  stay at least 0.1 seconds inside the video endpoint, and are selected by
  default.
- Queueing final JPEGs does not start rendering.
- Rendering verifies the source byte size and writes through an atomic
  temporary file. A zero-byte seek automatically retries up to 0.5 seconds
  earlier in small steps; provenance keeps both the requested timestamp and
  the actual successful timestamp.
- Render errors are counted per JPG frame, not per video. `View JPG errors`
  lists privacy-safe technical details, opens the affected video at the failed
  timestamp, and can requeue selected failed frames; only the separate render
  button starts output generation.
- The web server binds only to loopback.
- Rapid triage is distinct from detailed-review state. `triage_status` is one
  of `not_triaged`, `no_detail`, `rejected`, `summary_wanted`, `detail_wanted`,
  or `detail_completed`; `rating` is an integer from 0 through 5. Summary marks
  the request and queues the short-video job. Accepting detailed review sets
  `detail_completed`, while rejecting it sets `rejected`.
- Diary-output intent is stored independently from the single workflow status.
  Screenshots, Embedded, and Summary are additive booleans and may all be
  selected for one video. Reject is exclusive: rejecting clears all three, and
  selecting any output reopens a rejected video. Existing screenshot-review
  databases migrate with Screenshots selected unless their prior state was
  explicitly Embed, Summary, or Reject.
- Embedded currently records export intent only. Trimming, blocked-section
  removal, and diary video-attachment packaging remain separate future tools.
- Rapid triage fails closed for privacy by default: a private video default or
  any private frame excludes the entire video from rows, totals, and mutations.
  The explicit `Show private` filter includes private records and authorizes
  triage mutations for that opted-in view.
- The triage list is server-paginated to at most 96 records and defaults to 48.
  Its rows and all child frame descriptors are fetched with two bounded SQL
  queries rather than one query per card.
- Duplicate identity uses the decoded visual fingerprint of 8–16 prepared
  frames, guarded by the exact capture timestamp and a 0.25-second duration
  tolerance. This intentionally recognizes identical videos stored in
  different containers; it is separate from the source-file SHA-256 identity.
- When an untouched duplicate set is found, the earliest queued copy remains
  pending and the others are rejected automatically. If any copy has a human
  edit other than Reject, no copy is changed and the whole set appears together
  under `Problems to resolve`. Choosing `Keep this copy` preserves that copy's
  edits and rejects its siblings transactionally.
- Pointer movement performs no HTTP or database request. The browser maps the
  pointer to the nearest stored frame timestamp, preloads only a visible or
  hovered card's existing previews, and leaves off-screen images lazy.
- Synopsis generation is serialized and on demand. It uses 12–16 sparse
  2-second moving segments, preserves source audio when present, and applies a
  matching 0.2-second video/audio crossfade at each boundary. Sources without
  an audio stream remain silent. Sources shorter than 45 seconds use
  the source/proxy because a synopsis would not provide useful compression;
  Rapid Triage labels this `Use original — already short`.

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

Prepare every indexed video that is not already ready with Apple Vision. Optional
inclusive capture-date bounds accept either `yyyy-mm-dd` or `yyyy-mm`; omitting the
end uses the full start month or the single start day.

```bash
python3 project365_video_memory_review.py prepare-batch --all-missing
python3 project365_video_memory_review.py prepare-batch --all-missing --start-date 2020-02
```

After expanding the accepted source formats, refresh the legacy-video sidecar
before preparing another all-missing batch:

```bash
python3 project365_photo_library_index.py --legacy-video-sidecar
```

Serve the independent review page:

```bash
python3 project365_video_memory_review.py serve
```

Open `http://127.0.0.1:8767/triage` for rapid triage or
`http://127.0.0.1:8767/` for detailed screenshot review. The Control Panel
links to both, with rapid triage as the primary continuation action.

The initial queue can be reconciled from prepared previews with:

```bash
python3 project365_video_memory_review.py prune-duplicates
```

Newly prepared videos are reconciled automatically. Preparation also
fingerprints an older same-time, same-duration candidate when necessary, so a
previously unique queue item can be matched when its later copy arrives.

The triage page supports date, rating, state, duration, text, and sort controls.
Its state and current page are stored in browser local storage. Hover across a
card to scrub its existing timestamped previews; selecting a card updates the
fixed left player without preparing a playback proxy. Pressing `Play` is the
only action that prepares and loads that proxy. The player retains the
decode-aware timeline, step, speed, `Make key`, and `Get screenshot` controls
from detailed review. Captured screenshots are selected references for that
video. The selected-video panel edits title and description; changed text is
saved automatically when either field loses focus, while the Save button remains
available for an explicit save. When no title has been
saved, a leading `yyyy-mm-dd` filename prefix is removed to pre-populate a
suggested title only when a descriptive suffix remains. Date/time-only names,
such as `2000-08-22 014614`, leave the suggestion blank. Saving or overwriting
the field makes the user value authoritative. Cards keep the video edge clear, reserve a fixed metadata area
for time, title, selection, and status, and show a compact `Note` indicator
instead of description text when a description has been saved. Clicking the
displayed card title opens a same-size inline editor; Enter or leaving the field
saves a changed value, while Escape cancels the edit.

`J`/`K` or Up/Down move through the queue, `1`–`5` rate, `D` toggles detailed
review, `S` toggles Summary, `E` toggles Embed (keep the original without
further processing), `R` toggles Reject in place, and `P` toggles privacy for
the video and its screenshots. These are mutually exclusive triage decisions so the
page stays optimized for fast segmentation. Additive Screenshots, Embedded,
and Summary choices remain available in detailed review.
Clicking a card or hovering over its JPG scrubber selects and highlights that
video as the shortcut target. Targeted shortcuts show a small warning when no
video is selected. Clicking an already-selected decision, or
pressing its shortcut again, clears only that decision and returns the video to
Not triaged; ratings and saved metadata do not affect that status. The card
remains in its current position until filters are reapplied or the page is
reloaded. Player shortcuts match detailed
review: Space plays or pauses, Left/Right step 0.1 seconds,
Shift+Left/Right step 1 second, and `.` captures a screenshot. Enter opens
detailed review. Each card's `Play` button selects that video, prepares it when
needed, and starts it from the beginning in the fixed player. Clicking the fixed
player image also prepares and starts an unloaded video, or pauses/resumes a loaded video.
Clearing a decision restores a
rejected video to pending review and intentionally keeps the rating and saved
metadata. Multi-select
supports bounded bulk triage-state changes. No separate tag field was added
because this review database currently has no authoritative tag model.

The `P` control beside the rating stars marks the video and every generated
screenshot private in one operation; pressing it again clears privacy and
removes the private marker from rendered screenshot sidecars. Private or mixed-private videos are
excluded by default. `Show private` explicitly includes them in the triage
results and permits triage actions for that opted-in view.

The `Problems to resolve` state groups duplicate copies next to one another.
Its `Keep this copy` action is the only duplicate-resolution mutation in that
view. The normal private-media filter still applies; enable `Show private` to
include a private member of a problem group.

Selecting `Summary` records the output intent and adds one job to the serialized
background summary queue. The
result is a fast-start H.264/AAC MP4 (maximum 1280 pixels wide, preserved
aspect ratio, no overlays) with source audio when present, short crossfades,
and deterministic source association. This is an
export-capable derivative, but it is not yet inserted into Day One packages:
the current Day One exporter accepts square image derivative roles and needs a
separate video-attachment contract before MP4s can be added safely.

The first time `Play` is pressed for a source, the server prepares its
short-GOP playback proxy in a background job so the browser request remains
responsive. If the job
lasts more than two seconds, the player shows FFmpeg's encoded-time percentage
and elapsed source position until playback is ready. The conversion timeout is
the larger of 20 minutes or six times the source duration, capped at 12 hours;
failures remain visible in the player and can be retried. Reopening a video with
an existing proxy skips preparation.

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
video to Rejected and clears any unrendered jobs for it. Queued, Reviewed,
Accepted, and All lists make decisions reversible. Reviewed includes any video
with a saved user action and defaults to the memory month of the most recently
worked video.

`P All` marks every screenshot from that video private in one click. A purple
`P` on an individual screenshot marks only that frame private. `P Mixed` means
the video contains both private and non-private screenshots.

Rendering can be started from the page or explicitly from the command line:

```bash
python3 project365_video_memory_review.py render --limit 100
```

Move legacy hash-sharded JPG/JSON pairs into the same human-readable layout
without re-encoding them, and update their stored `final_path` values:

```bash
python3 project365_video_memory_review.py migrate-output-names
```

Normal render runs also reconcile already-rendered siblings first, so changing
the selected frame set or crossing a numbering-width boundary keeps the whole
video folder consistently ordered.

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

Preparation performs sixteen sparse frame extractions and one on-device Vision
score per frame. Detailed-review browser work is bounded to one video. Rapid
triage is O(p × f) in the browser for a page of `p <= 48` cards and normally
`f = 16` lightweight frame descriptors; only visible/hovered images decode.
Filtering and sorting scan or use indexes across `n` ready videos and return a
bounded page. Synopsis encoding cost is proportional to the 12–16 sampled
segments rather than the complete source duration. Duplicate reconciliation is
quadratic only within a same-timestamp candidate bucket (`O(g^2 × f)` for `g`
candidate videos and at most `f = 16` tiny fingerprint frames), not across the
full queue.

## 8. Adversarial review

- Apple Vision suggestions are advisory and can miss emotionally important but
  technically imperfect frames; the user remains authoritative.
- Vision aesthetics scores are quality-oriented rather than semantic memory
  understanding. Time-diversity avoids adjacent duplicates but cannot infer
  personal significance.
- The loopback web server serves only the local review UI and media. It is not
  part of AI inference.
- Automatic duplicate rejection deliberately fails open when there are too few
  prepared frames or when the fingerprint cannot be calculated: the videos stay
  in the queue for human review rather than being rejected on weak evidence.
- Review timestamps are provenance; variable-frame-rate sources may resolve to
  the closest decodable frame.
- Final rendered JPEGs are staged artifacts until a later explicit canonical
  import is designed and verified.
- Text search uses a bounded `LIKE` scan over filename/title/description; it is
  adequate for the current 10k-scale library but is the first query to profile
  if the collection grows by an order of magnitude.
- The current Day One exporter is image-specific. Synopsis MP4 creation is
  complete, but automatic Day One attachment remains intentionally deferred.
