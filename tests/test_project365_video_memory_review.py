from __future__ import annotations

import json
import hashlib
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

import project365_video_memory_review as review


class VideoMemoryReviewTests(unittest.TestCase):
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

    def test_frame_times_are_sixteen_interior_monotonic_samples(self) -> None:
        times = review.frame_times(30.0)

        self.assertEqual(len(times), 16)
        self.assertAlmostEqual(times[0], 30 / 17, places=3)
        self.assertAlmostEqual(times[-1], 30 * 16 / 17, places=3)
        self.assertEqual(times, sorted(times))
        self.assertTrue(all(0 < value < 30 for value in times))

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

            def fake_run(command: list[str], timeout: int = 180) -> subprocess.CompletedProcess[str]:
                commands.append(command)
                Path(command[-1]).write_bytes(b"compatible")
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            with mock.patch.object(review, "_ffmpeg", return_value="ffmpeg"), mock.patch.object(
                review, "_run", side_effect=fake_run
            ):
                first = review.scrub_optimized_video(source, root / "cache", "video-a")
                second = review.scrub_optimized_video(source, root / "cache", "video-a")

            self.assertEqual(first, second)
            self.assertEqual(first.read_bytes(), b"compatible")
            self.assertIn("playback-scrub-v1", str(first))
            self.assertEqual([command[0] for command in commands], ["ffmpeg"])
            command = commands[0]
            self.assertIn("libx264", command)
            self.assertIn("scale='min(960,iw)':-2", command)
            self.assertEqual(command[command.index("-g") + 1], "6")
            self.assertEqual(command[command.index("-keyint_min") + 1], "6")
            self.assertIn("+faststart", command)

    def test_playback_path_falls_back_to_original_when_proxy_generation_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            state = review.VideoMemoryServerState(root)

            with mock.patch.object(
                review, "scrub_optimized_video", side_effect=ValueError("encode failed")
            ):
                self.assertEqual(state.playback_path(source, "video-a"), source)

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
                ("legacy", "/legacy.mov", "f" * 64, 1, 2, "2005-06-07", "filename_date", "", 5.0, "ready", "jpeg", "complete", "", "", 1, 0, "", 0, "now", "now"),
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
            self.assertEqual(saved_metadata["memory_datetime"], "2019-01-03 112233")
            self.assertEqual(saved_metadata["title"], "API title")
            self.assertEqual(accepted["review_status"], "accepted")
            self.assertEqual(accepted["queued"], 1)
            self.assertEqual(queue["entries"][0]["video_id"], "video-api")
            self.assertEqual(queue["entries"][0]["title"], "API title")
            self.assertEqual(detail["description"], "API description")
            self.assertEqual(rejected["review_status"], "rejected")
            self.assertEqual(state.store.queued_render_count(), 0)

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
            state.playback_path = lambda _source, _video_id: source
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
                    "time_seconds": 0.5,
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
            store.set_frame_private(f"{sha}:01", False)
            self.assertFalse(json.loads(output.with_suffix(".json").read_text())["private"])

    def test_pilot_selection_is_deterministic_unique_and_spans_capture_dates(self) -> None:
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
                path = root / f"video-{year}.mov"
                path.write_bytes(b"x")
                sha = f"{offset + 1:064x}"
                connection.execute(
                    """
                    INSERT INTO photo_library_files (
                        path, root, filename, extension, byte_size,
                        filesystem_mtime_utc, filename_dates, media_creation_dates,
                        filesystem_dates, indexed_at, sha256, filesystem_mtime_ns,
                        media_width, media_height, media_duration_seconds
                    ) VALUES (?, ?, ?, '.mov', 1, '', '', '', '', '', ?, 1, 1920, 1080, ?)
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
        self.assertIn("Open in Finder", review.REVIEW_HTML)
        self.assertIn('data-open-in-finder="1"', review.REVIEW_HTML)
        self.assertIn("postVideo('open-in-finder')", review.REVIEW_HTML)
        self.assertIn("month=", review.REVIEW_HTML)
        self.assertIn("const AUTO_OPEN_MONTHS=4", review.REVIEW_HTML)
        self.assertNotIn("month_count=2", review.REVIEW_HTML)
        self.assertIn("review_status", review.REVIEW_HTML)
        self.assertIn('id="queueFilter"', review.REVIEW_HTML)
        self.assertIn('<option value="pending">Queued</option>', review.REVIEW_HTML)
        self.assertIn('<option value="accepted">Accepted</option>', review.REVIEW_HTML)
        self.assertIn('<option value="all">All</option>', review.REVIEW_HTML)
        self.assertIn('id="videoQueue"', review.REVIEW_HTML)
        self.assertIn("groupedQueueEntries", review.REVIEW_HTML)
        self.assertIn("month.count", review.REVIEW_HTML)
        self.assertIn("queueMonthCache", review.REVIEW_HTML)
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
        self.assertIn('id="playPauseButton"', review.REVIEW_HTML)
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
        self.assertIn("if(scrubController?.snapshot().phase!=='playing'||player.seeking)return", review.REVIEW_HTML)
        self.assertIn("Safari did not present the selected frame", review.REVIEW_HTML)
        self.assertNotIn("VIDEO_SEEK_INTERVAL_MS", review.REVIEW_HTML)
        self.assertNotIn("videoSeekInFlight", review.REVIEW_HTML)
        self.assertNotIn("pendingVideoSeek", review.REVIEW_HTML)
        self.assertNotIn("playAfterSeek", review.REVIEW_HTML)
        self.assertIn("function playerVideoUrl", review.REVIEW_HTML)
        self.assertIn("PLAYBACK_SESSION_TOKEN", review.REVIEW_HTML)
        self.assertIn("video.playback_url=playerVideoUrl(video.video_url)", review.REVIEW_HTML)
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
