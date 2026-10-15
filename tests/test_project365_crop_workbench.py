from __future__ import annotations

import datetime as dt
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import project365_original_picker as picker
import project365_crop_workbench as workbench
from swarm_dayone_importer import Entry, Photo
from swarm_media_workflow import SwarmMediaStore


class CropWorkbenchProviderTests(unittest.TestCase):
    def test_registry_exposes_provider_descriptors_and_rejects_unknown_ids(self) -> None:
        project = workbench.Project365CropProvider(mock.Mock())
        with tempfile.TemporaryDirectory() as temporary:
            swarm = workbench.SwarmCropProvider(SwarmMediaStore(Path(temporary) / "review.sqlite3"))
            registry = workbench.CropProviderRegistry(providers=[project, swarm])

            descriptors = registry.descriptors()

            self.assertEqual({item["id"] for item in descriptors}, {"project365", "swarm"})
            for descriptor in descriptors:
                self.assertTrue(descriptor["label"])
                self.assertIsInstance(descriptor["capabilities"], (dict, list, tuple, set))
                self.assertIsInstance(descriptor["filters"], (dict, list, tuple))
            self.assertIs(registry.get("project365"), project)
            self.assertIs(registry.get("swarm"), swarm)
            with self.assertRaises(ValueError):
                registry.get("unknown")

    def test_project365_adapter_keeps_save_staged_until_explicit_commit(self) -> None:
        picker_state = mock.Mock()
        picker_state.save_crop.return_value = {"entry_id": "project365:2024-01-02", "crop_has_crop": True}
        picker_state.pending_crop_commits.return_value = {"pending_count": 1}
        picker_state.commit_staged_crops.return_value = {"saved_count": 1, "remaining_count": 0}
        provider = workbench.Project365CropProvider(picker_state)
        crop = {"x": 0, "y": 0, "size": 10, "candidate_width": 20, "candidate_height": 20}

        saved = provider.save("project365:2024-01-02", "/private/original.jpg", crop)

        self.assertTrue(saved["crop_has_crop"])
        picker_state.save_crop.assert_called_once_with(
            entry_id="project365:2024-01-02",
            candidate_path="/private/original.jpg",
            crop=crop,
            source="manual",
        )
        picker_state.commit_staged_crops.assert_not_called()
        self.assertEqual(provider.pending_commits(), {"pending_count": 1})
        self.assertEqual(provider.commit(), {"saved_count": 1, "remaining_count": 0})
        picker_state.commit_staged_crops.assert_called_once_with()

    def test_swarm_adapter_filters_queued_photos_and_omits_absolute_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            first = _photo(base, "ready", _tiny_png())
            second = _photo(base, "missing", _tiny_png())
            entry = _entry([first, second])
            store = SwarmMediaStore(base / "review.sqlite3")
            store.sync([entry])
            store.set_crop_queued(entry.source_key, first.source_id, True)
            store.set_crop_queued(entry.source_key, second.source_id, True)
            provider = workbench.SwarmCropProvider(store)
            crop = {"x": 0, "y": 0, "size": 1, "candidate_width": 1, "candidate_height": 1}
            queued = provider.list_entries("all")
            ready_id = next(row["entry_id"] for row in queued if row["candidate_filename"] == "ready.png")

            with mock.patch("swarm_media_workflow._convert_image_atomically", side_effect=_write_derivative):
                saved = provider.save(ready_id, "", crop)

            all_entries = provider.list_entries("all")
            missing_entries = provider.list_entries("missing")
            confirmed_entries = provider.list_entries("confirmed")

            self.assertEqual({row["entry_id"] for row in all_entries}, {row["entry_id"] for row in queued})
            self.assertEqual([row["entry_id"] for row in missing_entries], [next(row["entry_id"] for row in queued if row["candidate_filename"] == "missing.png")])
            self.assertEqual([row["entry_id"] for row in confirmed_entries], [ready_id])
            self.assertTrue(saved["crop_has_crop"])
            for value in [*all_entries, *missing_entries, *confirmed_entries, provider.detail(ready_id)]:
                self.assertFalse(_contains_absolute_source_path(value))

    def test_swarm_photo_ids_are_opaque_and_disambiguate_same_source_id_in_two_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            first_photo = _photo(base, "one", _tiny_png())
            second_photo = _photo(base, "one", _tiny_png())
            first = _entry([first_photo], source_key="checkin:first")
            second = _entry([second_photo], source_key="checkin:second")
            store = SwarmMediaStore(base / "review.sqlite3")
            store.sync([first, second])
            store.set_crop_queued(first.source_key, first_photo.source_id, True)
            store.set_crop_queued(second.source_key, second_photo.source_id, True)
            provider = workbench.SwarmCropProvider(store)
            rows = provider.list_entries("all")
            first_id = next(row["entry_id"] for row in rows if row["title"] == first.source_key)
            second_id = next(row["entry_id"] for row in rows if row["title"] == second.source_key)
            self.assertNotEqual(first_id, second_id)
            self.assertRegex(first_id, re.compile(r"^swarm:[A-Za-z0-9_-]+$"))
            self.assertRegex(second_id, re.compile(r"^swarm:[A-Za-z0-9_-]+$"))
            crop = {"x": 0, "y": 0, "size": 1, "candidate_width": 1, "candidate_height": 1}

            with mock.patch("swarm_media_workflow._convert_image_atomically", side_effect=_write_derivative):
                provider.save(first_id, "", crop)

            self.assertTrue(provider.detail(first_id)["crop_has_crop"])
            self.assertFalse(provider.detail(second_id)["crop_has_crop"])
            provider.reset(first_id, "")
            self.assertFalse(provider.detail(first_id)["crop_has_crop"])
            self.assertFalse(provider.detail(second_id)["crop_has_crop"])

    def test_swarm_save_is_immediate_reset_preserves_queue_and_foreign_ids_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            photo = _photo(base, "one", _tiny_png())
            entry = _entry([photo])
            store = SwarmMediaStore(base / "review.sqlite3")
            store.sync([entry])
            store.set_crop_queued(entry.source_key, photo.source_id, True)
            provider = workbench.SwarmCropProvider(store)
            crop = {"x": 0, "y": 0, "size": 1, "candidate_width": 1, "candidate_height": 1}
            target_id = provider.list_entries("all")[0]["entry_id"]

            with mock.patch("swarm_media_workflow._convert_image_atomically", side_effect=_write_derivative):
                saved = provider.save(target_id, "", crop)
            self.assertTrue(saved["crop_has_crop"])
            self.assertEqual(len(provider.list_entries("confirmed")), 1)

            reset = provider.reset(target_id, "")

            self.assertFalse(reset["crop_has_crop"])
            self.assertEqual(len(provider.list_entries("missing")), 1)
            with sqlite3.connect(store.path) as connection:
                queued, crop_json, derivative_path, source_sha = connection.execute(
                    "SELECT crop_queued, crop_json, crop_derivative_path, crop_source_sha256 FROM swarm_photos"
                ).fetchone()
            self.assertEqual(queued, 1)
            self.assertEqual(crop_json, "")
            self.assertEqual(derivative_path, "")
            self.assertEqual(source_sha, "")
            with self.assertRaises(ValueError):
                provider.detail("project365:one")
            with self.assertRaises(ValueError):
                provider.save("project365:one", "", crop)
            with self.assertRaises(ValueError):
                provider.reset("project365:one", "")

    def test_crop_html_selects_provider_and_gates_provider_specific_controls(self) -> None:
        html = picker.CROP_HTML

        self.assertIn('id="cropProvider"', html)
        self.assertIn('fetchJson("/api/crop-providers")', html)
        self.assertIn('new URLSearchParams(window.location.search).get("provider")', html)
        self.assertIn('params.set("provider", state.cropProvider)', html)
        self.assertIn("state.cropProvider", html)
        self.assertIn('data-capability="commit"', html)
        self.assertIn('data-capability="reject"', html)
        self.assertIn('data-capability="private"', html)
        self.assertIn('data-capability="estimate"', html)
        self.assertIn("provider.capabilities", html)
        self.assertIn("provider.filters", html)


