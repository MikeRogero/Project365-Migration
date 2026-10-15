from __future__ import annotations

import datetime as dt
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from swarm_dayone_importer import Entry, Photo
from http.server import ThreadingHTTPServer

from swarm_photo_backfill_review import BackfillReviewState, create_handler


class SwarmBackfillBatchTests(unittest.TestCase):
    def test_staged_skip_keeps_pending_target_in_stable_list_until_submit(self) -> None:
        first = self._entry("target:first", "checkin", 2020, venue_id="venue")
        second = self._entry("target:second", "checkin", 2021, venue_id="venue")
        with tempfile.TemporaryDirectory() as temporary:
            state = BackfillReviewState([first, second], Path(temporary))
            first_token = state.entry_payload("pending", 0)["entry"]["entry_token"]

            state.skip(first_token)

            # Pending filters use committed choices only; a staged choice must
            # not shift the target currently being reviewed.
            self.assertEqual(state.store.decisions(), {})
            self.assertEqual(state.store.get("target:first"), None)
            self.assertEqual(set(state.store.staged_decisions()), {"target:first"})
            current = state.entry_payload("pending", 0)
            following = state.entry_payload("pending", 1)
            self.assertEqual(current["count"], 2)
            self.assertEqual(current["entry"]["entry_token"], first_token)
            self.assertEqual(current["index"], 0)
            self.assertEqual(following["index"], 1)
            self.assertEqual(following["entry"]["entry_token"], state.entry_tokens["target:second"])
            self.assertEqual(current["entry"]["decision"]["status"], "skipped")
            self.assertTrue(current["entry"]["staged"])
            self.assertEqual(current["summary"]["staged"], 1)
            self.assertEqual(current["summary"]["staged_skipped"], 1)

            submitted = state.submit()

            self.assertEqual(submitted, 1)
            self.assertEqual(state.store.get("target:first")["status"], "skipped")
            self.assertEqual(state.store.staged_decisions(), {})
            self.assertEqual(state.summary()["skipped"], 1)
            self.assertEqual(state.summary()["staged"], 0)

    def test_staged_selection_is_effective_in_payload_and_survives_reload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photo_path = root / "candidate.jpg"
            photo_path.write_bytes(b"candidate")
            photo = self._photo("candidate-1", "v", 2019, photo_path)
            source = self._entry("source:venue", "venue", 2019, venue_id="venue", photos=[photo])
            target = self._entry("target:checkin", "checkin", 2020, venue_id="venue")
            state = BackfillReviewState([source, target], root / "state")
            payload = state.entry_payload("pending", 0)["entry"]
            candidate_token = payload["local_candidates"][0]["candidate_token"]

            state.select_local(payload["entry_token"], candidate_token)

            self.assertEqual(state.store.decisions(), {})
            self.assertEqual(state.store.staged_decisions()["target:checkin"]["status"], "selected")
            effective = state.entry_payload("pending", 0)["entry"]
            self.assertTrue(effective["staged"])
            self.assertEqual(effective["decision"]["status"], "selected")
            self.assertEqual(effective["selected_candidate"]["preview_url"],
                             payload["local_candidates"][0]["preview_url"])

            reloaded = BackfillReviewState([source, target], root / "state")

            self.assertEqual(reloaded.store.decisions(), {})
            self.assertEqual(reloaded.store.staged_decisions(), state.store.staged_decisions())
            self.assertEqual(reloaded.entry_payload("pending", 0)["entry"]["decision"]["status"],
                             "selected")
            self.assertEqual(reloaded.submit(), 1)
            self.assertEqual(reloaded.store.get("target:checkin")["status"], "selected")

    def test_all_decision_actions_write_drafts_until_explicit_submit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate_path = root / "candidate.jpg"
            candidate_path.write_bytes(b"candidate")
            source = self._entry(
                "source:venue", "venue", 2019, venue_id="venue",
                photos=[self._photo("candidate-1", "v", 2019, candidate_path)],
            )
            targets = [
                self._entry(f"target:{index}", "checkin", 2020 + index,
                            venue_id="venue")
                for index in range(6)
            ]
            state = BackfillReviewState([source, *targets], root / "state")
            tokens = [state.entry_tokens[target.source_key] for target in targets]

            # Establish a committed prior choice to exercise select_previous.
            first_payload = state.entry_payload("pending", 0)["entry"]
            first_candidate = first_payload["local_candidates"][0]["candidate_token"]
            state.select_local(tokens[0], first_candidate)
            self.assertEqual(state.submit(), 1)

            second_payload = state.entry_payload("pending", 0)["entry"]
            previous = second_payload["previous_candidates"][0]
            state.select_previous(tokens[1], previous["repeat_token"])
            jpeg_bytes = b"\xff\xd8\xff" + b"manual image"
            state.save_manual(tokens[2], "manual.jpg", jpeg_bytes)

            class Response:
                headers = mock.Mock()
                headers.get_content_type.return_value = "image/jpeg"

                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return None

                def geturl(self):
                    return "https://images.example/manual.jpg"

                def read(self, _limit):
                    return b"\xff\xd8\xff" + b"remote image"

            with (
                mock.patch("swarm_photo_backfill_review._is_allowed_remote_image_url", return_value=True),
                mock.patch("swarm_photo_backfill_review.urllib.request.build_opener") as build_opener,
            ):
                build_opener.return_value.open.return_value = Response()
                state.save_remote(tokens[3], "https://images.example/manual.jpg")

            with (
                mock.patch.object(state, "commons_candidates", return_value=[{
                    "candidate_id": "commons-1",
                    "image_url": "https://upload.wikimedia.org/example.jpg",
                    "title": "File:Commons image.jpg",
                    "author": "Example author",
                    "license": "CC BY 4.0",
                    "license_url": "https://creativecommons.org/licenses/by/4.0/",
                    "source_url": "https://commons.wikimedia.org/wiki/File:Commons_image.jpg",
                    "original_url": "https://upload.wikimedia.org/example.jpg",
                    "width": 100,
                    "height": 80,
                }]),
                mock.patch("swarm_photo_backfill_review.urllib.request.urlopen") as urlopen,
            ):
                response = Response()
                response.headers.get_content_type.return_value = "image/jpeg"
                urlopen.return_value = response
                state.select_commons(tokens[4], "commons-1")
            state.skip(tokens[5])

            staged = state.store.staged_decisions()
            self.assertEqual(set(staged), {target.source_key for target in targets[1:]})
            self.assertEqual(state.store.decisions(), {"target:0": state.store.get("target:0")})
            self.assertEqual([staged[f"target:{index}"]["status"] for index in range(1, 6)],
                             ["selected"] * 4 + ["skipped"])
            self.assertEqual(state.submit(), 5)
            self.assertEqual(state.store.staged_decisions(), {})

    def test_clear_stages_pending_when_committed_and_removes_uncommitted_draft(self) -> None:
        target = self._entry("target:one", "checkin", 2020, venue_id="venue")
        with tempfile.TemporaryDirectory() as temporary:
            state = BackfillReviewState([target], Path(temporary))
            token = state.entry_tokens[target.source_key]
            state.skip(token)
            state.clear(token)
            self.assertEqual(state.store.staged_decisions(), {})
            self.assertEqual(state.store.decisions(), {})

            state.skip(token)
            self.assertEqual(state.submit(), 1)
            state.clear(token)
            self.assertEqual(state.store.get(target.source_key)["status"], "skipped")
            self.assertEqual(state.store.staged_decisions()[target.source_key]["status"], "pending")
            self.assertEqual(state.entry_payload("skipped", 0)["entry"]["decision"]["status"], "pending")
            self.assertTrue(state.entry_payload("skipped", 0)["entry"]["staged"])
            self.assertEqual(state.submit(), 1)
            self.assertIsNone(state.store.get(target.source_key))

    def test_submit_validation_is_atomic_and_keeps_both_committed_and_staged_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            missing = Photo(
                source_id="missing-photo", created_utc=dt.datetime(2019, 1, 1, tzinfo=dt.UTC),
                canonical_url="https://example.com/missing-photo", related_kind="v",
                related_id="venue", path=root / "missing.jpg", width=10, height=10,
            )
            source = self._entry("source:venue", "venue", 2019, venue_id="venue", photos=[missing])
            first = self._entry("target:first", "checkin", 2020, venue_id="venue")
            second = self._entry("target:second", "checkin", 2021, venue_id="venue")
            state = BackfillReviewState([source, first, second], root / "state")
            first_token = state.entry_tokens[first.source_key]
            second_token = state.entry_tokens[second.source_key]
            state.skip(first_token)
            candidate_token = state.entry_payload("pending", 1)["entry"]["local_candidates"][0]["candidate_token"]
            state.select_local(second_token, candidate_token)
            before_committed = state.store.decisions()
            before_staged = state.store.staged_decisions()

            with self.assertRaises(ValueError):
                state.submit()

            self.assertEqual(state.store.decisions(), before_committed)
            self.assertEqual(state.store.staged_decisions(), before_staged)
            self.assertEqual(state.store.get(first.source_key), None)

    def test_schema_one_decisions_file_contains_only_committed_choices_before_submit(self) -> None:
        target = self._entry("target:one", "checkin", 2020, venue_id="venue")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            decision_path = root / "decisions.json"
            decision_path.write_text(json.dumps({"schema_version": 1, "decisions": {}}), encoding="utf-8")
            state = BackfillReviewState([target], root)
            state.skip(state.entry_tokens[target.source_key])

            disk_payload = json.loads(decision_path.read_text(encoding="utf-8"))

            self.assertEqual(disk_payload["schema_version"], 1)
            self.assertEqual(disk_payload["decisions"], {})
            self.assertEqual(state.store.staged_decisions()[target.source_key]["status"], "skipped")
            self.assertEqual(state.submit(), 1)
            committed = json.loads(decision_path.read_text(encoding="utf-8"))
            self.assertEqual(committed["decisions"][target.source_key]["status"], "skipped")

    def test_resume_chooses_first_unstaged_and_last_when_everything_is_staged(self) -> None:
        targets = [
            self._entry(f"target:{index}", "checkin", 2020 + index, venue_id="venue")
            for index in range(3)
        ]
        with tempfile.TemporaryDirectory() as temporary:
            state = BackfillReviewState(targets, Path(temporary))
            state.skip(state.entry_tokens[targets[0].source_key])

            resumed = state.entry_payload("pending", 0, resume=True)

            self.assertEqual(resumed["index"], 1)
            self.assertEqual(resumed["entry"]["entry_token"], state.entry_tokens[targets[1].source_key])
            state.skip(state.entry_tokens[targets[1].source_key])
            state.skip(state.entry_tokens[targets[2].source_key])

            all_staged = state.entry_payload("pending", 0, resume=True)

            self.assertEqual(all_staged["index"], 2)
            self.assertEqual(all_staged["entry"]["entry_token"], state.entry_tokens[targets[2].source_key])

    def test_authenticated_submit_endpoint_commits_and_wrong_token_preserves_drafts(self) -> None:
        target = self._entry("target:one", "checkin", 2020, venue_id="venue")
        with tempfile.TemporaryDirectory() as temporary:
            state = BackfillReviewState([target], Path(temporary))
            state.skip(state.entry_tokens[target.source_key])
            server = ThreadingHTTPServer(("127.0.0.1", 0), create_handler(state))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                connection.request("POST", "/api/submit", body=b"{}",
                                   headers={"X-Backfill-Token": "wrong"})
                rejected = connection.getresponse()
                rejected_body = json.loads(rejected.read())
                self.assertEqual(rejected.status, 403)
                self.assertIn("error", rejected_body)
                self.assertEqual(state.store.decisions(), {})
                self.assertEqual(state.store.staged_decisions()[target.source_key]["status"], "skipped")

                connection.request("POST", "/api/submit", body=b"{}",
                                   headers={"X-Backfill-Token": state.csrf_token})
                accepted = connection.getresponse()
                accepted_body = json.loads(accepted.read())
                self.assertEqual(accepted.status, 200)
                self.assertEqual(accepted_body["submitted"], 1)
                self.assertEqual(state.store.get(target.source_key)["status"], "skipped")
                self.assertEqual(state.store.staged_decisions(), {})
                connection.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_malformed_staged_decision_fails_without_changing_committed_or_disk_state(self) -> None:
        target = self._entry("target:one", "checkin", 2020, venue_id="venue")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = BackfillReviewState([target], root)
            state.skip(state.entry_tokens[target.source_key])
            malformed = {
                "schema_version": 1,
                "decisions": {"target:one": {"status": "skipped"}},
                "staged_decisions": {"unknown:target": {"status": "selected"}},
            }
            decision_path = root / "decisions.json"
            decision_path.write_text(json.dumps(malformed), encoding="utf-8")

            with self.assertRaises(ValueError):
                state.submit()

            self.assertEqual(json.loads(decision_path.read_text(encoding="utf-8")), malformed)
            self.assertEqual(state.store.get("target:one")["status"], "skipped")
            self.assertEqual(state.store.staged_decisions(), {"unknown:target": {"status": "selected"}})

    def test_non_dictionary_staged_decision_fails_summary_and_submit_without_disk_changes(self) -> None:
        target = self._entry("target:one", "checkin", 2020, venue_id="venue")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = BackfillReviewState([target], root)
            malformed = {
                "schema_version": 1,
                "decisions": {},
                "staged_decisions": {target.source_key: []},
            }
            decision_path = root / "decisions.json"
            decision_path.write_text(json.dumps(malformed), encoding="utf-8")
            before = decision_path.read_bytes()

            with self.assertRaises(ValueError):
                state.summary()
            with self.assertRaises(ValueError):
                state.submit()

            self.assertEqual(decision_path.read_bytes(), before)

    def test_submit_write_failure_preserves_original_committed_and_staged_disk_state(self) -> None:
        targets = [
            self._entry(f"target:{index}", "checkin", 2020 + index, venue_id="venue")
            for index in range(2)
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = BackfillReviewState(targets, root)
            state.skip(state.entry_tokens[targets[0].source_key])
            self.assertEqual(state.submit(), 1)
            state.skip(state.entry_tokens[targets[1].source_key])
            decision_path = root / "decisions.json"
            before_bytes = decision_path.read_bytes()
            before_payload = state.store._payload()

            with mock.patch.object(state.store, "_write", side_effect=OSError("disk full")):
                with self.assertRaisesRegex(OSError, "disk full"):
                    state.submit()

            self.assertEqual(decision_path.read_bytes(), before_bytes)
            self.assertEqual(state.store._payload(), before_payload)
            self.assertEqual(state.store.get("target:0")["status"], "skipped")
            self.assertNotIn("target:1", state.store.decisions())
            self.assertEqual(state.store.staged_decisions()["target:1"]["status"], "skipped")

    @staticmethod
    def _photo(source_id: str, related_kind: str, year: int, path: Path) -> Photo:
        return Photo(
            source_id=source_id,
            created_utc=dt.datetime(year, 1, 1, tzinfo=dt.UTC),
            canonical_url=f"https://example.com/{source_id}",
            related_kind=related_kind,
            related_id="venue",
            path=path,
            width=100,
            height=80,
        )

    @staticmethod
    def _entry(source_key: str, kind: str, year: int, *, venue_id: str,
               photos: list[Photo] | None = None) -> Entry:
        return Entry(
            source_key=source_key,
            kind=kind,
            created_utc=dt.datetime(year, 1, 1, tzinfo=dt.UTC),
            offset_minutes=0,
            title="Example venue",
            body="",
            latitude=1.0,
            longitude=2.0,
            venue_id=venue_id,
            photos=list(photos or []),
        )


if __name__ == "__main__":
    unittest.main()
