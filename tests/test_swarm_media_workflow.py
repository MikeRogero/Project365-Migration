from __future__ import annotations

import datetime as dt
import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import HTTPServer
from pathlib import Path

from swarm_dayone_importer import Entry, Photo, _apply_media_workflow
from swarm_media_workflow import (
    SwarmMediaReviewState,
    SwarmMediaStore,
    _order_html,
    create_handler,
)


class SwarmMediaWorkflowTests(unittest.TestCase):
    def test_crop_page_redirects_to_shared_workbench_and_order_page_links_to_swarm_provider(self) -> None:
        order_html = _order_html("csrf")

        with tempfile.TemporaryDirectory() as temporary:
            store = SwarmMediaStore(Path(temporary) / "review.sqlite3")
            state = SwarmMediaReviewState(store)
            server = HTTPServer(("127.0.0.1", 0), create_handler(state))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, request, file_pointer, code, message, headers, new_url):
                    return None

            opener = urllib.request.build_opener(NoRedirect())
            try:
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    opener.open(f"http://127.0.0.1:{server.server_port}/crop", timeout=5)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

        self.assertIn('href="http://127.0.0.1:8766/crop?provider=swarm"', order_html)
        self.assertEqual(raised.exception.code, 302)
        self.assertEqual(raised.exception.headers.get("Location"), "http://127.0.0.1:8766/crop?provider=swarm")
        raised.exception.close()

    def test_review_pages_use_project_style_and_link_back_to_step_twelve(self) -> None:
        order_html = _order_html("csrf")
        self.assertIn("--bg:#f6f6f2", order_html)
        self.assertIn("← Project365 Control Panel", order_html)
        self.assertIn('href="http://127.0.0.1:8766/?step=import_hub"', order_html)
        self.assertIn("/api/reject", order_html)
        self.assertIn("/api/stage-order", order_html)
        self.assertIn("/api/submit-orders", order_html)
        self.assertIn('id="submitOrdersButton"', order_html)
        self.assertIn('id="stagedOrderCount"', order_html)
        self.assertIn("All items", order_html)
        self.assertIn('aria-pressed="${p.crop_queued}"', order_html)
        self.assertIn('aria-pressed="${p.rejected}"', order_html)
        self.assertIn(">Crop queue</button>", order_html)
        self.assertIn(">Reject</button>", order_html)
        self.assertNotIn('type="checkbox"', order_html)
        self.assertIn('onclick="goPrevious()"', order_html)
        self.assertIn("if(revisiting){await load(index+1,false);return}", order_html)
        self.assertIn("revisitFrontier=null;await load(index+1,true)", order_html)

    def test_order_page_uses_explicit_photo_position_drop_targets(self) -> None:
        order_html = _order_html("csrf")

        self.assertIn('class="photo-slot', order_html)
        self.assertIn('data-position="${position}"', order_html)
        self.assertIn('class="slot-hint">${hint}</span>', order_html)
        self.assertIn("'Drop photo here'", order_html)
        self.assertIn(".photo-slot.drop-target", order_html)
        self.assertIn("grid.classList.add('drag-active')", order_html)
        self.assertIn("slot.classList.add('drop-target')", order_html)
        self.assertIn("clearDropTargets", order_html)

    def test_staged_order_survives_reload_without_committing_review(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            first = self._photo(base, "one", b"one")
            second = self._photo(base, "two", b"two")
            entry = self._entry([first, second])
            store = SwarmMediaStore(base / "review.sqlite3")
            store.sync([entry])

            store.stage_order(entry.source_key, [second.source_id, first.source_id])
            reloaded = SwarmMediaStore(store.path)
            state = reloaded.entry_state(entry.source_key)

            self.assertTrue(state["order_staged"])
            self.assertFalse(state["order_reviewed"])
            self.assertEqual(
                [row["photo_source_id"] for row in state["photos"]], ["two", "one"]
            )
            self.assertEqual(reloaded.summary()["pending_order_commit_count"], 1)
            with sqlite3.connect(store.path) as connection:
                rows = connection.execute(
                    "SELECT custom_position FROM swarm_photos ORDER BY photo_source_id"
                ).fetchall()
            self.assertEqual(rows, [(None,), (None,)])

            payload = SwarmMediaReviewState(reloaded).entry_payload(0, skip_staged=True)
            self.assertIsNone(payload["entry"])
            self.assertEqual(payload["count"], 1)

    def test_submit_staged_orders_commits_batch_and_reviews_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            first = self._photo(base, "one", b"one")
            second = self._photo(base, "two", b"two")
            third = self._photo(base, "three", b"three")
            fourth = self._photo(base, "four", b"four")
            first_entry = self._entry([first, second], source_key="checkin:one")
            second_entry = self._entry([third, fourth], source_key="checkin:two")
            store = SwarmMediaStore(base / "review.sqlite3")
            store.sync([first_entry, second_entry])
            store.stage_order(first_entry.source_key, [second.source_id, first.source_id])
            store.stage_order(second_entry.source_key, [fourth.source_id, third.source_id])

            result = store.commit_staged_orders()

            self.assertEqual(result, {"submitted_count": 2, "newly_reviewed_count": 2})
            self.assertEqual(store.entry_keys(include_reviewed=False), [])
            self.assertEqual(store.summary()["pending_order_commit_count"], 0)
            self.assertEqual(
                [row["photo_source_id"] for row in store.entry_state(first_entry.source_key)["photos"]],
                ["two", "one"],
            )
            self.assertEqual(
                [row["photo_source_id"] for row in store.entry_state(second_entry.source_key)["photos"]],
                ["four", "three"],
            )

    def test_staging_advances_position_without_shrinking_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            first = self._photo(base, "one", b"one")
            second = self._photo(base, "two", b"two")
            third = self._photo(base, "three", b"three")
            fourth = self._photo(base, "four", b"four")
            first_entry = self._entry([first, second], source_key="checkin:one")
            second_entry = self._entry([third, fourth], source_key="checkin:two")
            store = SwarmMediaStore(base / "review.sqlite3")
            store.sync([first_entry, second_entry])
            store.stage_order(first_entry.source_key, [first.source_id, second.source_id])

            payload = SwarmMediaReviewState(store).entry_payload(1, skip_staged=True)

            self.assertEqual(payload["index"], 1)
            self.assertEqual(payload["count"], 2)
            self.assertEqual(payload["entry"]["entry_key"], second_entry.source_key)
            self.assertEqual(payload["summary"]["pending_order_entry_count"], 2)
            self.assertEqual(payload["summary"]["pending_order_commit_count"], 1)

    def test_submit_staged_orders_is_atomic_when_one_order_is_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            first = self._photo(base, "one", b"one")
            second = self._photo(base, "two", b"two")
            third = self._photo(base, "three", b"three")
            fourth = self._photo(base, "four", b"four")
            first_entry = self._entry([first, second], source_key="checkin:one")
            second_entry = self._entry([third, fourth], source_key="checkin:two")
            store = SwarmMediaStore(base / "review.sqlite3")
            store.sync([first_entry, second_entry])
            store.stage_order(first_entry.source_key, [second.source_id, first.source_id])
            store.stage_order(second_entry.source_key, [fourth.source_id, third.source_id])
            with sqlite3.connect(store.path) as connection:
                connection.execute(
                    "UPDATE swarm_order_staging SET photo_order_json = ? WHERE entry_key = ?",
                    (json.dumps(["missing"]), second_entry.source_key),
                )
                connection.commit()

            with self.assertRaisesRegex(ValueError, "every current entry photo"):
                store.commit_staged_orders()

            with sqlite3.connect(store.path) as connection:
                reviewed = connection.execute(
                    "SELECT SUM(order_reviewed) FROM swarm_entries"
                ).fetchone()[0]
                positioned = connection.execute(
                    "SELECT COUNT(*) FROM swarm_photos WHERE custom_position IS NOT NULL"
                ).fetchone()[0]
                staged = connection.execute("SELECT COUNT(*) FROM swarm_order_staging").fetchone()[0]
            self.assertEqual((reviewed, positioned, staged), (0, 0, 2))

    def test_completed_order_leaves_default_queue_but_remains_in_all_items(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            first = self._photo(base, "one", b"one")
            second = self._photo(base, "two", b"two")
            entry = self._entry([first, second])
            store = SwarmMediaStore(base / "review.sqlite3")
            store.sync([entry])

            store.complete_order_review(entry.source_key, [second.source_id, first.source_id])

            self.assertEqual(store.entry_keys(include_reviewed=False), [])
            self.assertEqual(store.entry_keys(include_reviewed=True), [entry.source_key])
            self.assertTrue(store.entry_state(entry.source_key)["order_reviewed"])
            self.assertEqual(
                [row["photo_source_id"] for row in store.entry_state(entry.source_key)["photos"]],
                ["two", "one"],
            )

            state = SwarmMediaReviewState(store)
            self.assertIsNone(state.entry_payload(0)["entry"])
            self.assertEqual(state.entry_payload(0, include_reviewed=True)["entry"]["entry_key"], entry.source_key)

            store.sync([entry])
            self.assertEqual(store.entry_keys(include_reviewed=False), [])

    def test_photo_membership_change_reopens_completed_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            first = self._photo(base, "one", b"one")
            second = self._photo(base, "two", b"two")
            entry = self._entry([first, second])
            store = SwarmMediaStore(base / "review.sqlite3")
            store.sync([entry])
            store.complete_order_review(entry.source_key, [first.source_id, second.source_id])
            store.stage_order(entry.source_key, [second.source_id, first.source_id])

            third = self._photo(base, "three", b"three")
            store.sync([self._entry([first, second, third])])

            self.assertEqual(store.entry_keys(include_reviewed=False), [entry.source_key])
            self.assertFalse(store.entry_state(entry.source_key)["order_reviewed"])
            self.assertEqual(store.summary()["pending_order_commit_count"], 0)

    def test_order_and_crop_queue_survive_resync(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            first = self._photo(base, "one", b"one")
            second = self._photo(base, "two", b"two")
            entry = self._entry([first, second])
            store = SwarmMediaStore(base / "review.sqlite3")

            store.sync([entry])
            store.save_order(entry.source_key, [second.source_id, first.source_id])
            store.set_crop_queued(entry.source_key, first.source_id, True)
            third = self._photo(base, "three", b"three")
            store.sync([self._entry([third, first, second])])

            state = store.entry_state(entry.source_key)
            self.assertEqual(
                [row["photo_source_id"] for row in state["photos"]], ["two", "one", "three"]
            )
            queued = {row["photo_source_id"]: row["crop_queued"] for row in state["photos"]}
            self.assertEqual(queued, {"two": False, "one": True, "three": False})

    def test_rejection_survives_resync_and_can_be_restored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            first = self._photo(base, "one", b"one")
            second = self._photo(base, "two", b"two")
            entry = self._entry([first, second])
            store = SwarmMediaStore(base / "review.sqlite3")
            store.sync([entry])

            store.set_rejected(entry.source_key, second.source_id, True)
            store.sync([entry])

            state = store.entry_state(entry.source_key)
            self.assertEqual(
                {row["photo_source_id"]: row["rejected"] for row in state["photos"]},
                {"one": False, "two": True},
            )
            self.assertEqual(store.summary()["rejected_photo_count"], 1)

            store.set_rejected(entry.source_key, second.source_id, False)
            state = store.entry_state(entry.source_key)
            self.assertFalse(next(row for row in state["photos"] if row["photo_source_id"] == "two")["rejected"])

            with self.assertRaisesRegex(ValueError, "Unknown active Swarm photo"):
                store.set_rejected(entry.source_key, "missing", True)

    def test_resync_invalidates_crop_and_people_when_source_bytes_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            photo = self._photo(base, "one", b"one")
            entry = self._entry([photo])
            store = SwarmMediaStore(base / "review.sqlite3")
            store.sync([entry])
            store.set_crop_queued(entry.source_key, photo.source_id, True)
            with sqlite3.connect(store.path) as connection:
                connection.execute(
                    "UPDATE swarm_photos SET crop_json = ?, crop_derivative_path = ?",
                    (json.dumps({"size": 1}), str(base / "old.jpg")),
                )
                connection.execute(
                    "INSERT INTO swarm_photo_people(photo_key, canonical_name, source) VALUES (?, ?, ?)",
                    (f"{entry.source_key}\0{photo.source_id}", "Alex Example", "digikam_xmp"),
                )
                connection.commit()
            photo.path.write_bytes(b"changed")

            store.sync([entry])

            with sqlite3.connect(store.path) as connection:
                crop_json, derivative = connection.execute(
                    "SELECT crop_json, crop_derivative_path FROM swarm_photos"
                ).fetchone()
                people = connection.execute("SELECT COUNT(*) FROM swarm_photo_people").fetchone()[0]
            self.assertEqual(crop_json, "")
            self.assertEqual(derivative, "")
            self.assertEqual(people, 0)

    def test_export_applies_saved_order_derivative_and_people_tags(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            first = self._photo(base, "one", b"one")
            second = self._photo(base, "two", b"two")
            entry = self._entry([first, second])
            entry.people_tags = ["person:Jordan Example"]
            store = SwarmMediaStore(base / "review.sqlite3")
            store.sync([entry])
            store.save_order(entry.source_key, [second.source_id, first.source_id])
            derivative = base / "cropped.jpg"
            derivative.write_bytes(b"cropped")
            with sqlite3.connect(store.path) as connection:
                connection.execute(
                    """
                    UPDATE swarm_photos
                    SET crop_queued = 1, crop_json = ?, crop_derivative_path = ?, crop_source_sha256 = source_sha256
                    WHERE photo_source_id = 'one'
                    """,
                    (json.dumps({"shape": "square", "size": 1}), str(derivative)),
                )
                connection.execute(
                    "INSERT INTO swarm_photo_people(photo_key, canonical_name, source) VALUES (?, ?, ?)",
                    (f"{entry.source_key}\0{second.source_id}", "Alex Example", "digikam_xmp"),
                )
                connection.commit()

            metrics = _apply_media_workflow([entry], store.path)

            self.assertEqual([photo.source_id for photo in entry.photos], ["two", "one"])
            self.assertEqual(entry.photos[1].path, derivative.resolve())
            self.assertEqual(
                entry.people_tags,
                ["person:Alex Example", "person:Jordan Example"],
            )
            self.assertEqual(metrics["workflow_cropped_photo_count"], 1)

    def test_export_omits_rejected_photo_and_preserves_remaining_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            first = self._photo(base, "one", b"same")
            second = self._photo(base, "two", b"same")
            third = self._photo(base, "three", b"three")
            entry = self._entry([first, second, third])
            store = SwarmMediaStore(base / "review.sqlite3")
            store.sync([entry])
            store.save_order(entry.source_key, [third.source_id, second.source_id, first.source_id])
            store.set_rejected(entry.source_key, second.source_id, True)

            metrics = _apply_media_workflow([entry], store.path)

            self.assertEqual([photo.source_id for photo in entry.photos], ["three", "one"])
            self.assertEqual(metrics["workflow_rejected_photo_count"], 1)

    def test_rejected_unfinished_crop_does_not_block_export(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            first = self._photo(base, "one", b"one")
            second = self._photo(base, "two", b"two")
            entry = self._entry([first, second])
            store = SwarmMediaStore(base / "review.sqlite3")
            store.sync([entry])
            store.set_crop_queued(entry.source_key, second.source_id, True)
            store.set_rejected(entry.source_key, second.source_id, True)

            metrics = _apply_media_workflow([entry], store.path)

            self.assertEqual([photo.source_id for photo in entry.photos], ["one"])
            self.assertEqual(metrics["workflow_rejected_photo_count"], 1)

    def test_export_stops_for_queued_unfinished_crop(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            photo = self._photo(base, "one", b"one")
            entry = self._entry([photo])
            store = SwarmMediaStore(base / "review.sqlite3")
            store.sync([entry])
            store.set_crop_queued(entry.source_key, photo.source_id, True)

            with self.assertRaisesRegex(ValueError, "unfinished crop"):
                _apply_media_workflow([entry], store.path)

    def test_version_one_database_migrates_without_losing_saved_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "review.sqlite3"
            with sqlite3.connect(database) as connection:
                connection.executescript(
                    """
                    CREATE TABLE swarm_review_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    INSERT INTO swarm_review_meta VALUES ('schema_version', '1');
                    CREATE TABLE swarm_entries (
                        entry_key TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL,
                        created_utc TEXT NOT NULL, local_date TEXT NOT NULL,
                        active INTEGER NOT NULL, last_seen_at TEXT NOT NULL
                    );
                    CREATE TABLE swarm_photos (
                        photo_key TEXT PRIMARY KEY, entry_key TEXT NOT NULL,
                        photo_source_id TEXT NOT NULL, source_path TEXT NOT NULL,
                        source_sha256 TEXT NOT NULL, width INTEGER, height INTEGER,
                        default_position INTEGER NOT NULL, custom_position INTEGER,
                        crop_queued INTEGER NOT NULL DEFAULT 0, crop_json TEXT NOT NULL DEFAULT '',
                        crop_derivative_path TEXT NOT NULL DEFAULT '',
                        crop_source_sha256 TEXT NOT NULL DEFAULT '',
                        digikam_working_path TEXT NOT NULL DEFAULT '',
                        digikam_source_sha256 TEXT NOT NULL DEFAULT '',
                        active INTEGER NOT NULL, last_seen_at TEXT NOT NULL,
                        UNIQUE(entry_key, photo_source_id)
                    );
                    CREATE TABLE swarm_photo_people (
                        photo_key TEXT NOT NULL, canonical_name TEXT NOT NULL, source TEXT NOT NULL,
                        PRIMARY KEY(photo_key, canonical_name)
                    );
                    """
                )

            SwarmMediaStore(database)

            with sqlite3.connect(database) as connection:
                photo_columns = {row[1] for row in connection.execute("PRAGMA table_info(swarm_photos)")}
                entry_columns = {row[1] for row in connection.execute("PRAGMA table_info(swarm_entries)")}
                version = connection.execute(
                    "SELECT value FROM swarm_review_meta WHERE key = 'schema_version'"
                ).fetchone()[0]
                tables = {
                    row[0] for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
            self.assertIn("rejected", photo_columns)
            self.assertIn("order_reviewed", entry_columns)
            self.assertIn("swarm_order_staging", tables)
            self.assertEqual(version, "4")

    def test_digikam_working_copy_and_exact_sidecar_people_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            photo = self._photo(base, "one", b"one")
            entry = self._entry([photo])
            store = SwarmMediaStore(base / "review.sqlite3")
            store.sync([entry])

            prepared = store.prepare_digikam()
            with sqlite3.connect(store.path) as connection:
                working = Path(connection.execute(
                    "SELECT digikam_working_path FROM swarm_photos"
                ).fetchone()[0])
            self.assertEqual(working.read_bytes(), b"one")
            self.assertNotEqual(working, photo.path)
            self.assertEqual(prepared["copied_count"], 1)
            working.with_suffix(working.suffix + ".xmp").write_text(
                """<x:xmpmeta xmlns:x="adobe:ns:meta/" xmlns:mwg-rs="http://www.metadataworkinggroup.com/schemas/regions/">
                <mwg-rs:Name>Alex Example</mwg-rs:Name></x:xmpmeta>""",
                encoding="utf-8",
            )

            imported = store.import_digikam_people()
            metrics = _apply_media_workflow([entry], store.path)

            self.assertEqual(imported["people_assignment_count"], 1)
            self.assertEqual(entry.people_tags, ["person:Alex Example"])
            self.assertEqual(metrics["workflow_people_tagged_entry_count"], 1)

    @staticmethod
    def _photo(base: Path, source_id: str, payload: bytes) -> Photo:
        path = base / f"{source_id}.jpg"
        path.write_bytes(payload)
        return Photo(
            source_id=source_id,
            created_utc=dt.datetime(2024, 1, 1, tzinfo=dt.UTC),
            canonical_url=f"https://example.com/{source_id}",
            related_kind="checkin",
            related_id="venue-1",
            path=path,
            width=100,
            height=80,
        )

    @staticmethod
    def _entry(photos: list[Photo], *, source_key: str = "checkin:one") -> Entry:
        return Entry(
            source_key=source_key,
            kind="checkin",
            created_utc=dt.datetime(2024, 1, 1, tzinfo=dt.UTC),
            offset_minutes=480,
            title="Example venue",
            body="",
            latitude=25.0,
            longitude=121.0,
            venue_id="venue-1",
            photos=list(photos),
        )


if __name__ == "__main__":
    unittest.main()