def _photo(base: Path, source_id: str, payload: bytes) -> Photo:
    path = base / f"{source_id}.png"
    path.write_bytes(payload)
    return Photo(
        source_id=source_id,
        created_utc=dt.datetime(2024, 1, 1, tzinfo=dt.UTC),
        canonical_url=f"https://example.com/{source_id}",
        related_kind="checkin",
        related_id="venue-1",
        path=path,
        width=1,
        height=1,
    )


def _entry(photos: list[Photo], source_key: str = "checkin:one") -> Entry:
    return Entry(
        source_key=source_key,
        kind="checkin",
        created_utc=dt.datetime(2024, 1, 1, tzinfo=dt.UTC),
        offset_minutes=480,
        title=source_key,
        body="",
        latitude=25.0,
        longitude=121.0,
        venue_id="venue-1",
        photos=list(photos),
    )


def _tiny_png() -> bytes:
    return bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000b49444154789c636000020000050001a5f645400000000049454e44ae426082"
    )


def _write_derivative(source: Path, destination: Path, *args: object, **kwargs: object) -> None:
    destination.write_bytes(b"test derivative")


def _contains_absolute_source_path(value: object) -> bool:
    if isinstance(value, dict):
        return any(_contains_absolute_source_path(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_absolute_source_path(item) for item in value)
    return isinstance(value, str) and value.startswith("/") and not value.startswith("/media/")


if __name__ == "__main__":
    unittest.main()
