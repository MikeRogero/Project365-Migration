from __future__ import annotations

import json
import hashlib
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import project365_video_memory_review as review


class VideoMemoryReviewTests(unittest.TestCase):
    def _add_duplicate_test_video(
        self,
        store: review.VideoMemoryStore,
        video_id: str,
        fingerprint: bytes | None,
        *,
        capture_timestamp: str = "2008-06-21T01:18:12",
        duration_seconds: float = 23.4,
    ) -> None:
        store.upsert_video(
            {
                "video_id": video_id,
                "source_path": f"/source/{video_id}.mov",
                "source_sha256": hashlib.sha256(video_id.encode()).hexdigest(),
                "source_byte_size": 100,
                "source_mtime_ns": 200,
                "capture_date": capture_timestamp[:10],
                "date_source": "filename_date",
                "capture_timestamp": capture_timestamp,
                "duration_seconds": duration_seconds,
                "status": "ready",
                "preview_format": "jpeg",
            }
        )
        if fingerprint is not None:
            store.save_content_fingerprint(video_id, fingerprint)

    def test_inferred_video_title_uses_only_the_suffix_after_a_leading_date(self) -> None:
        self.assertEqual(
            review.inferred_video_title("2000-02-02 Interview Nicky for TWC.mkv"),
            "Interview Nicky for TWC",
        )
        self.assertEqual(
            review.inferred_video_title("2000-01-13 Jubilee Interview\\.mkv"),
            "Jubilee Interview",
        )
        self.assertEqual(
            review.inferred_video_title("2000-01-13 123456 Evening Walk.mov"),
            "Evening Walk",
        )
        self.assertEqual(
            review.inferred_video_title("2000-08-22 014614.mkv"),
            "",
        )
        self.assertEqual(
            review.inferred_video_title("2000-08-22 01-46-14.mkv"),
            "",
        )
        self.assertEqual(
            review.inferred_video_title("2000-08-22 01.46.14.mkv"),
            "",
        )
        self.assertEqual(review.inferred_video_title("ordinary filename.mov"), "")

    def _run_scrub_controller_scenario(self, scenario: str) -> dict[str, object]:
        mock_player = r"""
class MockPlayer {
  constructor() {
    this._currentTime = 0;
    this.duration = 20;
    this.paused = true;
    this.ended = false;
    this.seeking = false;
    this.assignments = [];
    this.playCalls = 0;
    this.playWhilePlaying = 0;
    this.pauseCalls = 0;
    this.listeners = new Map();
    this.frameCallbacks = new Map();
    this.nextFrameCallback = 1;
  }
  get currentTime() { return this._currentTime; }
  set currentTime(value) {
    this._currentTime = Number(value);
    this.seeking = true;
    this.assignments.push({mode: 'exact', time: this._currentTime});
  }
  fastSeek(value) {
    this._currentTime = Number(value);
    this.seeking = true;
    this.assignments.push({mode: 'fast', time: this._currentTime});
  }
  addEventListener(type, callback) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(callback);
  }
  removeEventListener(type, callback) {
    const callbacks = this.listeners.get(type) || [];
    this.listeners.set(type, callbacks.filter(item => item !== callback));
  }
  dispatch(type) {
    for (const callback of [...(this.listeners.get(type) || [])]) callback({type});
  }
  pause() {
    this.pauseCalls += 1;
    const changed = !this.paused;
    this.paused = true;
    if (changed) this.dispatch('pause');
  }
  play() {
    this.playCalls += 1;
    if (!this.paused) this.playWhilePlaying += 1;
    this.paused = false;
    this.dispatch('play');
    this.dispatch('playing');
    return Promise.resolve();
  }
  requestVideoFrameCallback(callback) {
    const id = this.nextFrameCallback++;
    this.frameCallbacks.set(id, callback);
    return id;
  }
  cancelVideoFrameCallback(id) { this.frameCallbacks.delete(id); }
  completeSeek() {
    this.seeking = false;
    this.dispatch('seeked');
  }
  present(mediaTime) {
    const callbacks = [...this.frameCallbacks.values()];
    this.frameCallbacks.clear();
    for (const callback of callbacks) callback(0, {mediaTime, presentedFrames: 1});
  }
}
const player = new MockPlayer();
const selected = [];
const states = [];
const statuses = [];
let scheduledTimer = null;
const controller = createVideoScrubController(player, {
  onSelectedTime: value => selected.push(value),
  onStateChange: value => states.push(value.phase),
  onStatus: value => statuses.push(value),
  setTimer: callback => { scheduledTimer = callback; return 1; },
  clearTimer: id => { if (id === 1) scheduledTimer = null; },
});
"""
        completed = subprocess.run(
            [
                "node",
                "--input-type=commonjs",
                "-e",
                review.VIDEO_SCRUB_CONTROLLER_JS + mock_player + scenario,
            ],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def test_scrub_controller_keeps_only_the_latest_release_target(self) -> None:
        result = self._run_scrub_controller_scenario(
            r"""
controller.beginScrub();
controller.preview(1);
controller.preview(2);
controller.preview(3);
controller.finishScrub(4);
const beforeCompletion = controller.snapshot();
player.completeSeek();
player.present(1);
const afterSupersededFrame = controller.snapshot();
player.completeSeek();
player.present(4);
console.log(JSON.stringify({
  assignments: player.assignments,
  playCalls: player.playCalls,
  playWhilePlaying: player.playWhilePlaying,
  beforeCompletion,
  afterSupersededFrame,
  final: controller.snapshot(),
}));
"""
        )

        self.assertEqual(
            result["assignments"],
            [{"mode": "fast", "time": 1}, {"mode": "exact", "time": 4}],
        )
        self.assertEqual(result["playCalls"], 1)
        self.assertEqual(result["playWhilePlaying"], 0)
        self.assertEqual(result["beforeCompletion"]["pendingTarget"], 4)
        self.assertNotEqual(result["afterSupersededFrame"]["phase"], "idle")
        self.assertEqual(result["final"]["phase"], "idle")
        self.assertIsNone(result["final"]["activeTarget"])
        self.assertIsNone(result["final"]["pendingTarget"])

    def test_playback_timeline_follower_uses_the_presented_frame_time(self) -> None:
        result = self._run_scrub_controller_scenario(
            r"""
const followed = [];
const follower = createPlaybackTimelineFollower(
  player,
  value => followed.push(value),
);
player.play();
player._currentTime = 3.5;
player.present(3.25);
player._currentTime = 4;
player.pause();
const beforeDestroy = [...followed];
follower.destroy();
player.play();
player._currentTime = 5;
player.present(5);
console.log(JSON.stringify({
  beforeDestroy,
  afterDestroy: followed,
  pendingFrameCallbacks: player.frameCallbacks.size,
}));
"""
        )

        self.assertEqual(result["beforeDestroy"], [0, 3.25, 4])
        self.assertEqual(result["afterDestroy"], [0, 3.25, 4])
        self.assertEqual(result["pendingFrameCallbacks"], 0)

    def test_playback_timeline_follower_uses_animation_frame_fallback(self) -> None:
        result = self._run_scrub_controller_scenario(
            r"""
player.requestVideoFrameCallback = undefined;
player.cancelVideoFrameCallback = undefined;
const followed = [];
let animationCallback = null;
const cancelled = [];
const follower = createPlaybackTimelineFollower(
  player,
  value => followed.push(value),
  {
    requestAnimationFrame: callback => { animationCallback = callback; return 9; },
    cancelAnimationFrame: id => { cancelled.push(id); animationCallback = null; },
  },
);
player.play();
player._currentTime = 2.4;
const frame = animationCallback;
animationCallback = null;
frame();
player._currentTime = 2.5;
player.pause();
console.log(JSON.stringify({followed, cancelled, snapshot: follower.snapshot()}));
"""
        )

        self.assertEqual(result["followed"], [0, 2.4, 2.5])
        self.assertEqual(result["cancelled"], [9])
        self.assertFalse(result["snapshot"]["running"])
        self.assertIsNone(result["snapshot"]["animationFrameId"])

    def test_scrub_controller_waits_for_the_selected_frame_before_playing(self) -> None:
        result = self._run_scrub_controller_scenario(
            r"""
controller.selectExact(7);
controller.togglePlayback();
const beforeSeeked = player.playCalls;
player.completeSeek();
player.present(6.7);
const afterStaleFrame = player.playCalls;
player.present(7);
console.log(JSON.stringify({
  beforeSeeked,
  afterStaleFrame,
  afterSelectedFrame: player.playCalls,
  assignments: player.assignments,
  final: controller.snapshot(),
}));
"""
        )

        self.assertEqual(result["beforeSeeked"], 1)
        self.assertEqual(result["afterStaleFrame"], 1)
        self.assertEqual(result["afterSelectedFrame"], 1)
        self.assertEqual(result["assignments"], [{"mode": "exact", "time": 7}])
        self.assertEqual(result["final"]["phase"], "playing")

    def test_scrub_controller_same_target_release_waits_for_presentation(self) -> None:
        result = self._run_scrub_controller_scenario(
            r"""
controller.beginScrub();
controller.preview(4);
controller.finishScrub(4);
player.completeSeek();
const afterSeeked = controller.snapshot();
player.present(4);
console.log(JSON.stringify({
  assignments: player.assignments,
  afterSeeked,
  final: controller.snapshot(),
}));
"""
        )

        self.assertEqual(
            result["assignments"],
            [{"mode": "fast", "time": 4}, {"mode": "exact", "time": 4}],
        )
        self.assertEqual(result["afterSeeked"]["phase"], "settling")
        self.assertEqual(result["afterSeeked"]["activeTarget"], 4)
        self.assertIsNone(result["afterSeeked"]["pendingTarget"])
        self.assertEqual(result["final"]["phase"], "idle")
        self.assertIsNone(result["final"]["activeTarget"])
        self.assertIsNone(result["final"]["pendingTarget"])

    def test_scrub_controller_watchdog_recovers_without_a_presented_frame(self) -> None:
        result = self._run_scrub_controller_scenario(
            r"""
controller.selectExact(6);
player.completeSeek();
scheduledTimer();
console.log(JSON.stringify({snapshot: controller.snapshot(), statuses}));
"""
        )

        self.assertEqual(result["snapshot"]["phase"], "idle")
        self.assertIsNone(result["snapshot"]["activeTarget"])
        self.assertIsNone(result["snapshot"]["pendingTarget"])
        self.assertFalse(result["snapshot"]["playWhenSettled"])
        self.assertIn("Safari did not present the selected frame", result["statuses"][-1])

    def test_scrub_controller_slow_steps_each_assign_once(self) -> None:
        result = self._run_scrub_controller_scenario(
            r"""
controller.selectExact(5);
player.completeSeek();
player.present(5);
player.assignments = [];
controller.step(0.1);
player.completeSeek();
player.present(5.1);
const afterForward = [...player.assignments];
controller.step(-0.1);
player.completeSeek();
player.present(5);
console.log(JSON.stringify({
  afterForward,
  allAssignments: player.assignments,
  playCalls: player.playCalls,
  playWhilePlaying: player.playWhilePlaying,
  final: controller.snapshot(),
}));
"""
        )

        self.assertEqual(result["afterForward"], [{"mode": "exact", "time": 5.1}])
        self.assertEqual(
            result["allAssignments"],
            [{"mode": "exact", "time": 5.1}, {"mode": "exact", "time": 5}],
        )
        self.assertEqual(result["playCalls"], 3)
        self.assertEqual(result["playWhilePlaying"], 0)
        self.assertEqual(result["final"]["phase"], "idle")

    def test_scrub_controller_steps_from_live_time_after_pause_and_resume(self) -> None:
        result = self._run_scrub_controller_scenario(
            r"""
controller.togglePlayback();
player._currentTime = 5;
controller.togglePlayback();
controller.togglePlayback();
player._currentTime = 12;
player.assignments = [];
controller.step(1);
player.completeSeek();
player.present(13);
console.log(JSON.stringify({
  assignments: player.assignments,
  pauseCalls: player.pauseCalls,
  selected: controller.snapshot().selectedTime,
  currentTime: player.currentTime,
  phase: controller.snapshot().phase,
}));
"""
        )

        self.assertEqual(result["assignments"], [{"mode": "exact", "time": 13}])
        self.assertEqual(result["selected"], 13)
        self.assertEqual(result["currentTime"], 13)
        self.assertEqual(result["phase"], "idle")

    def test_frame_times_are_sixteen_interior_monotonic_samples(self) -> None:
        times = review.frame_times(30.0)

        self.assertEqual(len(times), 16)
        self.assertAlmostEqual(times[0], 30 / 17, places=3)
        self.assertAlmostEqual(times[-1], 30 * 16 / 17, places=3)
        self.assertEqual(times, sorted(times))
        self.assertTrue(all(0 < value < 30 for value in times))

    def test_automatic_preview_frame_retries_slightly_earlier_when_seek_has_no_frame(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mov"
            output = root / "preview.jpg"
            source.write_bytes(b"video")
            seek_times: list[str] = []

            def fake_run(command: list[str], timeout: int = 180) -> subprocess.CompletedProcess[str]:
                seek_times.append(command[command.index("-ss") + 1])
                if len(seek_times) == 2:
                    Path(command[-1]).write_bytes(b"jpeg")
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            with mock.patch.object(review, "_run", side_effect=fake_run):
                resolved_time = review.extract_frame(
                    source,
                    output,
                    2.071,
                    max_pixels=review.PREVIEW_MAX_PIXELS,
                    retry_earlier=True,
                )

            self.assertEqual(seek_times, ["2.071", "2.021"])
            self.assertEqual(resolved_time, 2.021)
            self.assertEqual(output.read_bytes(), b"jpeg")

    def test_exact_frame_capture_does_not_retry_an_empty_seek(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mov"
            output = root / "preview.jpg"
            source.write_bytes(b"video")
            completed = subprocess.CompletedProcess([], 0, stdout="", stderr="")

            with mock.patch.object(review, "_run", return_value=completed) as run:
                with self.assertRaisesRegex(ValueError, "Could not extract video frame"):
                    review.extract_frame(source, output, 2.071, max_pixels=640)

            self.assertEqual(run.call_count, 1)

    def test_frame_retry_can_be_limited_to_half_a_second(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mov"
            output = root / "preview.jpg"
            source.write_bytes(b"video")
            seek_times: list[str] = []

            def fake_run(command: list[str], timeout: int = 180) -> subprocess.CompletedProcess[str]:
                seek_times.append(command[command.index("-ss") + 1])
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            with mock.patch.object(review, "_run", side_effect=fake_run):
                with self.assertRaisesRegex(ValueError, "Could not extract video frame"):
                    review.extract_frame(
                        source,
                        output,
                        2.0,
                        max_pixels=640,
                        retry_earlier=True,
                        retry_earlier_limit_seconds=0.5,
                    )

            self.assertEqual(seek_times, ["2.000", "1.950", "1.900", "1.750", "1.500"])

    def test_prepare_video_keeps_other_samples_when_one_timestamp_cannot_decode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mov"
            source.write_bytes(b"video")
            store = review.VideoMemoryStore(root / "review.sqlite")
            video = {
                "video_id": "a" * 64,
                "source_path": str(source),
                "source_sha256": "a" * 64,
                "source_byte_size": source.stat().st_size,
                "source_mtime_ns": source.stat().st_mtime_ns,
                "capture_date": "2020-01-02",
                "date_source": "filename_date",
                "capture_timestamp": "2020-01-02T03:04:05",
                "duration_seconds": 30.0,
                "media_width": 1920,
                "media_height": 1080,
            }

            def extract(
                _source: Path,
                output: Path,
                time_seconds: float,
                **_kwargs: object,
            ) -> float:
                if output.name == "frame-04.jpg":
                    raise ValueError("No frame at this timestamp")
                output.write_bytes(b"jpeg")
                return time_seconds

            with (
                mock.patch.object(review, "extract_frame", side_effect=extract),
                mock.patch.object(
                    review,
                    "_vision_scores",
                    side_effect=lambda paths: [0.5] * len(paths),
                ),
            ):
                result = review.prepare_video(store, video, root / "cache")

            self.assertEqual(result["frames"], 15)
            page = store.video_page(limit=1, offset=0)
            frames = page["videos"][0]["frames"]
            self.assertEqual(len(frames), 15)
            self.assertNotIn(f"{'a' * 64}:04", {frame["frame_id"] for frame in frames})

    def test_vision_selection_keeps_a_small_time_diverse_subset(self) -> None:
        frames = [
            {
                "frame_id": f"frame-{index:02d}",
                "frame_index": index,
                "time_seconds": float(index),
                "vision_score": score,
            }
            for index, score in enumerate(
                [0.1, 0.91, 0.88, 0.2, 0.74, 0.3, 0.72, 0.1],
                start=1,
            )
        ]

        selection = review.choose_vision_selection(frames, max_keep=3, min_index_gap=2)

        self.assertEqual(selection["main_frame_id"], "frame-02")
        self.assertEqual(selection["selected_frame_ids"], ["frame-02", "frame-05", "frame-07"])
        self.assertEqual(selection["method"], "apple_vision")

    def test_vision_selection_still_keeps_one_when_scores_are_missing(self) -> None:
        frames = [
            {"frame_id": f"frame-{index:02d}", "frame_index": index, "time_seconds": index * 2.0}
            for index in range(1, 17)
        ]

        selection = review.choose_vision_selection(frames)

        self.assertEqual(selection["selected_frame_ids"], ["frame-08"])
        self.assertEqual(selection["main_frame_id"], "frame-08")
        self.assertEqual(selection["method"], "temporal_fallback")

    def test_batch_candidates_exclude_existing_and_prioritize_time_spread(self) -> None:
        candidates = [
            {
                "video_id": f"video-{index:02d}",
                "capture_date": f"{2000 + index:04d}-01-01",
            }
            for index in range(10)
        ]

        ordered = review.select_batch_candidates(
            candidates,
            excluded_video_ids={"video-01", "video-08"},
            count=4,
        )

        self.assertEqual(len(ordered), 8)
        self.assertEqual(len({item["video_id"] for item in ordered}), 8)
        self.assertTrue({"video-01", "video-08"}.isdisjoint(item["video_id"] for item in ordered))
        self.assertEqual(ordered[0]["video_id"], "video-00")
        self.assertEqual(ordered[3]["video_id"], "video-09")

        with self.assertRaises(ValueError):
            review.select_batch_candidates(candidates, excluded_video_ids=set(), count=11)

    def test_normalize_batch_date_scope_accepts_month_day_and_blank_end(self) -> None:
        self.assertEqual(
            review.normalize_batch_date_scope("2020-02", ""),
            ("2020-02-01", "2020-02-29"),
        )
        self.assertEqual(
            review.normalize_batch_date_scope("2020-02-14", ""),
            ("2020-02-14", "2020-02-14"),
        )
        self.assertEqual(
            review.normalize_batch_date_scope("2020-02", "2020-03"),
            ("2020-02-01", "2020-03-31"),
        )
        self.assertEqual(review.normalize_batch_date_scope("", ""), ("", ""))

        with self.assertRaisesRegex(ValueError, "start date"):
            review.normalize_batch_date_scope("", "2020-02")
        with self.assertRaisesRegex(ValueError, "on or before"):
            review.normalize_batch_date_scope("2020-03", "2020-02")
        with self.assertRaisesRegex(ValueError, "yyyy-mm-dd or yyyy-mm"):
            review.normalize_batch_date_scope("2020", "")

    def test_filter_batch_candidates_uses_inclusive_capture_date_scope(self) -> None:
        candidates = [
            {"video_id": "before", "capture_date": "2020-01-31"},
            {"video_id": "first", "capture_date": "2020-02-01"},
            {"video_id": "last", "capture_date": "2020-02-29"},
            {"video_id": "after", "capture_date": "2020-03-01"},
        ]

        filtered = review.filter_batch_candidates_by_date(
            candidates,
            start_date="2020-02",
        )

        self.assertEqual([item["video_id"] for item in filtered], ["first", "last"])

    def test_prepare_all_missing_attempts_every_in_scope_unready_video(self) -> None:
        candidates = [
            {"video_id": "done", "capture_date": "2020-02-01"},
            {"video_id": "new", "capture_date": "2020-02-02"},
            {"video_id": "retry", "capture_date": "2020-02-03"},
            {"video_id": "outside", "capture_date": "2020-03-01"},
        ]
        store = mock.Mock()
        store.completed_ai_video_ids.return_value = {"done"}
        store.incomplete_ai_video_ids.return_value = {"retry"}
        prepared_ids: list[str] = []

        def prepare(_store: object, video: dict[str, object], _cache: Path) -> dict[str, object]:
            prepared_ids.append(str(video["video_id"]))
            return {"frames": 16, "selected": 3, "ai_method": "apple_vision"}

        with (
            mock.patch.object(review, "select_pilot_videos", return_value=candidates),
            mock.patch.object(review, "VideoMemoryStore", return_value=store),
            mock.patch.object(review, "prepare_video", side_effect=prepare),
        ):
            summary = review.prepare_batch(
                Path("/canonical"),
                count=None,
                start_date="2020-02",
            )

        self.assertEqual(prepared_ids, ["retry", "new"])
        self.assertEqual(
            summary,
            {"requested": 2, "prepared": 2, "failed": 0, "attempted": 2},
        )

    def test_video_batch_progress_line_reports_completion_inputs(self) -> None:
        self.assertEqual(
            review.video_batch_progress_line(
                completed=3,
                total=10,
                prepared=2,
                failed=1,
                latest="failed ValueError",
            ),
            "Progress: 3/10 videos · prepared 2 · failed 1 · latest failed ValueError",
        )

    def test_ai_rating_is_apple_vision_only_and_has_no_network_model_client(self) -> None:
        source = Path(review.__file__).read_text()

        self.assertNotIn("import base64", source)
        self.assertNotIn("import http.client", source)
        self.assertNotIn("/v1/chat/completions", source)
        self.assertNotIn("/api/chat", source)
        self.assertNotIn("OLLAMA_", source)
        self.assertNotIn("MODEL_HOST", source)
        self.assertNotIn('add_argument("--ai"', source)
        self.assertNotIn("ai_mode", review.prepare_video.__annotations__)

    def test_standalone_review_links_back_to_control_panel(self) -> None:
        self.assertIn('href="http://127.0.0.1:8766/"', review.REVIEW_HTML)

    def test_byte_ranges_support_normal_and_suffix_requests(self) -> None:
        self.assertEqual(review.parse_byte_range("bytes=2-5", 10), (2, 5))
        self.assertEqual(review.parse_byte_range("bytes=-4", 10), (6, 9))
        self.assertEqual(review.parse_byte_range("bytes=7-", 10), (7, 9))
        with self.assertRaises(ValueError):
            review.parse_byte_range("bytes=20-30", 10)

    def test_scrub_optimized_video_transcodes_to_short_gop_proxy_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mov"
            source.write_bytes(b"source")
            commands: list[list[str]] = []
            timeouts: list[int] = []
            progress: list[float] = []

            def fake_run(
                command: list[str],
                *,
                timeout: int,
                progress_callback: object = None,
            ) -> subprocess.CompletedProcess[str]:
                commands.append(command)
                timeouts.append(timeout)
                if callable(progress_callback):
                    progress_callback(300.0)
                Path(command[-1]).write_bytes(b"compatible")
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            with mock.patch.object(review, "_ffmpeg", return_value="ffmpeg"), mock.patch.object(
                review, "_run_ffmpeg_with_progress", side_effect=fake_run
            ):
                first = review.scrub_optimized_video(
                    source,
                    root / "cache",
                    "video-a",
                    duration_seconds=1800.0,
                    progress_callback=progress.append,
                )
                second = review.scrub_optimized_video(
                    source,
                    root / "cache",
                    "video-a",
                    duration_seconds=1800.0,
                    progress_callback=progress.append,
                )

            self.assertEqual(first, second)
            self.assertEqual(first.read_bytes(), b"compatible")
            self.assertIn("playback-scrub-v1", str(first))
            self.assertEqual([command[0] for command in commands], ["ffmpeg"])
            self.assertEqual(timeouts, [10800])
            self.assertEqual(progress, [300.0, 1800.0, 1800.0])
            command = commands[0]
            self.assertIn("libx264", command)
            self.assertIn("scale='min(960,iw)':-2", command)
            self.assertEqual(command[command.index("-g") + 1], "6")
            self.assertEqual(command[command.index("-keyint_min") + 1], "6")
            self.assertEqual(command[command.index("-progress") + 1], "pipe:1")
            self.assertIn("+faststart", command)

    def test_playback_conversion_timeout_scales_with_duration_and_is_bounded(self) -> None:
        self.assertEqual(review.playback_conversion_timeout_seconds(60.0), 1200)
        self.assertEqual(review.playback_conversion_timeout_seconds(1800.0), 10800)
        self.assertEqual(review.playback_conversion_timeout_seconds(86400.0), 43200)

    def test_playback_preparation_runs_in_background_and_reports_progress(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            state = review.VideoMemoryServerState(root)
            started = threading.Event()
            finish = threading.Event()

            def prepare(
                _source: Path,
                cache_root: Path,
                video_id: str,
                *,
                duration_seconds: float,
                progress_callback: object,
            ) -> Path:
                self.assertEqual(duration_seconds, 200.0)
                self.assertTrue(callable(progress_callback))
                progress_callback(50.0)
                started.set()
                self.assertTrue(finish.wait(5))
                destination = review.playback_proxy_path(cache_root, video_id)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"proxy")
                return destination

            with mock.patch.object(review, "scrub_optimized_video", side_effect=prepare):
                initial = state.start_playback_preparation(source, "video-a", 200.0)
                self.assertIn(initial["status"], {"queued", "preparing"})
                self.assertTrue(started.wait(5))
                running = state.playback_status("video-a", 200.0)
                self.assertEqual(running["status"], "preparing")
                self.assertEqual(running["progress_percent"], 25)
                self.assertEqual(running["processed_seconds"], 50.0)
                finish.set()
                state._playback_threads["video-a"].join(timeout=5)

            ready = state.playback_status("video-a", 200.0)
            self.assertEqual(ready["status"], "ready")
            self.assertEqual(ready["progress_percent"], 100)
            self.assertEqual(state.ready_playback_path("video-a").read_bytes(), b"proxy")

    def test_playback_preparation_timeout_becomes_visible_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            state = review.VideoMemoryServerState(root)

            with mock.patch.object(
                review,
                "scrub_optimized_video",
                side_effect=subprocess.TimeoutExpired(["ffmpeg"], 1200),
            ):
                state.start_playback_preparation(source, "video-timeout", 60.0)
                state._playback_threads["video-timeout"].join(timeout=5)

            status = state.playback_status("video-timeout", 60.0)
            self.assertEqual(status["status"], "error")
            self.assertIn("timed out after 1200 seconds", status["error"])
            self.assertEqual(status["progress_percent"], 0)

    def test_video_month_index_returns_all_counts_and_one_loaded_month(self) -> None:
        entries = [
            {"video_id": "a", "memory_datetime": "2016-10-31 010203"},
            {"video_id": "b", "memory_datetime": "2016-11-01 020304"},
            {"video_id": "b2", "memory_datetime": "2016-11-02 020304"},
            {"video_id": "c", "memory_datetime": "2017-01-02 030405"},
            {"video_id": "d", "memory_datetime": "2017-02-01 040506"},
        ]

        index = review.video_month_index(entries, selected_month="2016-11")

        self.assertEqual(
            index["month_counts"],
            [
                {"month": "2016-10", "count": 1},
                {"month": "2016-11", "count": 2},
                {"month": "2017-01", "count": 1},
                {"month": "2017-02", "count": 1},
            ],
        )
        self.assertEqual(index["selected_month"], "2016-11")
        self.assertEqual([entry["video_id"] for entry in index["entries"]], ["b", "b2"])
        self.assertEqual(index["previous_month"], "2016-10")
        self.assertEqual(index["next_month"], "2017-01")
        with self.assertRaises(ValueError):
            review.video_month_index(entries, selected_month="2016-13")

    def test_initial_memory_datetime_preserves_the_existing_memory_date(self) -> None:
        self.assertEqual(
            review.initial_memory_datetime("2012-05-06T07:08:09", "2010-01-02"),
            "2010-01-02 070809",
        )

    def test_store_persists_ai_defaults_user_toggles_and_explicit_render_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = review.VideoMemoryStore(Path(temporary) / "review.sqlite")
            store.upsert_video(
                {
                    "video_id": "video-a",
                    "source_path": "/source/a.mov",
                    "source_sha256": "a" * 64,
                    "source_byte_size": 100,
                    "source_mtime_ns": 200,
                    "capture_date": "2012-05-06",
                    "date_source": "filename_date",
                    "capture_timestamp": "2012-05-06T07:08:09",
                    "duration_seconds": 30.0,
                    "status": "ready",
                    "preview_format": "jpeg",
                }
            )
            store.replace_frames(
                "video-a",
                [
                    {
                        "frame_id": f"video-a:{index:02d}",
                        "frame_index": index,
                        "time_seconds": float(index),
                        "preview_path": f"/preview/{index}.jpg",
                        "preview_bytes": 20,
                        "vision_score": 0.1 * index,
                    }
                    for index in range(1, 5)
                ],
            )
            store.save_ai_selection(
                "video-a",
                ["video-a:02", "video-a:04"],
                "video-a:04",
                method="apple_vision",
                model="VNCalculateImageAestheticsScoresRequest",
            )

            page = store.video_page(limit=8, offset=0)
            self.assertEqual(page["videos"][0]["memory_datetime"], "2012-05-06 070809")
            self.assertEqual(page["videos"][0]["title"], "")
            self.assertEqual(page["videos"][0]["description"], "")
            frames = {item["frame_id"]: item for item in page["videos"][0]["frames"]}
            self.assertTrue(frames["video-a:02"]["selected"])
            self.assertTrue(frames["video-a:04"]["is_main"])
            self.assertIn("?v=", frames["video-a:04"]["image_url"])

            deselected = store.set_frame_selected("video-a:02", False)
            selected_response = store.set_frame_selected("video-a:03", True)
            self.assertEqual(deselected["time_seconds"], 2.0)
            self.assertEqual(selected_response["time_seconds"], 3.0)
            self.assertIn("/media/frame/video-a%3A03", selected_response["image_url"])
            self.assertEqual(store.queued_render_count(), 0)

            queued = store.queue_selected_frames("video-a")
            self.assertEqual(queued, 2)
            self.assertEqual(store.queued_render_count(), 2)
            self.assertEqual(store.summary()["queued_render_videos"], 1)

            selected = {
                item["frame_id"]
                for item in store.video_page(limit=8, offset=0)["videos"][0]["frames"]
                if item["render_status"] == "queued"
            }
            self.assertEqual(selected, {"video-a:03", "video-a:04"})

            deselected = store.deselect_non_key_frames("video-a")
            self.assertEqual(deselected, {"video_id": "video-a", "deselected": 1})
            remaining = {
                item["frame_id"]
                for item in store.video_detail("video-a")["frames"]
                if item["selected"]
            }
            self.assertEqual(remaining, {"video-a:04"})

    def test_review_queue_accept_reject_filters_and_memory_date(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = review.VideoMemoryStore(Path(temporary) / "review.sqlite")
            for suffix, capture_date in (("a", "2010-01-02"), ("b", "2011-02-03")):
                video_id = f"video-{suffix}"
                store.upsert_video(
                    {
                        "video_id": video_id,
                        "source_path": f"/source/{suffix}.mov",
                        "source_sha256": suffix * 64,
                        "source_byte_size": 100,
                        "source_mtime_ns": 200,
                        "capture_date": capture_date,
                        "date_source": "filename_date",
                        "capture_timestamp": f"{capture_date}T12:34:56",
                        "duration_seconds": 30.0,
                        "status": "ready",
                        "preview_format": "jpeg",
                    }
                )
                store.replace_frames(
                    video_id,
                    [{
                        "frame_id": f"{video_id}:01",
                        "frame_index": 1,
                        "time_seconds": 1.0,
                        "preview_path": f"/preview/{suffix}.jpg",
                        "preview_bytes": 20,
                        "vision_score": 0.8,
                    }],
                )
                store.save_ai_selection(video_id, [f"{video_id}:01"], f"{video_id}:01", "apple_vision", "vision")

            first = store.video_page(limit=1, offset=0, review_status="pending")
            self.assertEqual(first["returned_count"], 1)
            self.assertEqual(first["videos"][0]["memory_date"], "2010-01-02")
            self.assertEqual(first["videos"][0]["memory_datetime"], "2010-01-02 123456")

            accepted = store.accept_video(
                "video-a", "2010-01-05 010203", "Arrival", "First office visit."
            )
            self.assertEqual(accepted["review_status"], "accepted")
            self.assertEqual(accepted["memory_date"], "2010-01-05")
            self.assertEqual(accepted["memory_datetime"], "2010-01-05 010203")
            self.assertEqual(accepted["queued"], 1)
            self.assertEqual(store.video_page(limit=1, offset=0, review_status="pending")["videos"][0]["video_id"], "video-b")

            rejected = store.reject_video("video-b")
            self.assertEqual(rejected["review_status"], "rejected")
            self.assertEqual(store.queued_render_count(), 1)
            self.assertEqual(store.video_page(limit=1, offset=0, review_status="pending")["total_count"], 0)
            self.assertEqual(store.video_page(limit=1, offset=0, review_status="accepted")["total_count"], 1)
            self.assertEqual(store.video_page(limit=1, offset=0, review_status="rejected")["total_count"], 1)

            saved = store.save_video_metadata(
                "video-b", "2011-03-04 112233", "Team dinner", "Met the local team."
            )
            self.assertEqual(saved["memory_datetime"], "2011-03-04 112233")
            self.assertEqual(saved["title"], "Team dinner")
            detail = store.video_detail("video-b")
            self.assertEqual(detail["description"], "Met the local team.")
            self.assertEqual(detail["memory_date"], "2011-03-04")
            with self.assertRaises(ValueError):
                store.save_video_metadata("video-b", "2011-03-04 11:22:33", "", "")

            queue = store.video_queue_month(review_status="all", selected_month="2010-01")
            self.assertEqual(
                queue["month_counts"],
                [{"month": "2010-01", "count": 1}, {"month": "2011-03", "count": 1}],
            )
            self.assertEqual(queue["selected_month"], "2010-01")
            self.assertEqual(queue["total_count"], 2)
            self.assertEqual(queue["loaded_count"], 1)
            self.assertNotIn("frames", queue["entries"][0])
            self.assertIn("key_frame_url", queue["entries"][0])

            store.update_output_options("video-b", screenshots=True)
            store.accept_video(
                "video-b", "2011-03-04 112233", "Team dinner", "Met the local team."
            )
            recent_accepted = store.video_queue_month(review_status="accepted")
            self.assertEqual(recent_accepted["selected_month"], "2011-03")
            self.assertEqual(
                [entry["video_id"] for entry in recent_accepted["entries"]],
                ["video-b"],
            )
            explicit_accepted = store.video_queue_month(
                review_status="accepted", selected_month="2010-01"
            )
            self.assertEqual(explicit_accepted["selected_month"], "2010-01")
            self.assertEqual(
                [entry["video_id"] for entry in explicit_accepted["entries"]],
                ["video-a"],
            )

    def test_reviewed_queue_only_lists_user_work_and_defaults_to_last_worked_month(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "review.sqlite"
            store = review.VideoMemoryStore(path)
            for suffix, capture_date in (
                ("a", "2010-01-02"),
                ("b", "2011-02-03"),
                ("untouched", "2012-03-04"),
            ):
                video_id = f"video-{suffix}"
                store.upsert_video(
                    {
                        "video_id": video_id,
                        "source_path": f"/source/{suffix}.mov",
                        "source_sha256": suffix[0] * 64,
                        "source_byte_size": 100,
                        "source_mtime_ns": 200,
                        "capture_date": capture_date,
                        "date_source": "filename_date",
                        "capture_timestamp": f"{capture_date}T12:34:56",
                        "duration_seconds": 30.0,
                        "status": "ready",
                        "preview_format": "jpeg",
                    }
                )

            with mock.patch.object(review, "utc_now", return_value="2026-01-01T00:00:01+00:00"):
                store.save_video_metadata("video-a", "2010-01-02 123456", "A", "")
            with mock.patch.object(review, "utc_now", return_value="2026-01-01T00:00:02+00:00"):
                store.save_video_metadata("video-b", "2011-02-03 123456", "B", "")
            with mock.patch.object(review, "utc_now", return_value="2026-01-01T00:00:03+00:00"):
                store.upsert_video(
                    {
                        "video_id": "video-a",
                        "source_path": "/source/a.mov",
                        "source_sha256": "a" * 64,
                        "source_byte_size": 100,
                        "source_mtime_ns": 200,
                        "capture_date": "2010-01-02",
                        "date_source": "filename_date",
                        "capture_timestamp": "2010-01-02T12:34:56",
                        "duration_seconds": 30.0,
                        "status": "ready",
                        "preview_format": "jpeg",
                    }
                )

            reviewed = store.video_queue_month(review_status="reviewed")
            self.assertEqual(reviewed["total_count"], 2)
            self.assertEqual(reviewed["selected_month"], "2011-02")
            self.assertEqual([entry["video_id"] for entry in reviewed["entries"]], ["video-b"])
            explicit = store.video_queue_month(review_status="reviewed", selected_month="2010-01")
            self.assertEqual(explicit["selected_month"], "2010-01")
            self.assertEqual([entry["video_id"] for entry in explicit["entries"]], ["video-a"])
            self.assertEqual(store.video_page(review_status="reviewed")["total_count"], 2)

            connection = sqlite3.connect(path)
            reviewed_at = dict(
                connection.execute(
                    "SELECT video_id, last_reviewed_at FROM video_review_items"
                ).fetchall()
            )
            connection.close()
            self.assertEqual(reviewed_at["video-a"], "2026-01-01T00:00:01+00:00")
            self.assertEqual(reviewed_at["video-b"], "2026-01-01T00:00:02+00:00")
            self.assertEqual(reviewed_at["video-untouched"], "")

    def test_schema_migration_adds_review_date_and_rotation_without_losing_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "review.sqlite"
            connection = sqlite3.connect(path)
            connection.execute(
                """
                CREATE TABLE video_review_items (
                    video_id TEXT PRIMARY KEY, source_path TEXT NOT NULL,
                    source_sha256 TEXT NOT NULL, source_byte_size INTEGER NOT NULL,
                    source_mtime_ns INTEGER NOT NULL, capture_date TEXT NOT NULL,
                    date_source TEXT NOT NULL, capture_timestamp TEXT NOT NULL DEFAULT '',
                    duration_seconds REAL NOT NULL, status TEXT NOT NULL,
                    preview_format TEXT NOT NULL, ai_status TEXT NOT NULL DEFAULT 'pending',
                    ai_method TEXT NOT NULL DEFAULT '', ai_model TEXT NOT NULL DEFAULT '',
                    ai_prompt_version INTEGER NOT NULL DEFAULT 0,
                    privacy_default INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
                    review_version INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "INSERT INTO video_review_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("legacy", "/legacy.mov", "f" * 64, 1, 2, "2005-06-07", "filename_date", "", 5.0, "ready", "jpeg", "complete", "", "", 1, 0, "", 2, "now", "now"),
            )
            connection.commit()
            connection.close()

            store = review.VideoMemoryStore(path)
            record = store.video_record("legacy")
            self.assertEqual(record["review_status"], "pending")
            self.assertEqual(record["memory_date"], "2005-06-07")
            self.assertEqual(record["memory_datetime"], "2005-06-07 000000")
            self.assertEqual(record["title"], "")
            self.assertEqual(record["description"], "")
            self.assertEqual(record["rotation_override"], 0)
            self.assertEqual(record["output_folder_name"], "")
            self.assertEqual(record["last_reviewed_at"], "now")
            self.assertEqual(record["output_screenshots"], 1)
            self.assertEqual(record["output_embedded"], 0)
            self.assertEqual(record["output_summary"], 0)
            self.assertEqual(record["human_edit_version"], 1)
            self.assertEqual(record["duplicate_group_id"], "")
            self.assertEqual(record["duplicate_state"], "")

    def test_output_folder_names_are_readable_unique_and_persistent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = review.VideoMemoryStore(root / "review.sqlite")
            sources = {
                "a" * 64: root / "Only" / "2010-01-01 010101.mov",
                "b" * 64: root / "Shared" / "2010-11-07 174843.mp4",
                "c" * 64: root / "Shared" / "2010-11-07 174843.MOV",
                "d" * 64: root / "Parent One" / "duplicate.mov",
                "e" * 64: root / "Parent Two" / "duplicate.mov",
            }
            for video_id, source in sources.items():
                store.upsert_video(
                    {
                        "video_id": video_id,
                        "source_path": str(source),
                        "source_sha256": video_id,
                        "source_byte_size": 1,
                        "source_mtime_ns": 2,
                        "capture_date": "2010-01-01",
                        "date_source": "filename_date",
                        "capture_timestamp": "",
                        "duration_seconds": 1.0,
                        "status": "ready",
                        "preview_format": "jpeg",
                    }
                )

            names = store.assign_output_folder_names()

            self.assertEqual(names["a" * 64], "2010-01-01 010101")
            self.assertEqual(names["b" * 64], "2010-11-07 174843.mp4")
            self.assertEqual(names["c" * 64], "2010-11-07 174843.MOV")
            self.assertEqual(names["d" * 64], "duplicate.mov — Parent One")
            self.assertEqual(names["e" * 64], "duplicate.mov — Parent Two")
            self.assertEqual(store.assign_output_folder_names(), names)

    def test_rotation_filters_are_explicit_and_normalized(self) -> None:
        self.assertEqual(review.video_filters(0, 640), ["scale=640:640:force_original_aspect_ratio=decrease"])
        self.assertEqual(review.video_filters(90, None), ["transpose=clock"])
        self.assertEqual(review.video_filters(-90, 640), ["transpose=cclock", "scale=640:640:force_original_aspect_ratio=decrease"])
        self.assertEqual(review.video_filters(180, None), ["hflip", "vflip"])
        with self.assertRaises(ValueError):
            review.video_filters(45, 640)

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required")
    def test_capture_frame_appends_a_selected_custom_choice(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            subprocess.run(
                [
                    str(shutil.which("ffmpeg")), "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "color=c=red:size=320x240:rate=10",
                    "-t", "2", "-pix_fmt", "yuv420p", "-y", str(source),
                ],
                check=True,
            )
            sha = hashlib.sha256(source.read_bytes()).hexdigest()
            store = review.VideoMemoryStore(root / "review.sqlite")
            store.upsert_video(
                {
                    "video_id": sha, "source_path": str(source), "source_sha256": sha,
                    "source_byte_size": source.stat().st_size,
                    "source_mtime_ns": source.stat().st_mtime_ns,
                    "capture_date": "2020-01-02", "date_source": "filename_date",
                    "duration_seconds": 2.0, "status": "ready", "preview_format": "jpeg",
                }
            )
            frame = review.capture_video_frame(store, root / "cache", sha, 0.75)
            self.assertTrue(frame["selected"])
            self.assertTrue(frame["is_main"])
            self.assertTrue(frame["user_captured"])
            self.assertAlmostEqual(frame["time_seconds"], 0.75)
            self.assertTrue(Path(store.frame_record(frame["frame_id"])["preview_path"]).is_file())
            with self.assertRaises(ValueError):
                review.capture_video_frame(store, root / "cache", sha, 0.76)
            rotated = review.rotate_video_previews(store, root / "cache", sha, "right")
            self.assertEqual(rotated["rotation_override"], 90)
            self.assertEqual(rotated["frames"], 1)
            self.assertEqual(store.video_record(sha)["rotation_override"], 90)
            self.assertTrue(store.frame_record(frame["frame_id"])["selected"])

            end_frame = review.capture_video_frame(store, root / "cache", sha, 2.0)
            self.assertAlmostEqual(end_frame["time_seconds"], 1.9)

    def test_captured_frame_is_flagged_and_returned_in_timestamp_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = review.VideoMemoryStore(root / "review.sqlite")
            store.upsert_video(
                {
                    "video_id": "video-order", "source_path": "/source/order.mov",
                    "source_sha256": "e" * 64, "source_byte_size": 100,
                    "source_mtime_ns": 200, "capture_date": "2020-01-02",
                    "date_source": "filename_date", "duration_seconds": 2.0,
                    "status": "ready", "preview_format": "jpeg",
                }
            )
            store.replace_frames(
                "video-order",
                [
                    {
                        "frame_id": "video-order:01", "frame_index": 1,
                        "time_seconds": 0.5, "preview_path": "/preview/first.jpg",
                        "preview_bytes": 1, "vision_score": 0.1,
                    },
                    {
                        "frame_id": "video-order:02", "frame_index": 2,
                        "time_seconds": 1.5, "preview_path": "/preview/second.jpg",
                        "preview_bytes": 1, "vision_score": 0.2,
                    },
                ],
            )

            captured = store.add_captured_frame(
                "video-order", time_seconds=0.75, preview_path=root / "captured.jpg",
                preview_bytes=1, vision_score=0.3,
            )
            ordered = store.video_page(limit=1, offset=0)["videos"][0]["frames"]

            self.assertTrue(captured["user_captured"])
            self.assertEqual([item["time_seconds"] for item in ordered], [0.5, 0.75, 1.5])
            self.assertEqual([item["user_captured"] for item in ordered], [False, True, False])

    def test_video_and_frame_privacy_are_independently_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = review.VideoMemoryStore(Path(temporary) / "review.sqlite")
            store.upsert_video(
                {
                    "video_id": "video-private",
                    "source_path": "/source/private.mov",
                    "source_sha256": "c" * 64,
                    "source_byte_size": 100,
                    "source_mtime_ns": 200,
                    "capture_date": "2018-07-08",
                    "date_source": "filename_date",
                    "capture_timestamp": "",
                    "duration_seconds": 30.0,
                    "status": "ready",
                    "preview_format": "jpeg",
                }
            )
            store.replace_frames(
                "video-private",
                [
                    {
                        "frame_id": f"video-private:{index:02d}",
                        "frame_index": index,
                        "time_seconds": float(index),
                        "preview_path": f"/preview/{index}.jpg",
                        "preview_bytes": 20,
                        "vision_score": 0.1 * index,
                    }
                    for index in range(1, 4)
                ],
            )

            all_private = store.set_video_private("video-private", True)
            self.assertEqual(all_private["privacy_state"], "all")
            self.assertEqual(all_private["private_count"], 3)

            no_private = store.set_video_private("video-private", False)
            self.assertEqual(no_private["privacy_state"], "none")
            self.assertEqual(no_private["private_count"], 0)
            self.assertFalse(no_private["video_private"])

            store.set_video_private("video-private", True)

            individual = store.set_frame_private("video-private:02", False)
            self.assertFalse(individual["private"])
            self.assertEqual(individual["video_privacy"]["privacy_state"], "mixed")

            page = store.video_page(limit=8, offset=0)["videos"][0]
            self.assertEqual(page["privacy_state"], "mixed")
            self.assertEqual(page["private_count"], 2)
            privacy_by_frame = {frame["frame_id"]: frame["private"] for frame in page["frames"]}
            self.assertEqual(
                privacy_by_frame,
                {
                    "video-private:01": True,
                    "video-private:02": False,
                    "video-private:03": True,
                },
            )

            store.replace_frames(
                "video-private",
                [
                    {
                        "frame_id": f"video-private:{index:02d}",
                        "frame_index": index,
                        "time_seconds": float(index),
                        "preview_path": f"/preview/rebuilt-{index}.jpg",
                        "preview_bytes": 21,
                        "vision_score": 0.2 * index,
                    }
                    for index in range(1, 4)
                ],
            )
            rebuilt = store.video_page(limit=8, offset=0)["videos"][0]
            self.assertEqual(
                {frame["frame_id"]: frame["private"] for frame in rebuilt["frames"]},
                privacy_by_frame,
            )

    def test_privacy_api_supports_individual_and_video_level_updates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = review.VideoMemoryServerState(root)
            state.store.upsert_video(
                {
                    "video_id": "video-api",
                    "source_path": "/source/api.mov",
                    "source_sha256": "d" * 64,
                    "source_byte_size": 100,
                    "source_mtime_ns": 200,
                    "capture_date": "2019-01-02",
                    "date_source": "filename_date",
                    "capture_timestamp": "",
                    "duration_seconds": 30.0,
                    "status": "ready",
                    "preview_format": "jpeg",
                }
            )
            state.store.replace_frames(
                "video-api",
                [
                    {
                        "frame_id": "video-api:01",
                        "frame_index": 1,
                        "time_seconds": 1.0,
                        "preview_path": "/preview/1.jpg",
                        "preview_bytes": 20,
                        "vision_score": 0.5,
                    },
                    {
                        "frame_id": "video-api:02",
                        "frame_index": 2,
                        "time_seconds": 2.0,
                        "preview_path": "/preview/2.jpg",
                        "preview_bytes": 20,
                        "vision_score": 0.6,
                    },
                ],
            )
            state.store.save_ai_selection(
                "video-api", ["video-api:02"], "video-api:02", "apple_vision", "vision"
            )
            server = review.ThreadingHTTPServer(
                ("127.0.0.1", 0), review.create_handler(state)
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = f"http://127.0.0.1:{server.server_port}"

                def post(path: str, payload: dict[str, object]) -> dict[str, object]:
                    request = urllib.request.Request(
                        f"{base_url}{path}",
                        data=json.dumps(payload).encode(),
                        headers={"content-type": "application/json"},
                        method="POST",
                    )
                    with urllib.request.urlopen(request, timeout=5) as response:
                        return json.loads(response.read())

                individual = post("/api/frames/video-api%3A01/privacy", {"private": True})
                bulk = post("/api/videos/video-api/privacy", {"private": True})
                cleared = post("/api/videos/video-api/privacy", {"private": False})
                saved_metadata = post(
                    "/api/videos/video-api/metadata",
                    {
                        "memory_datetime": "2019-01-03 112233",
                        "title": "API title",
                        "description": "API description",
                    },
                )
                accepted = post(
                    "/api/videos/video-api/accept",
                    {
                        "memory_datetime": "2019-01-03 112233",
                        "title": "API title",
                        "description": "API description",
                    },
                )
                with urllib.request.urlopen(
                    f"{base_url}/api/video-queue?review_status=accepted&month=2019-01",
                    timeout=5,
                ) as response:
                    queue = json.loads(response.read())
                with urllib.request.urlopen(f"{base_url}/api/videos/video-api", timeout=5) as response:
                    detail = json.loads(response.read())
                rejected = post("/api/videos/video-api/reject", {})
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

            self.assertTrue(individual["private"])
            self.assertEqual(individual["video_privacy"]["privacy_state"], "mixed")
            self.assertTrue(bulk["all_private"])
            self.assertEqual(bulk["private_count"], 2)
            self.assertFalse(cleared["all_private"])
            self.assertEqual(cleared["private_count"], 0)
            self.assertFalse(cleared["video_private"])
            self.assertEqual(saved_metadata["memory_datetime"], "2019-01-03 112233")
            self.assertEqual(saved_metadata["title"], "API title")
            self.assertEqual(accepted["review_status"], "accepted")
            self.assertEqual(accepted["queued"], 1)
            self.assertEqual(queue["entries"][0]["video_id"], "video-api")
            self.assertEqual(queue["entries"][0]["title"], "API title")
            self.assertEqual(detail["description"], "API description")
            self.assertEqual(rejected["review_status"], "rejected")
            self.assertEqual(state.store.queued_render_count(), 0)

    def test_triage_page_and_api_are_served_from_the_existing_review_service(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = review.VideoMemoryServerState(Path(temporary))
            state.store.upsert_video(
                {
                    "video_id": "video-triage-api",
                    "source_path": "/source/2020-01-02 API Interview.mkv",
                    "source_sha256": "e" * 64,
                    "source_byte_size": 100,
                    "source_mtime_ns": 200,
                    "capture_date": "2020-01-02",
                    "date_source": "filename_date",
                    "capture_timestamp": "2020-01-02T03:04:05",
                    "duration_seconds": 60.0,
                    "status": "ready",
                    "preview_format": "jpeg",
                }
            )
            state.store.replace_frames(
                "video-triage-api",
                [{
                    "frame_id": "video-triage-api:01",
                    "frame_index": 1,
                    "time_seconds": 10.0,
                    "preview_path": "/preview/triage.jpg",
                    "preview_bytes": 20,
                    "vision_score": 0.8,
                }],
            )
            server = review.ThreadingHTTPServer(
                ("127.0.0.1", 0), review.create_handler(state)
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = f"http://127.0.0.1:{server.server_port}"
                with urllib.request.urlopen(f"{base_url}/triage", timeout=5) as response:
                    html = response.read().decode()
                with urllib.request.urlopen(
                    f"{base_url}/api/triage?limit=48&triage_status=not_triaged",
                    timeout=5,
                ) as response:
                    listing = json.loads(response.read())
                request = urllib.request.Request(
                    f"{base_url}/api/triage/video-triage-api",
                    data=json.dumps(
                        {"rating": 4, "triage_status": "detail_wanted"}
                    ).encode(),
                    headers={"content-type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    updated = json.loads(response.read())
                outputs_request = urllib.request.Request(
                    f"{base_url}/api/triage/video-triage-api/outputs",
                    data=json.dumps({"embedded": True}).encode(),
                    headers={"content-type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(outputs_request, timeout=5) as response:
                    outputs_updated = json.loads(response.read())
                metadata_request = urllib.request.Request(
                    f"{base_url}/api/triage/video-triage-api/metadata",
                    data=json.dumps(
                        {
                            "memory_datetime": "2020-01-02 030405",
                            "title": "Saved interview title",
                            "description": "Saved from rapid triage.",
                        }
                    ).encode(),
                    headers={"content-type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(metadata_request, timeout=5) as response:
                    saved_metadata = json.loads(response.read())
                summary_request = urllib.request.Request(
                    f"{base_url}/api/triage/video-triage-api/summary",
                    data=b"{}",
                    headers={"content-type": "application/json"},
                    method="POST",
                )
                with mock.patch.object(
                    state,
                    "start_synopsis",
                    return_value={"status": "queued", "url": "", "error": ""},
                ):
                    with urllib.request.urlopen(summary_request, timeout=5) as response:
                        summary_queued = json.loads(response.read())
                reject_request = urllib.request.Request(
                    f"{base_url}/api/triage/video-triage-api/reject",
                    data=b"{}",
                    headers={"content-type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(reject_request, timeout=5) as response:
                    rejected = json.loads(response.read())
                privacy_request = urllib.request.Request(
                    f"{base_url}/api/videos/video-triage-api/privacy",
                    data=json.dumps({"private": True}).encode(),
                    headers={"content-type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(privacy_request, timeout=5) as response:
                    privacy = json.loads(response.read())
                hidden_update_request = urllib.request.Request(
                    f"{base_url}/api/triage/video-triage-api",
                    data=json.dumps({"rating": 2}).encode(),
                    headers={"content-type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as hidden_error:
                    urllib.request.urlopen(hidden_update_request, timeout=5)
                self.assertEqual(hidden_error.exception.code, 400)
                hidden_error.exception.close()
                shown_update_request = urllib.request.Request(
                    f"{base_url}/api/triage/video-triage-api",
                    data=json.dumps({"rating": 2, "show_private": True}).encode(),
                    headers={"content-type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(shown_update_request, timeout=5) as response:
                    shown_update = json.loads(response.read())
                with urllib.request.urlopen(
                    f"{base_url}/api/triage?limit=48&triage_status=all",
                    timeout=5,
                ) as response:
                    hidden_listing = json.loads(response.read())
                with urllib.request.urlopen(
                    f"{base_url}/api/triage?limit=48&triage_status=all&show_private=1",
                    timeout=5,
                ) as response:
                    shown_listing = json.loads(response.read())
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

            self.assertIn("Video Rapid Triage", html)
            self.assertEqual(listing["items"][0]["video_id"], "video-triage-api")
            self.assertEqual(listing["items"][0]["inferred_title"], "API Interview")
            self.assertEqual(updated["updated_count"], 1)
            self.assertTrue(outputs_updated["output_screenshots"])
            self.assertTrue(outputs_updated["output_embedded"])
            self.assertFalse(outputs_updated["output_summary"])
            self.assertEqual(saved_metadata["title"], "Saved interview title")
            self.assertEqual(summary_queued["triage_status"], "summary_wanted")
            self.assertEqual(summary_queued["synopsis_status"], "queued")
            self.assertFalse(summary_queued["output_screenshots"])
            self.assertFalse(summary_queued["output_embedded"])
            self.assertTrue(summary_queued["output_summary"])
            self.assertEqual(rejected["review_status"], "rejected")
            self.assertEqual(rejected["triage_status"], "rejected")
            self.assertFalse(rejected["output_screenshots"])
            self.assertFalse(rejected["output_embedded"])
            self.assertFalse(rejected["output_summary"])
            self.assertTrue(privacy["all_private"])
            self.assertEqual(hidden_listing["total_count"], 0)
            self.assertEqual(shown_listing["total_count"], 1)
            self.assertTrue(shown_listing["items"][0]["all_private"])
            self.assertEqual(shown_update["updated_count"], 1)
            detail = state.store.video_detail("video-triage-api")
            self.assertEqual(detail["title"], "Saved interview title")
            self.assertEqual(detail["description"], "Saved from rapid triage.")
            self.assertEqual(detail["triage_status"], "rejected")

    def test_open_in_finder_api_reveals_the_recorded_source_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source video.mov"
            source.write_bytes(b"video")
            state = review.VideoMemoryServerState(root)
            state.store.upsert_video(
                {
                    "video_id": "video-finder",
                    "source_path": str(source),
                    "source_sha256": "f" * 64,
                    "source_byte_size": source.stat().st_size,
                    "source_mtime_ns": source.stat().st_mtime_ns,
                    "capture_date": "2019-01-02",
                    "date_source": "filename_date",
                    "capture_timestamp": "",
                    "duration_seconds": 30.0,
                    "status": "ready",
                    "preview_format": "jpeg",
                }
            )
            server = review.ThreadingHTTPServer(
                ("127.0.0.1", 0), review.create_handler(state)
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{server.server_port}/api/videos/video-finder/open-in-finder",
                    data=b"{}",
                    headers={"content-type": "application/json"},
                    method="POST",
                )
                completed = subprocess.CompletedProcess(
                    ["open", "-R", str(source.resolve())], 0, stdout="", stderr=""
                )
                with mock.patch.object(review.sys, "platform", "darwin"), mock.patch.object(
                    review, "_run", return_value=completed
                ) as run:
                    with urllib.request.urlopen(request, timeout=5) as response:
                        result = json.loads(response.read())
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

            self.assertEqual(result, {"revealed": True})
            run.assert_called_once_with(
                ["open", "-R", str(source.resolve())], timeout=10
            )

    def test_open_in_finder_rejects_unknown_offline_and_failed_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = review.VideoMemoryServerState(root)
            with self.assertRaisesRegex(LookupError, "Video not found"):
                review.reveal_source_in_finder(state.store, "missing")

            source = root / "offline.mov"
            state.store.upsert_video(
                {
                    "video_id": "video-offline",
                    "source_path": str(source),
                    "source_sha256": "e" * 64,
                    "source_byte_size": 5,
                    "source_mtime_ns": 1,
                    "capture_date": "2019-01-02",
                    "date_source": "filename_date",
                    "capture_timestamp": "",
                    "duration_seconds": 30.0,
                    "status": "ready",
                    "preview_format": "jpeg",
                }
            )
            with self.assertRaisesRegex(FileNotFoundError, "Source video is offline"):
                review.reveal_source_in_finder(state.store, "video-offline")

            source.write_bytes(b"video")
            failed = subprocess.CompletedProcess(
                ["open", "-R", str(source.resolve())], 1, stdout="", stderr="failed"
            )
            with mock.patch.object(review.sys, "platform", "darwin"), mock.patch.object(
                review, "_run", return_value=failed
            ):
                with self.assertRaisesRegex(RuntimeError, "Finder could not reveal"):
                    review.reveal_source_in_finder(state.store, "video-offline")

    def test_video_media_ranges_are_session_cacheable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            source.write_bytes(b"0123456789")
            state = review.VideoMemoryServerState(root)
            state.store.upsert_video(
                {
                    "video_id": "video-cache",
                    "source_path": str(source),
                    "source_sha256": "e" * 64,
                    "source_byte_size": source.stat().st_size,
                    "source_mtime_ns": source.stat().st_mtime_ns,
                    "capture_date": "2019-01-02",
                    "date_source": "filename_date",
                    "capture_timestamp": "",
                    "duration_seconds": 30.0,
                    "status": "ready",
                    "preview_format": "jpeg",
                }
            )
            proxy = review.playback_proxy_path(state.cache_root, "video-cache")
            proxy.parent.mkdir(parents=True, exist_ok=True)
            proxy.write_bytes(b"0123456789")
            server = review.ThreadingHTTPServer(
                ("127.0.0.1", 0), review.create_handler(state)
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{server.server_port}/media/video/video-cache?session=test",
                    headers={"Range": "bytes=2-5"},
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    payload = response.read()
                    status = response.status
                    cache_control = response.headers["cache-control"]
                    etag = response.headers["etag"]
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

            self.assertEqual(status, 206)
            self.assertEqual(payload, b"2345")
            self.assertEqual(cache_control, "private, max-age=31536000, immutable")
            self.assertTrue(etag.startswith('"'))

    def test_render_queue_does_not_run_until_explicit_processor_trigger(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = review.VideoMemoryStore(root / "review.sqlite")
            store.upsert_video(
                {
                    "video_id": "video-b",
                    "source_path": str(root / "source.mov"),
                    "source_sha256": "b" * 64,
                    "source_byte_size": 100,
                    "source_mtime_ns": 200,
                    "capture_date": "2020-01-02",
                    "date_source": "filename_date",
                    "capture_timestamp": "",
                    "duration_seconds": 30.0,
                    "status": "ready",
                    "preview_format": "jpeg",
                }
            )
            store.replace_frames(
                "video-b",
                [{
                    "frame_id": "video-b:01",
                    "frame_index": 1,
                    "time_seconds": 1.5,
                    "preview_path": str(root / "preview.jpg"),
                    "preview_bytes": 20,
                    "vision_score": 0.8,
                }],
            )
            store.save_ai_selection(
                "video-b",
                ["video-b:01"],
                "video-b:01",
                method="apple_vision",
                model="vision",
            )
            store.queue_selected_frames("video-b")
            calls: list[str] = []

            def renderer(item: dict[str, object]) -> Path:
                calls.append(str(item["frame_id"]))
                output = root / "final.jpg"
                output.write_bytes(b"jpeg")
                return output

            self.assertEqual(calls, [])
            summary = review.process_render_queue(store, renderer, limit=10)

            self.assertEqual(calls, ["video-b:01"])
            self.assertEqual(summary, {"processed": 1, "rendered": 1, "failed": 0})
            self.assertEqual(store.queued_render_count(), 0)

    def test_render_error_details_are_frame_level_private_and_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = review.VideoMemoryStore(root / "review.sqlite")
            store.upsert_video(
                {
                    "video_id": "video-errors",
                    "source_path": "/source/private-name.mov",
                    "source_sha256": "f" * 64,
                    "source_byte_size": 100,
                    "source_mtime_ns": 200,
                    "capture_date": "2020-01-02",
                    "date_source": "filename_date",
                    "capture_timestamp": "",
                    "duration_seconds": 2.0,
                    "status": "ready",
                    "preview_format": "jpeg",
                }
            )
            store.replace_frames(
                "video-errors",
                [
                    {
                        "frame_id": f"video-errors:{index:02d}",
                        "frame_index": index,
                        "time_seconds": float(index),
                        "preview_path": str(root / f"preview-{index}.jpg"),
                        "preview_bytes": 20,
                        "vision_score": 0.8,
                    }
                    for index in (1, 2)
                ],
            )
            store.save_ai_selection(
                "video-errors",
                ["video-errors:01", "video-errors:02"],
                "video-errors:01",
                method="apple_vision",
                model="vision",
            )
            store.set_video_private("video-errors", True)
            store.accept_video("video-errors", "2020-01-02 000000", "", "")
            claimed = store.claim_render_items(10)
            for item in claimed:
                store.mark_render_failed(str(item["frame_id"]), "encoder failed")

            details = store.render_error_details()

            self.assertEqual(details["count"], 2)
            self.assertEqual(details["video_count"], 1)
            self.assertEqual(
                [item["role"] for item in details["errors"]],
                ["key", "linked"],
            )
            self.assertTrue(all(item["private"] for item in details["errors"]))
            self.assertNotIn("source_path", details["errors"][0])
            self.assertNotIn("title", details["errors"][0])
            self.assertEqual(store.requeue_render_errors(), 2)
            self.assertEqual(store.queued_render_count(), 2)

    def test_render_summary_exposes_background_worker_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = review.VideoMemoryServerState(Path(temporary))
            state._render_thread = mock.Mock()
            state._render_thread.is_alive.return_value = True

            summary = state.render_summary()

            self.assertTrue(summary["render_running"])
            self.assertEqual(summary["queued_renders"], 0)
            self.assertEqual(summary["rendered"], 0)
            self.assertEqual(summary["render_errors"], 0)

    def test_requeue_render_errors_does_not_start_the_render_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = review.VideoMemoryServerState(Path(temporary))
            state.store.requeue_render_errors = mock.Mock(return_value=4)
            state.start_render = mock.Mock()

            summary = state.requeue_render_errors()

            self.assertEqual(summary["requeued"], 4)
            state.start_render.assert_not_called()

    def test_render_start_api_returns_observable_worker_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = review.VideoMemoryServerState(Path(temporary))
            state.start_render = mock.Mock(return_value=True)
            state.render_summary = mock.Mock(
                return_value={
                    "queued_renders": 2,
                    "rendering": 1,
                    "rendered": 0,
                    "render_errors": 0,
                    "render_running": True,
                    "render_worker_error": "",
                }
            )
            server = review.ThreadingHTTPServer(
                ("127.0.0.1", 0), review.create_handler(state)
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{server.server_port}/api/render/start",
                    data=b"{}",
                    headers={"content-type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    payload = json.loads(response.read())
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

            state.start_render.assert_called_once_with()
            self.assertTrue(payload["started"])
            self.assertTrue(payload["render_running"])
            self.assertEqual(payload["rendering"], 1)

    def test_final_renderer_retries_empty_seek_earlier_and_records_resolved_time(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mov"
            source.write_bytes(b"video")
            requested_time = 5.0

            def fake_extract(
                source_path: Path,
                output_path: Path,
                time_seconds: float,
                **options: object,
            ) -> float:
                self.assertEqual(source_path, source)
                self.assertEqual(time_seconds, requested_time)
                self.assertTrue(options["retry_earlier"])
                self.assertEqual(options["retry_earlier_limit_seconds"], 0.5)
                output_path.write_bytes(b"jpeg")
                return 4.5

            item = {
                "video_id": "a" * 64,
                "frame_index": 1,
                "export_sequence": 2,
                "export_count": 12,
                "time_seconds": requested_time,
                "duration_seconds": 10.0,
                "source_path": str(source),
                "source_sha256": "b" * 64,
                "source_byte_size": source.stat().st_size,
                "rotation_override": 0,
                "is_main": 1,
                "private": 0,
                "user_captured": 1,
                "capture_date": "2020-01-02",
                "date_source": "filename_date",
                "capture_timestamp": "",
                "memory_datetime": "2020-01-02 000000",
                "title": "",
                "description": "",
            }

            with mock.patch.object(review, "extract_frame", side_effect=fake_extract):
                output = review.final_renderer(root / "output")(item)

            self.assertEqual(output.parent.name, "source")
            self.assertEqual(output.name, "frame-02-000005000ms-rot000-main.jpg")
            provenance = json.loads(output.with_suffix(".json").read_text())
            self.assertEqual(provenance["requested_time_seconds"], requested_time)
            self.assertEqual(provenance["time_seconds"], 4.5)
            self.assertEqual(provenance["export_sequence"], 2)
            self.assertEqual(provenance["export_count"], 12)

    def test_claim_render_items_numbers_selected_frames_by_time(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = review.VideoMemoryStore(root / "review.sqlite")
            video_id = "b" * 64
            store.upsert_video(
                {
                    "video_id": video_id,
                    "source_path": str(root / "2020-01-02 030405 (screen capture).mov"),
                    "source_sha256": video_id,
                    "source_byte_size": 100,
                    "source_mtime_ns": 200,
                    "capture_date": "2020-01-02",
                    "date_source": "filename_date",
                    "capture_timestamp": "",
                    "duration_seconds": 30.0,
                    "status": "ready",
                    "preview_format": "jpeg",
                }
            )
            frames = [
                {
                    "frame_id": f"{video_id}:{index:02d}",
                    "frame_index": index,
                    "time_seconds": time_seconds,
                    "preview_path": str(root / f"preview-{index}.jpg"),
                    "preview_bytes": 20,
                    "vision_score": 0.8,
                }
                for index, time_seconds in ((6, 20.0), (12, 5.0), (15, 10.0))
            ]
            store.replace_frames(video_id, frames)
            store.save_ai_selection(
                video_id,
                [frame["frame_id"] for frame in frames],
                f"{video_id}:12",
                method="apple_vision",
                model="vision",
            )
            store.queue_selected_frames(video_id)

            claimed = store.claim_render_items(10)

            self.assertEqual(
                [item["frame_index"] for item in claimed],
                [12, 15, 6],
            )
            self.assertEqual(
                [item["export_sequence"] for item in claimed],
                [1, 2, 3],
            )
            self.assertTrue(all(item["export_count"] == 3 for item in claimed))

    def test_final_output_path_uses_count_specific_zero_padding(self) -> None:
        source = Path("/videos/2020-01-02 030405 (screen capture).mov")
        expected_prefixes = {
            1: "frame-1-",
            9: "frame-1-",
            10: "frame-01-",
            99: "frame-01-",
            100: "frame-001-",
            999: "frame-001-",
            1000: "frame-0001-",
        }
        for export_count, prefix in expected_prefixes.items():
            with self.subTest(export_count=export_count):
                path = review.final_output_path(
                    Path("/output"),
                    {
                        "source_path": str(source),
                        "export_sequence": 1,
                        "export_count": export_count,
                        "time_seconds": 2.5,
                        "rotation_override": 0,
                        "is_main": 0,
                    },
                )
                self.assertEqual(path.parent.name, source.stem)
                self.assertTrue(path.name.startswith(prefix))

    def test_migrate_rendered_outputs_moves_pairs_and_updates_database_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output_root = (root / "video_memory_frames").resolve()
            store = review.VideoMemoryStore(root / "review.sqlite")
            video_id = "c" * 64
            source = root / "2020-01-02 030405 (screen capture).mov"
            store.upsert_video(
                {
                    "video_id": video_id,
                    "source_path": str(source),
                    "source_sha256": video_id,
                    "source_byte_size": 100,
                    "source_mtime_ns": 200,
                    "capture_date": "2020-01-02",
                    "date_source": "filename_date",
                    "capture_timestamp": "",
                    "duration_seconds": 30.0,
                    "status": "ready",
                    "preview_format": "jpeg",
                }
            )
            frames = [
                {
                    "frame_id": f"{video_id}:{index:02d}",
                    "frame_index": index,
                    "time_seconds": time_seconds,
                    "preview_path": str(root / f"preview-{index}.jpg"),
                    "preview_bytes": 20,
                    "vision_score": 0.8,
                }
                for index, time_seconds in ((6, 20.0), (15, 10.0))
            ]
            store.replace_frames(video_id, frames)
            store.save_ai_selection(
                video_id,
                [frame["frame_id"] for frame in frames],
                f"{video_id}:15",
                method="apple_vision",
                model="vision",
            )
            old_folder = output_root / video_id[:2] / video_id
            old_folder.mkdir(parents=True)
            for index, time_seconds in ((6, 20.0), (15, 10.0)):
                old_path = old_folder / f"frame-{index:02d}-{int(time_seconds * 1000):09d}ms-rot000-linked.jpg"
                old_path.write_bytes(f"jpeg-{index}".encode())
                old_path.with_suffix(".json").write_text(
                    json.dumps({"video_id": video_id, "frame_index": index})
                )
                store.mark_rendered(f"{video_id}:{index:02d}", old_path)

            summary = review.migrate_rendered_output_names(store, output_root)

            self.assertEqual(summary, {"videos": 1, "frames": 2, "files_moved": 4})
            expected = {
                15: output_root / source.stem / "frame-1-000010000ms-rot000-main.jpg",
                6: output_root / source.stem / "frame-2-000020000ms-rot000-linked.jpg",
            }
            for index, path in expected.items():
                self.assertEqual(Path(store.frame_record(f"{video_id}:{index:02d}")["final_path"]), path)
                self.assertTrue(path.is_file())
                self.assertTrue(path.with_suffix(".json").is_file())
                self.assertEqual(path.read_bytes(), f"jpeg-{index}".encode())
            store.set_frame_private(f"{video_id}:15", True)
            self.assertTrue(json.loads(expected[15].with_suffix(".json").read_text())["private"])
            self.assertFalse(old_folder.exists())

    def test_render_reconciles_existing_names_after_earlier_frame_is_selected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output_root = (root / "video_memory_frames").resolve()
            store = review.VideoMemoryStore(root / "review.sqlite")
            video_id = "d" * 64
            source = root / "2020-01-02 030405.mov"
            store.upsert_video(
                {
                    "video_id": video_id,
                    "source_path": str(source),
                    "source_sha256": video_id,
                    "source_byte_size": 1,
                    "source_mtime_ns": 2,
                    "capture_date": "2020-01-02",
                    "date_source": "filename_date",
                    "capture_timestamp": "",
                    "duration_seconds": 30.0,
                    "status": "ready",
                    "preview_format": "jpeg",
                }
            )
            frames = [
                {
                    "frame_id": f"{video_id}:{index:02d}",
                    "frame_index": index,
                    "time_seconds": time_seconds,
                    "preview_path": str(root / f"preview-{index}.jpg"),
                    "preview_bytes": 20,
                    "vision_score": 0.8,
                }
                for index, time_seconds in ((1, 10.0), (2, 20.0), (3, 5.0))
            ]
            store.replace_frames(video_id, frames)
            store.save_ai_selection(
                video_id,
                [f"{video_id}:01", f"{video_id}:02"],
                f"{video_id}:01",
                method="apple_vision",
                model="vision",
            )
            folder = output_root / source.stem
            folder.mkdir(parents=True)
            for index, time_seconds in ((1, 10.0), (2, 20.0)):
                old_path = folder / f"frame-{index}-{int(time_seconds * 1000):09d}ms-rot000-{'main' if index == 1 else 'linked'}.jpg"
                old_path.write_bytes(b"existing")
                old_path.with_suffix(".json").write_text(
                    json.dumps({"video_id": video_id, "frame_index": index})
                )
                store.mark_rendered(f"{video_id}:{index:02d}", old_path)
            store.set_frame_selected(f"{video_id}:03", True)
            store.queue_selected_frames(video_id)

            def fake_renderer(item: dict[str, object]) -> Path:
                path = review.final_output_path(output_root, item)
                path.write_bytes(b"new")
                path.with_suffix(".json").write_text(
                    json.dumps({"video_id": video_id, "frame_index": item["frame_index"]})
                )
                return path

            setattr(fake_renderer, "output_root", output_root)
            summary = review.process_render_queue(store, fake_renderer)

            self.assertEqual(summary, {"processed": 1, "rendered": 1, "failed": 0})
            self.assertEqual(
                sorted(path.name for path in folder.glob("*.jpg")),
                [
                    "frame-1-000005000ms-rot000-linked.jpg",
                    "frame-2-000010000ms-rot000-main.jpg",
                    "frame-3-000020000ms-rot000-linked.jpg",
                ],
            )

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required")
    def test_real_renderer_writes_jpeg_and_provenance_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            subprocess.run(
                [
                    str(shutil.which("ffmpeg")),
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=blue:size=320x240:rate=10",
                    "-t",
                    "1",
                    "-pix_fmt",
                    "yuv420p",
                    "-y",
                    str(source),
                ],
                check=True,
            )
            sha = hashlib.sha256(source.read_bytes()).hexdigest()
            store = review.VideoMemoryStore(root / "review.sqlite")
            store.upsert_video(
                {
                    "video_id": sha,
                    "source_path": str(source),
                    "source_sha256": sha,
                    "source_byte_size": source.stat().st_size,
                    "source_mtime_ns": source.stat().st_mtime_ns,
                    "capture_date": "2020-01-02",
                    "date_source": "filename_date",
                    "capture_timestamp": "",
                    "duration_seconds": 1.0,
                    "status": "ready",
                    "preview_format": "jpeg",
                }
            )
            store.replace_frames(
                sha,
                [{
                    "frame_id": f"{sha}:01",
                    "frame_index": 1,
                    "time_seconds": 1.0,
                    "preview_path": str(root / "preview.jpg"),
                    "preview_bytes": 0,
                    "vision_score": 0.5,
                }],
            )
            store.save_ai_selection(
                sha,
                [f"{sha}:01"],
                f"{sha}:01",
                method="apple_vision",
                model="vision",
            )
            store.set_frame_private(f"{sha}:01", True)
            store.queue_selected_frames(sha)

            summary = review.process_render_queue(
                store,
                review.final_renderer(root / "output"),
            )

            self.assertEqual(summary, {"processed": 1, "rendered": 1, "failed": 0})
            record = store.frame_record(f"{sha}:01")
            output = Path(str(record["final_path"]))
            self.assertTrue(output.is_file())
            self.assertGreater(output.stat().st_size, 0)
            provenance = json.loads(output.with_suffix(".json").read_text())
            self.assertEqual(provenance["source_sha256"], sha)
            self.assertEqual(provenance["status"], "rendered_not_canonical")
            self.assertTrue(provenance["private"])
            self.assertAlmostEqual(provenance["requested_time_seconds"], 1.0)
            self.assertAlmostEqual(provenance["time_seconds"], 0.9)
            store.set_frame_private(f"{sha}:01", False)
            cleared_provenance = json.loads(output.with_suffix(".json").read_text())
            self.assertNotIn("private", cleared_provenance)
            self.assertEqual(cleared_provenance["source_sha256"], sha)

    def test_pilot_selection_of_legacy_avi_is_deterministic_unique_and_spans_capture_dates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            index = root / "videos.sqlite"
            connection = sqlite3.connect(index)
            connection.executescript(
                """
                CREATE TABLE photo_library_files (
                    path TEXT PRIMARY KEY,
                    root TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    extension TEXT NOT NULL,
                    byte_size INTEGER NOT NULL,
                    filesystem_mtime_utc TEXT NOT NULL,
                    filename_dates TEXT NOT NULL,
                    media_creation_dates TEXT NOT NULL,
                    filesystem_dates TEXT NOT NULL,
                    quality_score INTEGER NOT NULL DEFAULT 0,
                    quality_evidence TEXT NOT NULL DEFAULT '',
                    indexed_at TEXT NOT NULL,
                    capture_timestamp TEXT NOT NULL DEFAULT '',
                    capture_timestamp_source TEXT NOT NULL DEFAULT '',
                    sha256 TEXT NOT NULL DEFAULT '',
                    filesystem_mtime_ns INTEGER NOT NULL DEFAULT 0,
                    media_width INTEGER,
                    media_height INTEGER,
                    media_duration_seconds REAL
                );
                CREATE TABLE photo_library_dates (
                    file_path TEXT NOT NULL,
                    date TEXT NOT NULL,
                    source TEXT NOT NULL
                );
                """
            )
            for offset, year in enumerate(range(2001, 2021)):
                path = root / f"video-{year}.avi"
                path.write_bytes(b"x")
                sha = f"{offset + 1:064x}"
                connection.execute(
                    """
                    INSERT INTO photo_library_files (
                        path, root, filename, extension, byte_size,
                        filesystem_mtime_utc, filename_dates, media_creation_dates,
                        filesystem_dates, indexed_at, sha256, filesystem_mtime_ns,
                        media_width, media_height, media_duration_seconds
                    ) VALUES (?, ?, ?, '.avi', 1, '', '', '', '', '', ?, 1, 1920, 1080, ?)
                    """,
                    (str(path), str(root), path.name, sha, 10.0 + offset),
                )
                connection.execute(
                    "INSERT INTO photo_library_dates VALUES (?, ?, 'filename_date')",
                    (str(path), f"{year}-06-15"),
                )
            connection.commit()
            connection.close()

            first = review.select_pilot_videos([index], count=8)
            second = review.select_pilot_videos([index], count=8)

            self.assertEqual(first, second)
            self.assertEqual(len(first), 8)
            self.assertEqual(len({item["source_sha256"] for item in first}), 8)
            years = [int(str(item["capture_date"])[:4]) for item in first]
            self.assertLessEqual(min(years), 2003)
            self.assertGreaterEqual(max(years), 2018)

    def test_review_html_is_single_item_picker_style_queue(self) -> None:
        self.assertIn('loading="lazy"', review.REVIEW_HTML)
        self.assertIn('decoding="async"', review.REVIEW_HTML)
        self.assertIn("Accept &amp; Next", review.REVIEW_HTML)
        self.assertIn("Reject &amp; Next", review.REVIEW_HTML)
        self.assertIn("Current key frame", review.REVIEW_HTML)
        self.assertIn("Capture selected frame", review.REVIEW_HTML)
        self.assertIn("data-adjust=\"-0.1\"", review.REVIEW_HTML)
        self.assertIn("data-rotate=\"left\"", review.REVIEW_HTML)
        self.assertIn("data-rotate=\"right\"", review.REVIEW_HTML)
        self.assertIn("Render queued JPGs", review.REVIEW_HTML)
        self.assertIn("summary.queued_render_videos", review.REVIEW_HTML)
        self.assertIn("queuedVideos===1?'video':'videos'", review.REVIEW_HTML)
        self.assertIn("JPGs rendering", review.REVIEW_HTML)
        self.assertIn("JPG render errors", review.REVIEW_HTML)
        self.assertIn('id="renderErrorsButton"', review.REVIEW_HTML)
        self.assertIn('id="renderErrorsDialog"', review.REVIEW_HTML)
        self.assertIn("/api/render/errors", review.REVIEW_HTML)
        self.assertIn("/api/render/requeue-errors", review.REVIEW_HTML)
        self.assertIn("Requeue failed JPGs", review.REVIEW_HTML)
        self.assertIn('data-error-time="', review.REVIEW_HTML)
        self.assertIn("playerTime:Number(button.dataset.errorTime)", review.REVIEW_HTML)
        self.assertIn("Render started", review.REVIEW_HTML)
        self.assertIn("summary.render_running", review.REVIEW_HTML)
        self.assertIn("Open in Finder", review.REVIEW_HTML)
        self.assertIn('data-open-in-finder="1"', review.REVIEW_HTML)
        self.assertIn("postVideo('open-in-finder')", review.REVIEW_HTML)
        self.assertIn("month=", review.REVIEW_HTML)
        self.assertIn("const AUTO_OPEN_MONTHS=4", review.REVIEW_HTML)
        self.assertIn(
            "reviewStatus==='accepted'||reviewStatus==='reviewed'?1:AUTO_OPEN_MONTHS",
            review.REVIEW_HTML,
        )
        self.assertIn(
            "reviewStatus==='all'||reviewStatus==='reviewed'||reviewStatus===newStatus",
            review.REVIEW_HTML,
        )
        self.assertNotIn("month_count=2", review.REVIEW_HTML)
        self.assertIn("review_status", review.REVIEW_HTML)
        self.assertIn('id="queueFilter"', review.REVIEW_HTML)
        self.assertIn('<option value="pending">Queued</option>', review.REVIEW_HTML)
        self.assertIn('<option value="reviewed">Reviewed</option>', review.REVIEW_HTML)
        self.assertIn('<option value="accepted">Accepted</option>', review.REVIEW_HTML)
        self.assertIn('<option value="all">All</option>', review.REVIEW_HTML)
        self.assertIn("options.useRememberedVideo===false?'':", review.REVIEW_HTML)
        self.assertIn("document.getElementById('queueFilter').value='pending'", review.REVIEW_HTML)
        self.assertIn("loadQueue({month:'',useRememberedVideo:false})", review.REVIEW_HTML)
        self.assertIn('id="videoQueue"', review.REVIEW_HTML)
        self.assertIn("groupedQueueEntries", review.REVIEW_HTML)
        self.assertIn("month.count", review.REVIEW_HTML)
        self.assertIn("queueMonthCache", review.REVIEW_HTML)

    def test_triage_state_rating_filters_and_bulk_updates_are_persistent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = review.VideoMemoryStore(Path(temporary) / "review.sqlite")
            for suffix, capture_date, duration in (
                ("a", "2010-01-02", 30.0),
                ("b", "2011-02-03", 90.0),
                ("c", "2012-03-04", 180.0),
            ):
                video_id = f"video-{suffix}"
                store.upsert_video(
                    {
                        "video_id": video_id,
                        "source_path": f"/source/{suffix}.mov",
                        "source_sha256": suffix * 64,
                        "source_byte_size": 100,
                        "source_mtime_ns": 200,
                        "capture_date": capture_date,
                        "date_source": "filename_date",
                        "capture_timestamp": f"{capture_date}T12:34:56",
                        "duration_seconds": duration,
                        "status": "ready",
                        "preview_format": "jpeg",
                        "title": f"Video {suffix.upper()}",
                        "description": "mountain walk" if suffix == "b" else "",
                    }
                )
                store.replace_frames(
                    video_id,
                    [
                        {
                            "frame_id": f"{video_id}:{index:02d}",
                            "frame_index": index,
                            "time_seconds": time_seconds,
                            "preview_path": f"/preview/{suffix}-{index}.jpg",
                            "preview_bytes": 20,
                            "vision_score": 0.1 * index,
                        }
                        for index, time_seconds in ((1, duration * 0.75), (2, duration * 0.25))
                    ],
                )
                store.save_ai_selection(
                    video_id,
                    [f"{video_id}:01"],
                    f"{video_id}:01",
                    "apple_vision",
                    "vision",
                )

            rating_only = store.update_triage(["video-b"], rating=5)
            self.assertEqual(rating_only["updated_count"], 1)
            self.assertEqual(
                store.video_detail("video-b")["triage_status"], "not_triaged"
            )
            updated = store.update_triage(
                ["video-b"], triage_status="detail_wanted"
            )
            bulk = store.update_triage(
                ["video-a", "video-c"], triage_status="no_detail"
            )
            store.update_triage(["video-a"], triage_status="summary_wanted")
            store.set_frame_private("video-c:01", True)

            summary_choice = store.video_detail("video-a")
            self.assertFalse(summary_choice["output_screenshots"])
            self.assertFalse(summary_choice["output_embedded"])
            self.assertTrue(summary_choice["output_summary"])
            detail_choice = store.video_detail("video-b")
            self.assertTrue(detail_choice["output_screenshots"])
            self.assertFalse(detail_choice["output_embedded"])
            self.assertFalse(detail_choice["output_summary"])
            embed_choice = store.video_detail("video-c")
            self.assertFalse(embed_choice["output_screenshots"])
            self.assertTrue(embed_choice["output_embedded"])
            self.assertFalse(embed_choice["output_summary"])

            self.assertEqual(updated["updated_count"], 1)
            self.assertEqual(bulk["updated_count"], 2)
            page = store.triage_page(
                limit=48,
                offset=0,
                triage_status="detail_wanted",
                minimum_rating=4,
                minimum_duration=60,
                maximum_duration=120,
                search="mountain",
                sort="rating_desc",
            )
            self.assertEqual(page["total_count"], 1)
            self.assertEqual(page["items"][0]["video_id"], "video-b")
            self.assertEqual(page["items"][0]["rating"], 5)
            self.assertEqual(
                [frame["time_seconds"] for frame in page["items"][0]["frames"]],
                [22.5, 67.5],
            )
            self.assertEqual(page["summary"]["detail_wanted"], 1)
            self.assertEqual(page["summary"]["summary_wanted"], 1)
            self.assertEqual(page["summary"]["no_detail"], 0)
            self.assertEqual(page["summary"]["ready"], 2)
            summary_page = store.triage_page(
                limit=48, offset=0, triage_status="summary_wanted"
            )
            self.assertEqual(summary_page["total_count"], 1)
            self.assertEqual(summary_page["items"][0]["video_id"], "video-a")
            private_page = store.triage_page(
                limit=48, offset=0, triage_status="all", search="Video C"
            )
            self.assertEqual(private_page["total_count"], 0)
            shown_private_page = store.triage_page(
                limit=48,
                offset=0,
                triage_status="all",
                search="Video C",
                show_private=True,
            )
            self.assertEqual(shown_private_page["total_count"], 1)
            self.assertEqual(shown_private_page["items"][0]["privacy_state"], "mixed")
            self.assertTrue(
                any(frame["private"] for frame in shown_private_page["items"][0]["frames"])
            )
            with self.assertRaises(ValueError):
                store.update_triage(["video-c"], rating=3)
            private_update = store.update_triage(
                ["video-c"], rating=3, allow_private=True
            )
            self.assertEqual(private_update["updated_count"], 1)
            with self.assertRaises(ValueError):
                store.save_video_metadata(
                    "video-c",
                    "2012-03-04 000000",
                    "Private title",
                    "",
                    public_only=True,
                )
            with self.assertRaises(ValueError):
                store.reject_video("video-c", public_only=True)
            with self.assertRaises(ValueError):
                store.update_triage(["video-a"], rating=6)
            with self.assertRaises(ValueError):
                store.update_triage(["video-a"], triage_status="maybe")
            with self.assertRaises(ValueError):
                store.update_triage([], triage_status="not_triaged")

            reopened = review.VideoMemoryStore(Path(temporary) / "review.sqlite")
            self.assertEqual(
                reopened.video_detail("video-b")["triage_status"], "detail_wanted"
            )

    def test_automatic_duplicate_prune_rejects_only_untouched_visual_copies(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = review.VideoMemoryStore(Path(temporary) / "review.sqlite")
            base = review.encode_content_fingerprint(
                [(index, bytes(64)) for index in range(1, 17)]
            )
            near_copy = review.encode_content_fingerprint(
                [(index, bytes([1]) * 64) for index in range(1, 17)]
            )
            different = review.encode_content_fingerprint(
                [(index, bytes([255]) * 64) for index in range(1, 17)]
            )
            with mock.patch.object(
                review,
                "utc_now",
                side_effect=(
                    "2026-01-01T00:00:01+00:00",
                    "2026-01-01T00:00:01+00:00",
                    "2026-01-01T00:00:02+00:00",
                    "2026-01-01T00:00:02+00:00",
                    "2026-01-01T00:00:03+00:00",
                    "2026-01-01T00:00:03+00:00",
                    "2026-01-01T00:00:04+00:00",
                    "2026-01-01T00:00:05+00:00",
                ),
            ):
                self._add_duplicate_test_video(store, "video-first", base)
                self._add_duplicate_test_video(store, "video-copy", near_copy)
                self._add_duplicate_test_video(store, "video-different", different)

            first = store.reconcile_duplicate_groups()
            second = store.reconcile_duplicate_groups()

            self.assertEqual(first["duplicate_groups"], 1)
            self.assertEqual(first["auto_rejected"], 1)
            self.assertEqual(first, second)
            keeper = store.video_detail("video-first")
            copy = store.video_detail("video-copy")
            unrelated = store.video_detail("video-different")
            self.assertEqual(keeper["review_status"], "pending")
            self.assertEqual(keeper["duplicate_state"], "keeper")
            self.assertEqual(copy["review_status"], "rejected")
            self.assertEqual(copy["duplicate_state"], "auto_rejected")
            self.assertEqual(copy["duplicate_of_video_id"], "video-first")
            self.assertEqual(unrelated["review_status"], "pending")
            self.assertEqual(unrelated["duplicate_state"], "")

    def test_accepted_human_edited_duplicate_keeps_the_group_unresolved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = review.VideoMemoryStore(Path(temporary) / "review.sqlite")
            fingerprint = review.encode_content_fingerprint(
                [(index, bytes([index]) * 64) for index in range(1, 17)]
            )
            self._add_duplicate_test_video(store, "video-pending", fingerprint)
            self._add_duplicate_test_video(store, "video-accepted", fingerprint)
            store.update_output_options(
                "video-accepted", screenshots=False, embedded=True
            )
            store.accept_video("video-accepted", "2008-06-21 011812")

            summary = store.reconcile_duplicate_groups()
            problems = store.triage_page(triage_status="problems")

            self.assertEqual(summary["problem_groups"], 1)
            self.assertEqual(summary["auto_rejected"], 0)
            self.assertEqual(problems["total_count"], 2)
            self.assertEqual(
                {item["video_id"] for item in problems["items"]},
                {"video-pending", "video-accepted"},
            )
            self.assertEqual(
                store.video_detail("video-pending")["review_status"], "pending"
            )
            self.assertEqual(
                store.video_detail("video-accepted")["review_status"], "accepted"
            )

    def test_new_preparation_backfills_older_duplicate_candidate_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = review.VideoMemoryStore(Path(temporary) / "review.sqlite")
            fingerprint = review.encode_content_fingerprint(
                [(index, bytes([index]) * 64) for index in range(1, 17)]
            )
            self._add_duplicate_test_video(store, "video-old", None)
            self._add_duplicate_test_video(store, "video-new", fingerprint)
            self._add_duplicate_test_video(
                store,
                "video-other-date",
                None,
                capture_timestamp="2008-06-22T01:18:12",
            )

            def save_fingerprint(
                candidate_store: review.VideoMemoryStore, candidate_id: str
            ) -> bytes:
                candidate_store.save_content_fingerprint(candidate_id, fingerprint)
                return fingerprint

            with mock.patch.object(
                review,
                "fingerprint_prepared_video",
                side_effect=save_fingerprint,
            ) as calculate:
                failures = review.backfill_duplicate_candidate_fingerprints(
                    store, "video-new"
                )

            self.assertEqual(failures, 0)
            calculate.assert_called_once_with(store, "video-old")
            self.assertEqual(
                store.video_record("video-old")["content_fingerprint_version"],
                review.CONTENT_FINGERPRINT_VERSION,
            )

    def test_duplicate_group_with_nonreject_edits_moves_to_problems(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = review.VideoMemoryStore(Path(temporary) / "review.sqlite")
            fingerprint = review.encode_content_fingerprint(
                [(index, bytes([index]) * 64) for index in range(1, 17)]
            )
            self._add_duplicate_test_video(store, "video-a", fingerprint)
            self._add_duplicate_test_video(store, "video-b", fingerprint)
            store.update_triage(["video-b"], rating=5)

            summary = store.reconcile_duplicate_groups()
            problems = store.triage_page(triage_status="problems")

            self.assertEqual(summary["problem_groups"], 1)
            self.assertEqual(summary["auto_rejected"], 0)
            self.assertEqual(problems["total_count"], 2)
            self.assertEqual(
                [item["video_id"] for item in problems["items"]],
                ["video-a", "video-b"],
            )
            self.assertEqual(
                {item["duplicate_group_id"] for item in problems["items"]},
                {"video-a"},
            )
            self.assertTrue(
                all(item["review_status"] == "pending" for item in problems["items"])
            )

    def test_reject_only_does_not_turn_duplicate_group_into_a_problem(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = review.VideoMemoryStore(Path(temporary) / "review.sqlite")
            fingerprint = review.encode_content_fingerprint(
                [(index, bytes([index]) * 64) for index in range(1, 17)]
            )
            self._add_duplicate_test_video(store, "video-a", fingerprint)
            self._add_duplicate_test_video(store, "video-b", fingerprint)
            store.reject_video("video-b")

            summary = store.reconcile_duplicate_groups()

            self.assertEqual(summary["problem_groups"], 0)
            self.assertEqual(store.video_detail("video-a")["duplicate_state"], "keeper")
            self.assertEqual(
                store.video_detail("video-b")["duplicate_state"], "auto_rejected"
            )

    def test_triage_reject_clears_queued_screenshots_before_reenable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = review.VideoMemoryStore(Path(temporary) / "review.sqlite")
            store.upsert_video(
                {
                    "video_id": "video-queued",
                    "source_path": "/source/queued.mov",
                    "source_sha256": "q" * 64,
                    "source_byte_size": 100,
                    "source_mtime_ns": 200,
                    "capture_date": "2008-06-21",
                    "date_source": "filename_date",
                    "capture_timestamp": "2008-06-21T01:18:12",
                    "duration_seconds": 23.4,
                    "status": "ready",
                    "preview_format": "jpeg",
                }
            )
            store.replace_frames(
                "video-queued",
                [{
                    "frame_id": "video-queued:01",
                    "frame_index": 1,
                    "time_seconds": 1.0,
                    "preview_path": "/preview/queued.jpg",
                    "preview_bytes": 20,
                    "vision_score": 0.8,
                }],
            )
            store.save_ai_selection(
                "video-queued", ["video-queued:01"], "video-queued:01", "apple_vision", "vision"
            )
            store.accept_video("video-queued", "2008-06-21 011812")
            self.assertEqual(store.queued_render_count(), 1)

            store.update_triage(["video-queued"], triage_status="rejected")

            self.assertEqual(store.queued_render_count(), 0)
            self.assertEqual(
                store.frame_record("video-queued:01")["render_status"], "not_queued"
            )
            store.update_triage(["video-queued"], triage_status="detail_wanted")
            self.assertEqual(store.queued_render_count(), 0)
            self.assertEqual(
                store.frame_record("video-queued:01")["render_status"], "not_queued"
            )

    def test_keep_duplicate_copy_rejects_siblings_and_preserves_chosen_edits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = review.VideoMemoryStore(Path(temporary) / "review.sqlite")
            fingerprint = review.encode_content_fingerprint(
                [(index, bytes([index]) * 64) for index in range(1, 17)]
            )
            self._add_duplicate_test_video(store, "video-a", fingerprint)
            self._add_duplicate_test_video(store, "video-b", fingerprint)
            store.update_triage(["video-b"], rating=4, triage_status="no_detail")
            store.save_video_metadata(
                "video-b", "2008-06-21 011812", "Chosen copy", "Keep these edits."
            )
            store.reconcile_duplicate_groups()

            result = store.resolve_duplicate_group("video-a", "video-b")

            self.assertEqual(result["keeper_video_id"], "video-b")
            chosen = store.video_detail("video-b")
            rejected = store.video_detail("video-a")
            self.assertEqual(chosen["review_status"], "pending")
            self.assertEqual(chosen["duplicate_state"], "keeper")
            self.assertEqual(chosen["rating"], 4)
            self.assertEqual(chosen["title"], "Chosen copy")
            self.assertEqual(chosen["description"], "Keep these edits.")
            self.assertTrue(chosen["output_embedded"])
            self.assertEqual(rejected["review_status"], "rejected")
            self.assertEqual(rejected["duplicate_state"], "auto_rejected")
            self.assertEqual(rejected["duplicate_of_video_id"], "video-b")
            self.assertEqual(
                store.triage_page(triage_status="problems")["total_count"], 0
            )

    def test_diary_output_options_are_additive_and_reject_is_exclusive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "review.sqlite"
            store = review.VideoMemoryStore(path)
            store.upsert_video(
                {
                    "video_id": "video-output-options",
                    "source_path": "/source/output-options.mov",
                    "source_sha256": "o" * 64,
                    "source_byte_size": 100,
                    "source_mtime_ns": 200,
                    "capture_date": "2013-04-05",
                    "date_source": "filename_date",
                    "capture_timestamp": "2013-04-05T12:34:56",
                    "duration_seconds": 90.0,
                    "status": "ready",
                    "preview_format": "jpeg",
                }
            )

            default = store.video_detail("video-output-options")
            self.assertTrue(default["output_screenshots"])
            self.assertFalse(default["output_embedded"])
            self.assertFalse(default["output_summary"])
            self.assertFalse(default["output_rejected"])

            additive = store.update_output_options(
                "video-output-options",
                embedded=True,
                summary=True,
            )
            self.assertEqual(
                {
                    key: additive[key]
                    for key in (
                        "output_screenshots",
                        "output_embedded",
                        "output_summary",
                        "output_rejected",
                    )
                },
                {
                    "output_screenshots": True,
                    "output_embedded": True,
                    "output_summary": True,
                    "output_rejected": False,
                },
            )

            store.update_output_options("video-output-options", screenshots=False)
            persisted = review.VideoMemoryStore(path).video_detail(
                "video-output-options"
            )
            self.assertFalse(persisted["output_screenshots"])
            self.assertTrue(persisted["output_embedded"])
            self.assertTrue(persisted["output_summary"])

            accepted_without_screenshots = store.accept_video(
                "video-output-options", "2013-04-05 123456", "", ""
            )
            self.assertEqual(accepted_without_screenshots["queued"], 0)

            rejected = store.update_output_options(
                "video-output-options", rejected=True
            )
            self.assertTrue(rejected["output_rejected"])
            self.assertFalse(rejected["output_screenshots"])
            self.assertFalse(rejected["output_embedded"])
            self.assertFalse(rejected["output_summary"])

            reopened = store.update_output_options(
                "video-output-options", embedded=True
            )
            self.assertFalse(reopened["output_rejected"])
            self.assertTrue(reopened["output_embedded"])
            with self.assertRaises(ValueError):
                store.update_output_options("video-output-options")
            with self.assertRaises(ValueError):
                store.update_output_options("video-output-options", summary=1)

    def test_detailed_review_decisions_complete_the_triage_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = review.VideoMemoryStore(Path(temporary) / "review.sqlite")
            for suffix in ("accepted", "rejected"):
                video_id = f"video-{suffix}"
                store.upsert_video(
                    {
                        "video_id": video_id,
                        "source_path": f"/source/{suffix}.mov",
                        "source_sha256": suffix[0] * 64,
                        "source_byte_size": 100,
                        "source_mtime_ns": 200,
                        "capture_date": "2010-01-02",
                        "date_source": "filename_date",
                        "capture_timestamp": "2010-01-02T12:34:56",
                        "duration_seconds": 30.0,
                        "status": "ready",
                        "preview_format": "jpeg",
                    }
                )
                store.replace_frames(
                    video_id,
                    [{
                        "frame_id": f"{video_id}:01",
                        "frame_index": 1,
                        "time_seconds": 1.0,
                        "preview_path": f"/preview/{suffix}.jpg",
                        "preview_bytes": 20,
                        "vision_score": 0.8,
                    }],
                )
                store.save_ai_selection(
                    video_id, [f"{video_id}:01"], f"{video_id}:01", "apple_vision", "vision"
                )

            store.accept_video("video-accepted", "2010-01-02 123456", "", "")
            store.reject_video("video-rejected")

            self.assertEqual(
                store.video_detail("video-accepted")["triage_status"],
                "detail_completed",
            )
            self.assertEqual(
                store.video_detail("video-rejected")["triage_status"],
                "rejected",
            )
            store.update_triage(["video-rejected"], triage_status="no_detail")
            overridden = store.video_detail("video-rejected")
            self.assertEqual(overridden["triage_status"], "no_detail")
            self.assertEqual(overridden["review_status"], "pending")
            store.reject_video("video-rejected")
            store.update_triage(["video-rejected"], triage_status="not_triaged")
            reopened = store.video_detail("video-rejected")
            self.assertEqual(reopened["triage_status"], "not_triaged")
            self.assertEqual(reopened["review_status"], "pending")
            with self.assertRaises(ValueError):
                store.update_triage(["video-accepted"], triage_status="not_triaged")
            self.assertEqual(
                store.video_detail("video-accepted")["triage_status"],
                "detail_completed",
            )

    def test_triage_html_is_bounded_keyboard_driven_and_uses_existing_frames(self) -> None:
        self.assertIn("Video Rapid Triage", review.TRIAGE_HTML)
        self.assertIn("/api/triage?", review.TRIAGE_HTML)
        self.assertIn('loading="lazy"', review.TRIAGE_HTML)
        self.assertIn("nearestFrame", review.TRIAGE_HTML)
        self.assertIn("new Image()", review.TRIAGE_HTML)
        self.assertIn("IntersectionObserver", review.TRIAGE_HTML)
        self.assertIn(">Detailed</button>", review.TRIAGE_HTML)
        self.assertIn(">Summary</button>", review.TRIAGE_HTML)
        self.assertIn(">Embed</button>", review.TRIAGE_HTML)
        self.assertIn('data-play-video>Play</button>', review.TRIAGE_HTML)
        self.assertNotIn("Generate Synopsis", review.TRIAGE_HTML)
        self.assertNotIn("Open detailed review", review.TRIAGE_HTML)
        self.assertNotIn("AI: ", review.TRIAGE_HTML)
        self.assertNotIn("AI: ", review.REVIEW_HTML)
        self.assertNotIn(">Reset</button>", review.TRIAGE_HTML)
        self.assertNotIn("Return to queue", review.TRIAGE_HTML)
        self.assertIn('<option value="not_triaged">Not triaged</option>', review.TRIAGE_HTML)
        self.assertIn('<option value="problems">Problems to resolve</option>', review.TRIAGE_HTML)
        self.assertIn('data-keep-copy class="keep-copy">Keep this copy</button>', review.TRIAGE_HTML)
        self.assertIn("/api/duplicates/", review.TRIAGE_HTML)
        self.assertNotIn('<option value="unreviewed">', review.TRIAGE_HTML)
        self.assertIn('data-reject class="', review.TRIAGE_HTML)
        self.assertIn(">Reject</button>", review.TRIAGE_HTML)
        self.assertNotIn('data-reject class="danger"', review.TRIAGE_HTML)
        self.assertIn('<option value="rejected">Rejected</option>', review.TRIAGE_HTML)
        self.assertNotIn("Contact sheet", review.TRIAGE_HTML)
        self.assertNotIn('data-output=', review.TRIAGE_HTML)
        self.assertNotIn('class="output-option"', review.TRIAGE_HTML)
        self.assertNotIn("function contactHtml", review.TRIAGE_HTML)
        self.assertNotIn("function openContact", review.TRIAGE_HTML)
        self.assertNotIn("Keep Original", review.TRIAGE_HTML)
        self.assertIn("event.key.toLowerCase()==='s'", review.TRIAGE_HTML)
        self.assertIn("event.key>='1'&&event.key<='5'", review.TRIAGE_HTML)
        self.assertIn("event.key.toLowerCase()==='e'", review.TRIAGE_HTML)
        self.assertIn("event.key.toLowerCase()==='p'", review.TRIAGE_HTML)
        self.assertIn("event.code==='Space'", review.TRIAGE_HTML)
        self.assertIn("playSelected().catch", review.TRIAGE_HTML)
        self.assertIn("event.key==='ArrowLeft'||event.key==='ArrowRight'", review.TRIAGE_HTML)
        self.assertIn("event.shiftKey?1:0.1", review.TRIAGE_HTML)
        self.assertIn("if(event.key==='.')", review.TRIAGE_HTML)
        self.assertNotIn("event.key.toLowerCase()==='c'", review.TRIAGE_HTML)
        self.assertIn("function toggledTriageStatus", review.TRIAGE_HTML)
        self.assertIn("toggledTriageStatus(item,'detail_wanted')", review.TRIAGE_HTML)
        self.assertIn("toggledTriageStatus(item,'summary_wanted')", review.TRIAGE_HTML)
        self.assertIn("toggledTriageStatus(item,'no_detail')", review.TRIAGE_HTML)
        self.assertIn("Use original — already short", review.TRIAGE_HTML)
        self.assertIn('id="shortcutWarning"', review.TRIAGE_HTML)
        self.assertIn("function showShortcutWarning", review.TRIAGE_HTML)
        self.assertIn("let currentIndex=-1", review.TRIAGE_HTML)
        hover_handler = review.TRIAGE_HTML.split(
            "grid.addEventListener('pointerover'", 1
        )[1].split("grid.addEventListener('pointerout'", 1)[0]
        self.assertIn("setCurrent(page.items.indexOf(item))", hover_handler)
        keyboard_handler = review.TRIAGE_HTML.split(
            "document.addEventListener('keydown'", 1
        )[1].split("const saved=restoreState()", 1)[0]
        self.assertIn("showShortcutWarning()", keyboard_handler)
        self.assertIn("togglePrivate(item)", keyboard_handler)
        self.assertIn("<kbd>Space</kbd> play/pause", review.TRIAGE_HTML)
        self.assertIn("<kbd>E</kbd> toggle embed", review.TRIAGE_HTML)
        self.assertIn("<kbd>P</kbd> toggle private", review.TRIAGE_HTML)
        self.assertNotIn("<kbd>Space</kbd> contact sheet", review.TRIAGE_HTML)
        self.assertNotIn("<kbd>C</kbd>", review.TRIAGE_HTML)
        self.assertIn("TRIAGE_PAGE_SIZE=48", review.TRIAGE_HTML)
        self.assertNotIn("<video autoplay", review.TRIAGE_HTML)
        self.assertIn('id="selectedPanel"', review.TRIAGE_HTML)
        self.assertIn('id="triagePlayer"', review.TRIAGE_HTML)
        self.assertIn('data-player-stage', review.TRIAGE_HTML)
        player_click_handler = review.TRIAGE_HTML.split(
            "selectedPanel.addEventListener('click'", 1
        )[1].split("selectedPanel.addEventListener('input'", 1)[0]
        self.assertIn("event.target.closest('[data-player-stage]')", player_click_handler)
        self.assertIn("await playSelected()", player_click_handler)
        self.assertIn('id="playerTimeline"', review.TRIAGE_HTML)
        self.assertIn(
            ".player-timeline{width:100%;min-height:24px;margin:0;padding:0;border:0",
            review.TRIAGE_HTML,
        )
        self.assertIn(
            "playerTimelineFollower=createPlaybackTimelineFollower",
            review.TRIAGE_HTML,
        )
        self.assertIn("createVideoScrubController", review.TRIAGE_HTML)
        self.assertIn(">Get screenshot</button>", review.TRIAGE_HTML)
        self.assertIn(">Make key</button>", review.TRIAGE_HTML)
        self.assertIn('id="triageTitle"', review.TRIAGE_HTML)
        self.assertIn('id="triageDescription"', review.TRIAGE_HTML)
        self.assertIn("const metadataDrafts=new Map()", review.TRIAGE_HTML)
        self.assertIn("const metadataSavePromises=new Map()", review.TRIAGE_HTML)
        self.assertIn("metadataDrafts.set(activePlayerVideoId", review.TRIAGE_HTML)
        metadata_blur_handler = review.TRIAGE_HTML.split(
            "selectedPanel.addEventListener('focusout'", 1
        )[1].split("selectedPanel.addEventListener('pointerdown'", 1)[0]
        self.assertIn("saveSelectedMetadata(false)", metadata_blur_handler)
        self.assertIn("/capture-frame", review.TRIAGE_HTML)
        self.assertIn("/main", review.TRIAGE_HTML)
        self.assertIn('aria-pressed="', review.TRIAGE_HTML)
        self.assertIn('id="showPrivate" type="checkbox"', review.TRIAGE_HTML)
        self.assertIn('data-private', review.TRIAGE_HTML)
        self.assertIn('.privacy-button{margin-left:', review.TRIAGE_HTML)
        self.assertIn('Press Play to prepare this video.', review.TRIAGE_HTML)
        self.assertIn('async function playSelected()', review.TRIAGE_HTML)
        self.assertIn('await prepareSelectedPlayback', review.TRIAGE_HTML)
        self.assertIn('async function playFromBeginning(item)', review.TRIAGE_HTML)
        play_from_beginning = review.TRIAGE_HTML.split(
            "async function playFromBeginning(item)", 1
        )[1].split("function activateItem", 1)[0]
        self.assertIn("setCurrent(page.items.indexOf(item),0)", play_from_beginning)
        self.assertIn("playerController.reset(0)", play_from_beginning)
        self.assertIn("playerController.togglePlayback()", play_from_beginning)
        self.assertIn("if(playerPreparing){playerPreparing=false;++playerSerial}", play_from_beginning)
        card_click_handler = review.TRIAGE_HTML.split(
            "grid.addEventListener('click'", 1
        )[1].split("grid.addEventListener('pointermove'", 1)[0]
        self.assertIn("event.target.closest('[data-play-video]')", card_click_handler)
        self.assertIn("await playFromBeginning(item)", card_click_handler)
        self.assertIn('class="filter-main"', review.TRIAGE_HTML)
        self.assertIn('class="filter-group date-filters"', review.TRIAGE_HTML)
        self.assertIn('.filter-main{display:flex', review.TRIAGE_HTML)
        self.assertIn('class="card-title-row"', review.TRIAGE_HTML)
        self.assertIn('class="card-status-row"', review.TRIAGE_HTML)
        self.assertIn('class="description-indicator"', review.TRIAGE_HTML)
        self.assertIn('data-edit-title', review.TRIAGE_HTML)
        self.assertIn('data-inline-title', review.TRIAGE_HTML)
        self.assertIn('className="inline-title-input"', review.TRIAGE_HTML)
        self.assertIn("function beginInlineTitle", review.TRIAGE_HTML)
        self.assertIn("async function commitInlineTitle", review.TRIAGE_HTML)
        inline_title_handler = review.TRIAGE_HTML.split(
            "grid.addEventListener('keydown'", 1
        )[1].split("grid.addEventListener('focusout'", 1)[0]
        self.assertIn("event.key==='Enter'", inline_title_handler)
        self.assertIn("event.key==='Escape'", inline_title_handler)
        inline_title_blur = review.TRIAGE_HTML.split(
            "grid.addEventListener('focusout'", 1
        )[1].split("selectedPanel.addEventListener('click'", 1)[0]
        self.assertIn("commitInlineTitle(input)", inline_title_blur)
        self.assertIn("saveMetadataForItem(item,false)", review.TRIAGE_HTML)
        self.assertNotIn('<div class="card-head">', review.TRIAGE_HTML)
        update_handler = review.TRIAGE_HTML.split(
            "async function updateItem", 1
        )[1].split("async function requestSummary", 1)[0]
        summary_handler = review.TRIAGE_HTML.split(
            "async function requestSummary", 1
        )[1].split("async function rejectItem", 1)[0]
        self.assertIn("updateCardState(item)", update_handler)
        self.assertNotIn("await load(", update_handler)
        self.assertNotIn("render()", update_handler)
        self.assertNotIn("setCurrent(", update_handler)
        self.assertIn("updateCardState(item)", summary_handler)
        self.assertNotIn("await load(", summary_handler)
        self.assertNotIn("render()", summary_handler)
        self.assertNotIn("setCurrent(", summary_handler)
        reject_handler = review.TRIAGE_HTML.split(
            "async function rejectItem", 1
        )[1].split("async function togglePrivate", 1)[0]
        self.assertIn("applyItemChanges(item", reject_handler)
        self.assertIn("updateCardState(item)", reject_handler)
        self.assertNotIn("await load(", reject_handler)
        self.assertNotIn("render()", reject_handler)
        self.assertNotIn("setCurrent(", reject_handler)
        private_handler = review.TRIAGE_HTML.split(
            "async function togglePrivate", 1
        )[1].split("async function keepDuplicateCopy", 1)[0]
        self.assertIn("const requested=!item.all_private", private_handler)
        self.assertIn("private:requested", private_handler)
        self.assertIn("frame.private=requested", private_handler)
        self.assertIn("updateCardState(item)", private_handler)
        self.assertNotIn("render()", private_handler)
        self.assertNotIn("setCurrent(", private_handler)
        self.assertIn('class="summary-action"', review.TRIAGE_HTML)
        self.assertIn("function currentPageMatchCount()", review.TRIAGE_HTML)
        self.assertIn("page.offset+currentPageMatchCount()", review.TRIAGE_HTML)
        synopsis_poll = review.TRIAGE_HTML.split(
            "async function pollSynopsis", 1
        )[1].split("function currentPageMatchCount", 1)[0]
        self.assertIn("updateCardState(item)", synopsis_poll)
        self.assertNotIn("render()", synopsis_poll)
        self.assertNotIn(
            'class="description">\'+escapeHtml(item.description)',
            review.TRIAGE_HTML,
        )
        self.assertNotIn(
            'selectedPanel.innerHTML=selectedPanelHtml(item);prepareSelectedPlayback',
            review.TRIAGE_HTML,
        )

    def test_synopsis_plan_is_adaptive_bounded_and_skips_short_sources(self) -> None:
        self.assertEqual(review.synopsis_segment_plan(20.0, [2.0, 10.0, 18.0]), [])
        first = review.synopsis_segment_plan(
            600.0, [600.0 * index / 17 for index in range(1, 17)]
        )
        second = review.synopsis_segment_plan(
            600.0, [600.0 * index / 17 for index in range(1, 17)]
        )
        self.assertEqual(first, second)
        self.assertGreaterEqual(len(first), 12)
        self.assertLessEqual(len(first), 16)
        self.assertEqual({item[1] for item in first}, {2.0})
        self.assertGreaterEqual(sum(item[1] for item in first), 24.0)
        self.assertLessEqual(sum(item[1] for item in first), 32.0)
        self.assertLess(first[0][0], 600.0 * 0.15)
        self.assertGreater(first[-1][0], 600.0 * 0.85)

    def test_synopsis_command_preserves_audio_and_crossfades_segments(self) -> None:
        command = review.synopsis_ffmpeg_command(
            Path("/source/example.mov"),
            Path("/output/example-synopsis.mp4"),
            [(10.0, 1.25), (50.0, 1.25)],
            include_audio=True,
        )
        filters = command[command.index("-filter_complex") + 1]
        self.assertIn("libx264", command)
        self.assertIn("yuv420p", command)
        self.assertIn("+faststart", command)
        self.assertIn("aac", command)
        self.assertNotIn("-an", command)
        self.assertIn("[outa]", command)
        self.assertEqual(filters.count("xfade=transition=fade:duration=0.200"), 1)
        self.assertEqual(filters.count("acrossfade=d=0.200:c1=qsin:c2=qsin"), 1)

    def test_synopsis_command_crossfades_silent_sources_without_adding_audio(self) -> None:
        command = review.synopsis_ffmpeg_command(
            Path("/source/example.mov"),
            Path("/output/example-synopsis.mp4"),
            [(10.0, 1.0), (30.0, 1.0), (50.0, 1.0)],
            include_audio=False,
        )
        filters = command[command.index("-filter_complex") + 1]
        self.assertIn("-an", command)
        self.assertNotIn("[outa]", command)
        self.assertEqual(filters.count("xfade=transition=fade:duration=0.200"), 2)
        self.assertNotIn("acrossfade=", filters)

    def test_synopsis_artifact_requires_current_generation_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "summary.mp4"
            output.write_bytes(b"video")
            sidecar = output.with_suffix(".json")
            sidecar.write_text(json.dumps({"schema_version": 1}))
            self.assertFalse(review.synopsis_artifact_is_current(output))
            sidecar.write_text(json.dumps({"schema_version": 2}))
            self.assertFalse(review.synopsis_artifact_is_current(output))
            sidecar.write_text(json.dumps({"schema_version": "invalid"}))
            self.assertFalse(review.synopsis_artifact_is_current(output))
            sidecar.write_text(
                json.dumps({"schema_version": review.SYNOPSIS_SCHEMA_VERSION})
            )
            self.assertTrue(review.synopsis_artifact_is_current(output))

    def test_synopsis_media_url_changes_with_the_generated_version(self) -> None:
        first = review.synopsis_media_url("video/id", "2026-09-27T12:00:00+00:00")
        second = review.synopsis_media_url("video/id", "2026-09-27T12:01:00+00:00")
        self.assertEqual(
            first,
            "/media/synopsis/video%2Fid?v=2026-09-27T12%3A00%3A00%2B00%3A00",
        )
        self.assertNotEqual(first, second)

    def test_review_html_layout_and_player_contracts(self) -> None:
        self.assertIn("Diary output", review.REVIEW_HTML)
        self.assertIn('data-detail-output="screenshots"', review.REVIEW_HTML)
        self.assertIn('data-detail-output="embedded"', review.REVIEW_HTML)
        self.assertIn('data-detail-output="summary"', review.REVIEW_HTML)
        self.assertIn("postVideo('outputs'", review.REVIEW_HTML)
        self.assertIn('loading="lazy" decoding="async"', review.REVIEW_HTML)
        self.assertIn('data-video-private="1"', review.REVIEW_HTML)
        self.assertIn('data-private="1"', review.REVIEW_HTML)
        self.assertIn("--bg:#f6f7f9", review.REVIEW_HTML)
        self.assertIn("color-scheme:light", review.REVIEW_HTML)
        self.assertIn("html,body{height:100%;overflow:hidden}", review.REVIEW_HTML)
        self.assertIn("main{min-height:0;overflow:hidden;padding:12px;max-width:2100px;width:100%;margin:0 auto", review.REVIEW_HTML)
        self.assertIn(".workspace{min-height:0;overflow:hidden;display:grid", review.REVIEW_HTML)
        self.assertIn(".viewer{min-height:0;overflow-x:hidden;overflow-y:auto", review.REVIEW_HTML)
        self.assertIn(".candidates{min-height:0;min-width:0;overflow-x:hidden;overflow-y:auto", review.REVIEW_HTML)
        self.assertIn(".key-view{height:112px", review.REVIEW_HTML)
        self.assertIn('id="sharedPlayer" preload="auto" playsinline', review.REVIEW_HTML)
        self.assertNotIn(".player-still", review.REVIEW_HTML)
        self.assertNotIn("function showPlayerStill", review.REVIEW_HTML)
        self.assertNotIn("poster=", review.REVIEW_HTML)
        self.assertIn("function addCapturedFrameToView", review.REVIEW_HTML)
        self.assertIn("addCapturedFrameToView(created)", review.REVIEW_HTML)
        self.assertIn("const created=await postVideo('capture-frame'", review.REVIEW_HTML)
        captured_update = review.REVIEW_HTML.split("function addCapturedFrameToView", 1)[1].split(
            "function updateFrameDom", 1
        )[0]
        self.assertNotIn("currentTime", captured_update)
        capture_handler = review.REVIEW_HTML.split(
            "if(event.target.closest('[data-capture]'))", 1
        )[1].split("if(event.target.closest('[data-make-key]'))", 1)[0]
        self.assertIn("const captureTime=selectedPlayerTime", capture_handler)
        self.assertIn("time_seconds:captureTime", capture_handler)
        self.assertNotIn("reloadCurrentPreservingView", capture_handler)
        self.assertNotIn("currentTime", capture_handler)
        make_key_handler = review.REVIEW_HTML.split(
            "async function makeCurrentFrameKey", 1
        )[1].split("reviewElement.addEventListener('click'", 1)[0]
        self.assertIn("const keyTime=selectedPlayerTime", make_key_handler)
        self.assertIn("addCapturedFrameToView(created)", make_key_handler)
        self.assertNotIn("reloadCurrentPreservingView", make_key_handler)
        self.assertNotIn("currentTime", make_key_handler)
        self.assertNotIn("player.setAttribute('poster'", review.REVIEW_HTML)
        self.assertIn('id="memoryTitle"', review.REVIEW_HTML)
        self.assertIn('id="memoryDescription"', review.REVIEW_HTML)
        self.assertIn('id="memoryDateTime" type="text"', review.REVIEW_HTML)
        self.assertIn('placeholder="yyyy-mm-dd hhmmss"', review.REVIEW_HTML)
        self.assertNotIn('<button data-save-metadata="1"', review.REVIEW_HTML)
        self.assertIn('data-deselect-all="1"', review.REVIEW_HTML)
        self.assertIn('id="playerTimeline" type="range"', review.REVIEW_HTML)
        self.assertIn(
            ".player-timeline{width:100%;min-height:24px;margin:0;padding:0;border:0",
            review.REVIEW_HTML,
        )
        self.assertIn(
            "playerTimelineFollower=createPlaybackTimelineFollower",
            review.REVIEW_HTML,
        )
        self.assertIn('id="playPauseButton"', review.REVIEW_HTML)
        self.assertIn('id="playbackRate"', review.REVIEW_HTML)
        self.assertIn('aria-label="Playback speed"', review.REVIEW_HTML)
        self.assertIn("const PLAYBACK_RATES=[1,1.25,1.5,1.75,2]", review.REVIEW_HTML)
        self.assertIn("function playbackRateOptions()", review.REVIEW_HTML)
        self.assertIn("if(!PLAYBACK_RATES.includes(rate))return", review.REVIEW_HTML)
        self.assertIn("player.playbackRate=currentPlaybackRate", review.REVIEW_HTML)
        self.assertIn("setPlaybackRate(event.target.value)", review.REVIEW_HTML)
        self.assertIn('data-make-key="1"', review.REVIEW_HTML)
        self.assertIn('id="selectedFrameStrip"', review.REVIEW_HTML)
        self.assertIn("selectedFramesHtml", review.REVIEW_HTML)
        self.assertIn('data-zoom="1"', review.REVIEW_HTML)
        self.assertIn('id="frameZoom"', review.REVIEW_HTML)
        self.assertIn("const ZOOM_HOVER_DELAY_MS=750", review.REVIEW_HTML)
        self.assertIn("scheduleFrameZoom", review.REVIEW_HTML)
        self.assertIn("background:transparent;pointer-events:none", review.REVIEW_HTML)
        self.assertNotIn("data-close-zoom", review.REVIEW_HTML)
        self.assertIn('class="player-navigation"', review.REVIEW_HTML)
        self.assertIn('class="capture-tools"', review.REVIEW_HTML)
        self.assertIn("#playPauseButton{min-width:96px}", review.REVIEW_HTML)
        self.assertIn("event.code==='Space'", review.REVIEW_HTML)
        self.assertIn("event.shiftKey?1:0.1", review.REVIEW_HTML)
        self.assertIn("capture.click()", review.REVIEW_HTML)
        self.assertIn("event.key==='.'", review.REVIEW_HTML)
        self.assertIn("selectedPlayerTime", review.REVIEW_HTML)
        self.assertIn("Selected: ", review.REVIEW_HTML)
        self.assertNotIn("function scrubFrameUrl", review.REVIEW_HTML)
        self.assertNotIn("/media/scrub-frame/", review.REVIEW_HTML)
        self.assertNotIn("playerPreview", review.REVIEW_HTML)
        self.assertNotIn("commitTimelineSeek", review.REVIEW_HTML)
        self.assertIn("function createVideoScrubController", review.REVIEW_HTML)
        self.assertIn("let activeRequest=null", review.REVIEW_HTML)
        self.assertIn("let pendingRequest=null", review.REVIEW_HTML)
        self.assertIn("if(!request.exact&&typeof player.fastSeek==='function')player.fastSeek(request.target)", review.REVIEW_HTML)
        self.assertIn("else player.currentTime=request.target", review.REVIEW_HTML)
        self.assertIn("player.addEventListener('seeked',handleSeeked)", review.REVIEW_HTML)
        self.assertIn("Math.abs(mediaTime-request.target)<=frameTolerance", review.REVIEW_HTML)
        self.assertIn(
            "if(scrubController?.snapshot().phase==='playing'&&!player.seeking)updateSelectedTime(value)",
            review.REVIEW_HTML,
        )
        self.assertIn("Safari did not present the selected frame", review.REVIEW_HTML)
        self.assertNotIn("VIDEO_SEEK_INTERVAL_MS", review.REVIEW_HTML)
        self.assertNotIn("videoSeekInFlight", review.REVIEW_HTML)
        self.assertNotIn("pendingVideoSeek", review.REVIEW_HTML)
        self.assertNotIn("playAfterSeek", review.REVIEW_HTML)
        self.assertIn("function playerVideoUrl", review.REVIEW_HTML)
        self.assertIn("PLAYBACK_SESSION_TOKEN", review.REVIEW_HTML)
        self.assertIn("video.playback_url=playerVideoUrl(video.video_url)", review.REVIEW_HTML)
        self.assertIn('id="playbackProgress"', review.REVIEW_HTML)
        self.assertIn("const PLAYBACK_PROGRESS_DELAY_MS=2000", review.REVIEW_HTML)
        self.assertIn("/api/playback/", review.REVIEW_HTML)
        self.assertIn("async function prepareVideoPlayback", review.REVIEW_HTML)
        self.assertIn("progress_percent", review.REVIEW_HTML)
        self.assertNotIn("URL.createObjectURL", review.REVIEW_HTML)
        self.assertNotIn("URL.revokeObjectURL", review.REVIEW_HTML)
        select_handler = review.REVIEW_HTML.split("function selectPlayerTime", 1)[1].split(
            "function bindPlayer", 1
        )[0]
        self.assertNotIn("currentTime=", select_handler)
        self.assertIn("scrubController?.selectExact(time)", select_handler)
        play_handler = review.REVIEW_HTML.split("function togglePlayback", 1)[1].split(
            "async function loadSummary", 1
        )[0]
        self.assertNotIn("currentTime=", play_handler)
        self.assertIn("function adjustPlayer(delta){scrubController?.step(Number(delta))}", review.REVIEW_HTML)
        self.assertIn(
            "const base=!player.paused&&!player.ended&&Number.isFinite(mediaTime)?mediaTime:selectedTime",
            review.REVIEW_HTML,
        )
        self.assertIn("scrubController.preview(event.target.value)", review.REVIEW_HTML)
        self.assertIn("scrubController.finishScrub(event.target.value)", review.REVIEW_HTML)
        self.assertIn("if(event.repeat)return;adjustPlayer", review.REVIEW_HTML)
        self.assertIn("player.requestVideoFrameCallback(function(_now,metadata)", review.REVIEW_HTML)
        self.assertNotIn("function paintPausedVideoFrame", review.REVIEW_HTML)
        self.assertNotIn("paintingVideoFrame", review.REVIEW_HTML)
        self.assertNotIn("player.dataset.seekNudge", review.REVIEW_HTML)
        self.assertNotIn("navigateQueue(-1)", review.REVIEW_HTML)
        self.assertNotIn("navigateQueue(1)", review.REVIEW_HTML)
        self.assertIn(".selected-mini{width:80px;height:80px", review.REVIEW_HTML)
        self.assertIn(".frame-tools button{font-size:.72rem", review.REVIEW_HTML)
        self.assertIn(".frame.selected{outline:8px solid var(--accent);outline-offset:3px}", review.REVIEW_HTML)
        self.assertIn(".frame.selected .frame-tools{background:#9fe3bd", review.REVIEW_HTML)
        self.assertIn("grid-template-columns:repeat(auto-fill,80px)", review.REVIEW_HTML)
        self.assertIn("height:168px;overflow-x:hidden;overflow-y:auto", review.REVIEW_HTML)
        self.assertIn(".frame.main{outline:8px solid var(--gold)", review.REVIEW_HTML)
        self.assertIn(".key-badge{right:.35rem;background:var(--gold);color:#2b1b00;font-size:.82rem", review.REVIEW_HTML)
        self.assertIn(".frame.ai-selected{box-shadow:0 0 0 4px", review.REVIEW_HTML)
        self.assertIn(".frame.ai-selected.selected,.frame.ai-selected.main{box-shadow:0 0 0 15px", review.REVIEW_HTML)
        self.assertIn('.frame.user-captured:before{content:"";position:absolute;inset:-4px;border:4px', review.REVIEW_HTML)
        self.assertIn('.frame.user-captured.selected:before,.frame.user-captured.main:before{inset:-19px}', review.REVIEW_HTML)
        self.assertIn(".frame.main{outline:8px solid var(--gold);outline-offset:3px}", review.REVIEW_HTML)
        self.assertIn('content:" · red outer = captured"', review.REVIEW_HTML)
        self.assertIn("framesByTime", review.REVIEW_HTML)
        self.assertIn("frame.selected&&!frame.is_main", review.REVIEW_HTML)
        self.assertIn("overflow-y:auto", review.REVIEW_HTML)
        self.assertIn('content:"↺"', review.REVIEW_HTML)
        self.assertIn('content:"↻"', review.REVIEW_HTML)
        self.assertNotIn("defaultPlayerTime", review.REVIEW_HTML)
        self.assertNotIn("seekPlayer", review.REVIEW_HTML)
        self.assertNotIn('data-seek-image="', review.REVIEW_HTML)
        self.assertIn("if(player.readyState>=1)initialize();else player.addEventListener('loadedmetadata',initialize,{once:true})", review.REVIEW_HTML)
        self.assertIn("updateFrameFromResponse", review.REVIEW_HTML)
        self.assertIn("player.pause()", review.REVIEW_HTML)
        self.assertNotIn('data-main="1"', review.REVIEW_HTML)
        self.assertNotIn('id="sharedPlayer" controls', review.REVIEW_HTML)
        self.assertNotIn('id="playerModal"', review.REVIEW_HTML)
        self.assertNotIn("return loadPage()}if(event.target.closest('[data-main]'))", review.REVIEW_HTML)
        self.assertNotIn("autoplay", review.REVIEW_HTML)


if __name__ == "__main__":
    unittest.main()
