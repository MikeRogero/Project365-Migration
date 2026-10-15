from __future__ import annotations

import datetime as dt
import hashlib
import json
import shutil
import sqlite3
import tempfile
import unittest
import zipfile
from collections import Counter
from pathlib import Path

from swarm_dayone_importer import (
    Entry,
    Photo,
    _build_entries,
    _checkin_people_tags,
    _entry_review_class,
    _resolve_people_mappings,
    _review_sample_entries,
    _source_utc_datetime,
    build_package,
    verify_package,
)
from swarm_media_workflow import SwarmMediaStore


class SwarmDayOneImporterTests(unittest.TestCase):
    def test_source_timestamp_is_an_absolute_instant(self) -> None:
        self.assertEqual(
            _source_utc_datetime("2024-01-02T12:00:00+08:00", "createdAt").isoformat(),
            "2024-01-02T04:00:00+00:00",
        )

    def test_checkin_people_tags_preserve_other_people_and_ignore_invalid_entities(self) -> None:
        self.assertEqual(
            _checkin_people_tags({
                "shout": "with Jordan",
                "entities": [{"id": "legacy", "indices": [5, 11], "type": "user"}],
            }, {"user:legacy": "Jordan Example"}),
            ["person:Jordan Example"],
        )
        self.assertEqual(
            _checkin_people_tags({
                "shout": "with Alex Example",
                "entities": [{
                    "id": "facebook-1",
                    "indices": [5, 17],
                    "type": "facebookUser",
                }],
            }, {"facebookUser:facebook-1": "Alex Canonical"}),
            ["person:Alex Canonical"],
        )
        self.assertEqual(
            _checkin_people_tags({
                "shout": "with Alex Example",
                "entities": [{
                    "id": "facebook-1",
                    "indices": [5, 17],
                    "type": "facebookUser",
                }],
            }),
            [],
        )
        self.assertEqual(
            _checkin_people_tags({
                "shout": "Thanks Jordan",
                "entities": [{"id": "example-person", "indices": [7, 13], "type": "user"}],
            }),
            [],
        )

    def test_unknown_companion_is_prompted_once_and_mapping_is_reused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = self._source(base)
            self._write_json(root / "checkins1.json", {"count": 2, "items": [
                {
                    "id": "one", "createdAt": "2024-01-01 10:00:00.000000",
                    "timeZoneOffset": 0, "shout": "with Brad",
                    "entities": [{
                        "id": "facebook-1", "indices": [5, 9], "type": "facebookUser",
                    }],
                    "lat": 0.0, "lng": 0.0,
                },
                {
                    "id": "two", "createdAt": "2024-01-02 10:00:00.000000",
                    "timeZoneOffset": 0, "shout": "Coffee with Brad",
                    "entities": [{
                        "id": "facebook-1", "indices": [12, 16], "type": "facebookUser",
                    }],
                    "lat": 0.0, "lng": 0.0,
                },
            ]})
            mapping_path = base / "output" / "swarm_people_mappings.json"
            prompts: list[str] = []

            entries, _, _, unresolved_metrics = _build_entries(root)
            self.assertEqual(unresolved_metrics["checkin_people_unresolved_identity_count"], 1)
            self.assertTrue(all(not entry.people_tags for entry in entries))

            mappings = _resolve_people_mappings(
                root,
                mapping_path,
                lambda message: prompts.append(message) or "People/Bradley Owens",
            )

            self.assertEqual(prompts, [
                'Swarm companion "Brad" is not mapped. Enter the matching digiKam People name: '
            ])
            self.assertEqual(mappings, {"facebookUser:facebook-1": "Bradley Owens"})
            payload = json.loads(mapping_path.read_text(encoding="utf-8"))
            self.assertEqual(payload, {
                "schema_version": 1,
                "mappings": {
                    "facebookUser:facebook-1": {
                        "swarm_names": ["Brad"],
                        "digikam_name": "Bradley Owens",
                    },
                },
            })
            summary = build_package(
                root,
                base / "packages",
                people_mappings=mappings,
            )
            with zipfile.ZipFile(summary["package_path"]) as archive:
                entries = json.loads(archive.read("Journal.json"))["entries"]
            self.assertEqual(len(entries), 2)
            self.assertTrue(all("person:Bradley Owens" in entry["tags"] for entry in entries))
            self.assertEqual(
                _resolve_people_mappings(
                    root,
                    mapping_path,
                    lambda _message: self.fail("saved mapping should avoid another prompt"),
                ),
                mappings,
            )

    def test_known_companion_is_recorded_without_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = self._source(base)
            self._write_json(root / "checkins1.json", {"count": 1, "items": [{
                "id": "one", "createdAt": "2024-01-01 10:00:00.000000",
                "timeZoneOffset": 0, "shout": "with Jordan",
                "entities": [{"id": "example-person", "indices": [5, 11], "type": "user"}],
                "lat": 0.0, "lng": 0.0,
            }]})
            mapping_path = base / "output" / "swarm_people_mappings.json"

            mapping_path.parent.mkdir(parents=True)
            self._write_json(mapping_path, {
                "schema_version": 1,
                "mappings": {"user:example-person": {
                    "swarm_names": ["Jordan"], "digikam_name": "Jordan Example",
                }},
            })
            mappings = _resolve_people_mappings(
                root,
                mapping_path,
                lambda _message: self.fail("known mapping should not prompt"),
            )

            self.assertEqual(mappings, {"user:example-person": "Jordan Example"})
            payload = json.loads(mapping_path.read_text(encoding="utf-8"))
            self.assertEqual(
                payload["mappings"]["user:example-person"],
                {"swarm_names": ["Jordan"], "digikam_name": "Jordan Example"},
            )

    def test_unknown_companion_without_prompt_fails_before_writing_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = self._source(base)
            self._write_json(root / "checkins1.json", {"count": 1, "items": [{
                "id": "one", "createdAt": "2024-01-01 10:00:00.000000",
                "timeZoneOffset": 0, "shout": "with Unknown",
                "entities": [{"id": "user-1", "indices": [5, 12], "type": "user"}],
                "lat": 0.0, "lng": 0.0,
            }]})
            mapping_path = base / "output" / "swarm_people_mappings.json"

            with self.assertRaisesRegex(ValueError, "requires interactive name mapping"):
                _resolve_people_mappings(root, mapping_path, None)
            self.assertFalse(mapping_path.exists())

            source_mapping = root / "swarm_people_mappings.json"
            with self.assertRaisesRegex(ValueError, "must be outside the Swarm export"):
                _resolve_people_mappings(root, source_mapping, lambda _message: "Unknown Person")
            self.assertFalse(source_mapping.exists())
        self.assertEqual(
            _checkin_people_tags({
                "shout": "with Alex",
                "entities": [
                    {"id": "other", "indices": [5, 99], "type": "user"},
                    {"id": "topic", "indices": [5, 9], "type": "topic"},
                ],
            }),
            [],
        )

    def test_groups_exact_photos_and_preserves_location_text_and_links(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._source(Path(temporary))
            self._write_json(root / "checkins1.json", {
                "count": 1,
                "items": [{
                    "id": "checkin-1",
                    "createdAt": "2024-01-02 12:00:00.000000",
                    "timeZoneOffset": 480,
                    "canonicalUrl": "https://app.foursquare.com/share/checkin/checkin-1",
                    "shout": "A journal note with Jordan",
                    "entities": [{
                        "id": "example-person",
                        "indices": [20, 26],
                        "type": "user",
                    }],
                    "venue": {
                        "id": "venue-1",
                        "name": "Example Cafe",
                        "url": "https://app.foursquare.com/v/example/venue-1",
                    },
                    "comments": {"count": 2},
                    "lat": 25.04,
                    "lng": 121.51,
                }],
            })
            self._write_json(root / "photos1.json", {
                "count": 3,
                "items": [
                    self._photo("photo-1", "2024-01-02 12:01:00.000000",
                                "https://app.foursquare.com/checkin/checkin-1", "one.jpg"),
                    self._photo("photo-2", "2024-01-02 12:02:00.000000",
                                "https://app.foursquare.com/checkin/checkin-1", "two.jpg"),
                    self._photo("photo-3", "2024-01-02 12:03:00.000000",
                                "https://app.foursquare.com/v/venue-1", "three.jpg"),
                ],
            })
            self._write_json(root / "tips.json", {"count": 0, "items": []})
            self._write_json(root / "comments.json", {"count": 0, "items": []})
            for name, payload in (("one.jpg", b"one"), ("two.jpg", b"two"),
                                  ("three.jpg", b"three")):
                (root / "pix" / name).write_bytes(payload)

            summary = build_package(
                root.parent, Path(temporary) / "output",
                people_mappings={"user:example-person": "Jordan Example"},
            )

            with zipfile.ZipFile(summary["package_path"]) as archive:
                journal = json.loads(archive.read("Journal.json"))
                entry = next(item for item in journal["entries"] if "swarm:checkin" in item["tags"])
                venue_photo = next(item for item in journal["entries"]
                                   if "swarm:venue-photo" in item["tags"])
                self.assertEqual(entry["creationDate"], "2024-01-02T12:00:00Z")
                self.assertEqual(entry["timeZone"], "Etc/GMT-8")
                self.assertEqual(entry["location"], {
                    "latitude": 25.04,
                    "longitude": 121.51,
                    "placeName": "Example Cafe",
                    "region": {
                        "center": {"latitude": 25.04, "longitude": 121.51},
                        "radius": 75,
                    },
                })
                self.assertEqual(len(entry["photos"]), 2)
                self.assertEqual(entry["photos"][0]["date"], "2024-01-02T12:01:00Z")
                self.assertEqual(len(venue_photo["photos"]), 1)
                self.assertLess(entry["text"].index("A journal note"),
                                entry["text"].index("dayone-moment://"))
                self.assertLess(entry["text"].rindex("dayone-moment://"),
                                entry["text"].index("_Source:"))
                self.assertIn("[Swarm check-in]", entry["text"])
                self.assertIn("[Foursquare venue]", entry["text"])
                self.assertIn("person:Jordan Example", entry["tags"])
                for photo in entry["photos"]:
                    self.assertIn(f"![](dayone-moment://{photo['identifier']})", entry["text"])
                    member = f"photos/{photo['md5']}.{photo['type']}"
                    self.assertEqual(hashlib.md5(archive.read(member)).hexdigest(), photo["md5"])
            self.assertEqual(summary["entry_count"], 2)
            self.assertEqual(summary["attachment_count"], 3)
            self.assertEqual(summary["explicit_checkin_photos"], 2)
            self.assertEqual(summary["venue_photos"], 1)
            manifest = json.loads(Path(summary["manifest_path"]).read_text(encoding="utf-8"))
            self.assertEqual(manifest["source_counts"]["comment_bodies_not_exported"], 2)
            self.assertTrue(any("comment bodies" in warning for warning in manifest["warnings"]))
            verify_package(Path(summary["package_path"]))

    @unittest.skipUnless(
        shutil.which("magick") or Path("/opt/homebrew/bin/magick").is_file(),
        "ImageMagick is required for visual duplicate reconciliation",
    )
    def test_merges_close_visual_duplicate_into_richer_checkin_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._source(Path(temporary))
            self._write_json(root / "checkins1.json", {"count": 1, "items": [{
                "id": "checkin-1", "createdAt": "2024-01-02 12:00:00.000000",
                "timeZoneOffset": 480,
                "canonicalUrl": "https://app.foursquare.com/c/checkin-1",
                "shout": "Keep this journal note",
                "venue": {"id": "venue-1", "name": "Example Cafe",
                          "url": "https://app.foursquare.com/v/example/venue-1"},
                "lat": 25.04, "lng": 121.51,
            }]})
            explicit = self._photo(
                "photo-explicit", "2024-01-02 12:00:20.000000",
                "https://app.foursquare.com/checkin/checkin-1", "explicit.jpg",
            )
            venue = self._photo(
                "photo-venue", "2024-01-02 12:00:21.000000",
                "https://app.foursquare.com/v/venue-1", "venue.jpg",
            )
            self._write_json(root / "photos1.json", {"count": 2, "items": [venue, explicit]})
            self._write_json(root / "tips.json", {"count": 0, "items": []})
            pixels = bytes(range(256))
            (root / "pix" / "explicit.jpg").write_bytes(b"P5\n16 16\n255\n" + pixels)
            altered = bytearray(pixels)
            altered[42] = min(255, altered[42] + 1)
            (root / "pix" / "venue.jpg").write_bytes(b"P5\n16 16\n255\n" + altered)

            summary = build_package(root, Path(temporary) / "output")

            with zipfile.ZipFile(summary["package_path"]) as archive:
                entries = json.loads(archive.read("Journal.json"))["entries"]
            self.assertEqual(len(entries), 1)
            self.assertIn("swarm:checkin", entries[0]["tags"])
            self.assertIn("Keep this journal note", entries[0]["text"])
            self.assertEqual(len(entries[0]["photos"]), 1)
            self.assertEqual(summary["visually_merged_venue_photos"], 1)
            self.assertEqual(summary["standalone_venue_photo_entries"], 0)
            self.assertEqual(summary["attachment_count"], 1)

    def test_review_sample_has_exact_stratified_classes_and_keeps_whole_entries(self) -> None:
        quotas = {
            "checkin-visual-duplicate": 4,
            "checkin-missing-venue": 3,
            "checkin-multi-photo": 18,
            "checkin-single-photo": 18,
            "checkin-no-photo-comment-count": 10,
            "checkin-no-photo-other": 12,
            "tip-photo-location": 4,
            "tip-no-photo-location": 2,
            "tip-unresolved-location": 9,
            "venue-photo": 8,
            "comment": 12,
        }
        entries: list[Entry] = []
        for class_index, (review_class, count) in enumerate(quotas.items()):
            for index in range(count + 3):
                kind = "checkin"
                title = "Example venue"
                body = ""
                latitude: float | None = 1.0
                longitude: float | None = 2.0
                photo_count = 0
                merged = 0
                reported_comments = 0
                if review_class == "comment":
                    kind, latitude, longitude = "comment", None, None
                elif review_class == "venue-photo":
                    kind, photo_count = "venue-photo", 1
                elif review_class.startswith("tip-"):
                    kind = "tip"
                    photo_count = int(review_class == "tip-photo-location")
                    if review_class == "tip-unresolved-location":
                        latitude, longitude = None, None
                elif review_class == "checkin-visual-duplicate":
                    photo_count, merged = 1, 1
                elif review_class == "checkin-missing-venue":
                    title = "Swarm check-in"
                elif review_class == "checkin-multi-photo":
                    photo_count = 3
                elif review_class == "checkin-single-photo":
                    photo_count = 1
                elif review_class == "checkin-no-photo-comment-count":
                    reported_comments = 2
                photos = [Photo(
                    source_id=f"photo-{class_index}-{index}-{photo_index}",
                    created_utc=dt.datetime(2000 + class_index, 1, 1, tzinfo=dt.UTC),
                    canonical_url="",
                    related_kind="checkin",
                    related_id="",
                    path=Path(f"unused-{class_index}-{index}-{photo_index}.jpg"),
                    width=100,
                    height=80,
                ) for photo_index in range(photo_count)]
                entries.append(Entry(
                    source_key=f"{review_class}:{index}",
                    kind=kind,
                    created_utc=dt.datetime(2000 + class_index, 1, index + 1, tzinfo=dt.UTC),
                    offset_minutes=0,
                    title=title,
                    body=body,
                    latitude=latitude,
                    longitude=longitude,
                    photos=photos,
                    visual_duplicates_merged=merged,
                    reported_comment_count=reported_comments,
                ))

        selected = _review_sample_entries(entries, 100)

        self.assertEqual(len(selected), 100)
        self.assertEqual(len({entry.source_key for entry in selected}), 100)
        self.assertEqual(Counter(_entry_review_class(entry) for entry in selected), quotas)
        self.assertTrue(all(entry.photos in [candidate.photos for candidate in entries]
                            for entry in selected))
        self.assertEqual(
            selected,
            sorted(selected, key=lambda entry: (entry.created_utc, entry.source_key)),
        )

    def test_reviewed_exact_venue_backfill_is_labeled_and_attached_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = self._source(base)
            self._write_json(root / "checkins1.json", {"count": 2, "items": [
                {
                    "id": "target", "createdAt": "2024-01-02 12:00:00.000000",
                    "timeZoneOffset": 480,
                    "canonicalUrl": "https://app.foursquare.com/c/target",
                    "venue": {"id": "venue-1", "name": "Example Cafe",
                              "url": "https://app.foursquare.com/v/example/venue-1"},
                    "lat": 25.04, "lng": 121.51,
                },
                {
                    "id": "source", "createdAt": "2023-01-02 12:00:00.000000",
                    "timeZoneOffset": 480,
                    "canonicalUrl": "https://app.foursquare.com/c/source",
                    "venue": {"id": "venue-1", "name": "Example Cafe",
                              "url": "https://app.foursquare.com/v/example/venue-1"},
                    "lat": 25.04, "lng": 121.51,
                },
            ]})
            self._write_json(root / "photos1.json", {"count": 1, "items": [
                self._photo("photo-source", "2023-01-02 12:01:00.000000",
                            "https://app.foursquare.com/checkin/source", "source.jpg"),
            ]})
            (root / "pix" / "source.jpg").write_bytes(b"source-photo")
            self._write_json(root / "tips.json", {"count": 0, "items": []})
            decisions = base / "backfill" / "decisions.json"
            decisions.parent.mkdir()
            self._write_json(decisions, {"schema_version": 1, "decisions": {
                "checkin:target": {
                    "status": "selected",
                    "candidate": {
                        "kind": "export",
                        "photo_source_id": "photo-source",
                        "source_entry_key": "checkin:source",
                        "source_url": "https://app.foursquare.com/photo/photo-source",
                        "label": "Photo from another check-in at the exact same venue",
                    },
                },
            }})

            summary = build_package(
                root,
                base / "output",
                backfill_decisions=decisions,
            )

            with zipfile.ZipFile(summary["package_path"]) as archive:
                entries = json.loads(archive.read("Journal.json"))["entries"]
            target = next(entry for entry in entries if "swarm:representative-photo" in entry["tags"])
            self.assertEqual(len(target["photos"]), 1)
            self.assertIn("Representative venue image", target["text"])
            self.assertIn("not captured during this visit", target["text"])
            self.assertEqual(summary["representative_backfills"], 1)
            self.assertIn("_v5_", Path(summary["package_path"]).name)

    def test_version_five_applies_persistent_photo_order_and_people(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = self._source(base)
            self._write_json(root / "checkins1.json", {"count": 1, "items": [{
                "id": "checkin-1", "createdAt": "2024-01-02 12:00:00.000000",
                "timeZoneOffset": 480,
                "canonicalUrl": "https://app.foursquare.com/c/checkin-1",
                "venue": {"id": "venue-1", "name": "Example Cafe"},
                "lat": 25.04, "lng": 121.51,
            }]})
            self._write_json(root / "photos1.json", {"count": 2, "items": [
                self._photo("one", "2024-01-02 12:01:00.000000",
                            "https://app.foursquare.com/checkin/checkin-1", "one.jpg"),
                self._photo("two", "2024-01-02 12:02:00.000000",
                            "https://app.foursquare.com/checkin/checkin-1", "two.jpg"),
            ]})
            self._write_json(root / "tips.json", {"count": 0, "items": []})
            (root / "pix" / "one.jpg").write_bytes(b"one")
            (root / "pix" / "two.jpg").write_bytes(b"two")
            entries, _, _, _ = _build_entries(root)
            store = SwarmMediaStore(base / "review.sqlite3")
            store.sync(entries)
            store.save_order("checkin:checkin-1", ["two", "one"])
            with sqlite3.connect(store.path) as connection:
                connection.execute(
                    "INSERT INTO swarm_photo_people(photo_key, canonical_name, source) VALUES (?, ?, ?)",
                    ("checkin:checkin-1\0two", "Alex Example", "digikam_xmp"),
                )
                connection.commit()

            summary = build_package(
                root, base / "output", media_workflow_database=store.path
            )

            with zipfile.ZipFile(summary["package_path"]) as archive:
                item = json.loads(archive.read("Journal.json"))["entries"][0]
            self.assertIn("_v6_", Path(summary["package_path"]).name)
            self.assertEqual(item["photos"][0]["md5"], hashlib.md5(b"two").hexdigest())
            self.assertEqual(item["photos"][1]["md5"], hashlib.md5(b"one").hexdigest())
            self.assertIn("person:Alex Example", item["tags"])
            self.assertEqual(summary["workflow_reordered_entry_count"], 1)

    def test_exports_self_authored_comment_without_guessing_its_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._source(Path(temporary))
            self._write_json(root / "checkins1.json", {"count": 1, "items": [{
                "id": "checkin-1", "createdAt": "2024-01-02 12:00:00.000000",
                "timeZoneOffset": 480, "canonicalUrl": "https://app.foursquare.com/c/checkin-1",
                "comments": {"count": 2}, "lat": 25.04, "lng": 121.51,
            }]})
            self._write_json(root / "photos1.json", {"count": 0, "items": []})
            self._write_json(root / "tips.json", {"count": 0, "items": []})
            self._write_json(root / "users.json", {"self": {"id": "7"}})
            self._write_json(root / "comments.json", {"count": 2, "items": [
                {"userId": 7, "time": "2024-01-02 12:05:00.000000", "comment": "My reply"},
                {"userId": 7, "time": "2024-01-02 12:06:00.000000", "comment": ""},
            ]})

            summary = build_package(root, Path(temporary) / "output")

            with zipfile.ZipFile(summary["package_path"]) as archive:
                entries = json.loads(archive.read("Journal.json"))["entries"]
            checkin = next(item for item in entries if "swarm:checkin" in item["tags"])
            comment = next(item for item in entries if "swarm:comment" in item["tags"])
            self.assertIn("reports 2 comments", checkin["text"])
            self.assertIn("My reply", comment["text"])
            self.assertIn("Original check-in was not identified", comment["text"])
            self.assertNotIn("location", comment)
            self.assertIn("swarm:comment-parent-unresolved", comment["tags"])
            self.assertEqual(summary["exported_comment_entries"], 1)
            self.assertEqual(summary["empty_comment_records"], 1)

    def test_tip_photo_stays_with_tip_and_uses_same_venue_location_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._source(Path(temporary))
            self._write_json(root / "checkins1.json", {"count": 1, "items": [{
                "id": "checkin-1", "createdAt": "2024-02-03 09:00:00.000000",
                "timeZoneOffset": -300, "canonicalUrl": "https://app.foursquare.com/c/checkin-1",
                "venue": {"id": "venue-1", "name": "Example Place",
                          "url": "https://app.foursquare.com/v/example/venue-1"},
                "lat": 40.7, "lng": -74.0,
            }]})
            self._write_json(root / "tips.json", {"count": 1, "items": [{
                "id": "tip-1", "createdAt": "2024-02-03 09:30:00.000000",
                "text": "A useful tip", "canonicalUrl": "https://app.foursquare.com/item/tip-1",
                "venue": {"id": "venue-1", "name": "Example Place",
                          "url": "https://app.foursquare.com/v/example/venue-1"},
            }]})
            self._write_json(root / "photos1.json", {"count": 1, "items": [
                self._photo("photo-1", "2024-02-03 09:31:00.000000",
                            "https://app.foursquare.com/item/tip-1", "tip.jpg"),
            ]})
            (root / "pix" / "tip.jpg").write_bytes(b"tip-photo")

            summary = build_package(root, Path(temporary) / "output")

            with zipfile.ZipFile(summary["package_path"]) as archive:
                entries = json.loads(archive.read("Journal.json"))["entries"]
            tip = next(entry for entry in entries if "swarm:tip" in entry["tags"])
            self.assertEqual(len(tip["photos"]), 1)
            self.assertEqual(tip["location"]["placeName"], "Example Place")
            self.assertIn("A useful tip", tip["text"])
            self.assertIn("[Foursquare tip]", tip["text"])
            self.assertEqual(summary["tip_locations_from_checkins"], 1)

    def test_late_venue_photo_becomes_one_dated_entry_instead_of_guessing_a_checkin(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._source(Path(temporary))
            self._write_json(root / "checkins1.json", {"count": 1, "items": [{
                "id": "checkin-1", "createdAt": "2024-01-01 10:00:00.000000",
                "timeZoneOffset": 60, "canonicalUrl": "https://app.foursquare.com/c/checkin-1",
                "venue": {"id": "venue-1", "name": "Example Venue",
                          "url": "https://app.foursquare.com/v/example/venue-1"},
                "lat": 48.8, "lng": 2.3,
            }]})
            self._write_json(root / "tips.json", {"count": 0, "items": []})
            self._write_json(root / "photos1.json", {"count": 2, "items": [
                self._photo("photo-1", "2024-01-04 11:00:00.000000",
                            "https://app.foursquare.com/v/venue-1", "one.jpg"),
                self._photo("photo-2", "2024-01-04 11:01:00.000000",
                            "https://app.foursquare.com/v/venue-1", "two.jpg"),
            ]})
            (root / "pix" / "one.jpg").write_bytes(b"one")
            (root / "pix" / "two.jpg").write_bytes(b"two")

            summary = build_package(root, Path(temporary) / "output")

            with zipfile.ZipFile(summary["package_path"]) as archive:
                entries = json.loads(archive.read("Journal.json"))["entries"]
            venue_photo = next(entry for entry in entries if "swarm:venue-photo" in entry["tags"])
            self.assertEqual(len(venue_photo["photos"]), 2)
            self.assertEqual(venue_photo["location"]["placeName"], "Example Venue")
            self.assertEqual(summary["standalone_venue_photo_entries"], 1)

    def test_missing_media_fails_before_publishing_a_zip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._source(Path(temporary))
            self._write_json(root / "checkins1.json", {"count": 1, "items": [{
                "id": "checkin-1", "createdAt": "2024-01-01 10:00:00.000000",
                "timeZoneOffset": 0, "canonicalUrl": "https://app.foursquare.com/c/checkin-1",
                "lat": 0.0, "lng": 0.0,
            }]})
            self._write_json(root / "tips.json", {"count": 0, "items": []})
            self._write_json(root / "photos1.json", {"count": 1, "items": [
                self._photo("photo-1", "2024-01-01 10:01:00.000000",
                            "https://app.foursquare.com/checkin/checkin-1", "missing.jpg"),
            ]})
            output = Path(temporary) / "output"

            with self.assertRaisesRegex(ValueError, "Missing local photo"):
                build_package(root, output)
            self.assertFalse(list(output.glob("*.zip")) if output.exists() else [])

    def test_refuses_to_split_an_entry_with_more_than_thirty_photos(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._source(Path(temporary))
            self._write_json(root / "checkins1.json", {"count": 1, "items": [{
                "id": "checkin-1", "createdAt": "2024-01-01 10:00:00.000000",
                "timeZoneOffset": 0, "canonicalUrl": "https://app.foursquare.com/c/checkin-1",
                "lat": 0.0, "lng": 0.0,
            }]})
            self._write_json(root / "tips.json", {"count": 0, "items": []})
            photos = []
            for index in range(31):
                filename = f"photo-{index}.jpg"
                photos.append(self._photo(
                    f"photo-{index}", "2024-01-01 10:01:00.000000",
                    "https://app.foursquare.com/checkin/checkin-1", filename,
                ))
                (root / "pix" / filename).write_bytes(filename.encode())
            self._write_json(root / "photos1.json", {"count": len(photos), "items": photos})

            with self.assertRaisesRegex(ValueError, "refusing to split one diary entry"):
                build_package(root, Path(temporary) / "output")

    @staticmethod
    def _source(parent: Path) -> Path:
        root = parent / "wrapper" / "export"
        (root / "pix").mkdir(parents=True)
        return root

    @staticmethod
    def _photo(photo_id: str, created: str, related: str, filename: str) -> dict[str, object]:
        return {
            "id": photo_id,
            "createdAt": created,
            "canonicalUrl": f"https://app.foursquare.com/photo/{photo_id}",
            "relatedItemUrl": related,
            "fullUrl": f"https://fastly.4sqi.net/img/general/original/{filename}",
            "width": 100,
            "height": 80,
        }

    @staticmethod
    def _write_json(path: Path, value: object) -> None:
        path.write_text(json.dumps(value), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
