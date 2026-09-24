from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

from facebook_dayone_importer import (_looks_mojibake, _repair_text, _resolve_facebook_link,
                                      build_package, verify_media_hashes)


class FacebookDayOneImporterTests(unittest.TestCase):
    def test_resolves_facebook_intermediate_link_to_canonical_post(self) -> None:
        intermediate = "https://www.facebook.com/dyi/l/?l=encoded&s=518"
        canonical = "https://www.facebook.com/example/posts/pfbid02ExampleCanonicalPost"
        self.assertEqual(_resolve_facebook_link(intermediate, {intermediate: canonical}), canonical)

    def test_keeps_intermediate_link_when_resolution_fails(self) -> None:
        intermediate = "https://www.facebook.com/dyi/l/?l=encoded&s=518"
        self.assertEqual(_resolve_facebook_link(intermediate, {}), intermediate)

    def test_verified_link_override_is_used_in_package(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "facebook"
            intermediate = "https://www.facebook.com/dyi/l/?l=encoded&s=518"
            canonical = "https://www.facebook.com/example/posts/pfbid123"
            self._write_json(root, "your_posts_1.json", [{"timestamp": 1690000000,
                                                            "url": intermediate}])
            (root.parent / "facebook_post_link_overrides.json").write_text(
                json.dumps({intermediate: canonical}), encoding="utf-8")

            summary = build_package(root, Path(temporary) / "output")
            with zipfile.ZipFile(summary["package_path"]) as archive:
                entry = json.loads(archive.read("Journal.json"))["entries"][0]
            self.assertIn(canonical, entry["text"])
            self.assertNotIn(intermediate, entry["text"])
            self.assertEqual(summary["resolved_post_links"], 1)
            self.assertEqual(summary["unresolved_intermediate_links"], 0)

    def test_suspect_encoding_detection_preserves_original_text(self) -> None:
        self.assertTrue(_looks_mojibake("cafÃ©"))
        self.assertFalse(_looks_mojibake("café"))

    def test_repairs_utf8_misdecoded_as_latin1_without_changing_valid_text(self) -> None:
        damaged = "I donâ\x80\x99t care â\x80\x93 å¤§ç¾\x8eå¥³ç\x94\x9fæ\x97¥å¿«æ¨\x82ï¼\x81"
        self.assertEqual(_repair_text(damaged), "I don’t care – 大美女生日快樂！")
        self.assertEqual(_repair_text("Already correct: don’t; 大美女生日快樂！; café"),
                         "Already correct: don’t; 大美女生日快樂！; café")
        self.assertEqual(_repair_text("broken â€™ and correct 世界"), "broken ’ and correct 世界")
        self.assertEqual(_repair_text("malformed âx and valid café"), "malformed âx and valid café")

    def test_repairs_post_and_attachment_text_in_generated_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "facebook"
            self._write_json(root, "your_posts_1.json", [{
                "timestamp": 1690000000,
                "data": [{"post": "I donâ\x80\x99t care"}],
                "attachments": [{"data": [{"text": "å¤§ç¾\x8eå¥³ç\x94\x9fæ\x97¥å¿«æ¨\x82ï¼\x81"}]}],
            }])
            summary = build_package(root, Path(temporary) / "output")
            with zipfile.ZipFile(summary["package_path"]) as archive:
                entry = json.loads(archive.read("Journal.json"))["entries"][0]
            self.assertIn("I don’t care", entry["text"])
            self.assertIn("大美女生日快樂！", entry["text"])
            self.assertEqual(summary["text_encoding_review_entries"], 0)

    def test_marketplace_listing_is_legible_but_does_not_claim_unlinked_photo(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "facebook"
            self._write_json(root, "your_posts_1.json", [{"timestamp": 1690000000}])
            market = root / "your_activity_across_facebook/facebook_marketplace/items_sold.json"
            market.parent.mkdir(parents=True)
            market.write_text(json.dumps({"items_selling_v2": [{
                "created_timestamp": 1690000100, "updated_timestamp": 1690000200,
                "title": "Sample furniture", "description": "Sample description",
                "price": "NT$100", "category": "Household", "marketplace": "Marketplace",
                "location": {"coordinate": {"latitude": 25.04, "longitude": 121.51}},
            }]}), encoding="utf-8")

            summary = build_package(root, Path(temporary) / "output")
            with zipfile.ZipFile(summary["package_path"]) as archive:
                entries = json.loads(archive.read("Journal.json"))["entries"]
            listing = next(item for item in entries if "facebook:marketplace" in item["tags"])
            self.assertIn("Sample furniture", listing["text"])
            self.assertIn("Sample description", listing["text"])
            self.assertIn("NT$100", listing["text"])
            self.assertEqual(listing["modifiedDate"], "2023-07-22T04:30:00Z")
            self.assertNotIn("photos", listing)
            self.assertEqual(summary["source_counts"]["marketplace_listings"], 1)

    def test_repeated_marketplace_listing_absorbs_nearby_photos_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "facebook"
            self._write_json(root, "your_posts_1.json", [{"timestamp": 1623860000}])
            market = root / "your_activity_across_facebook/facebook_marketplace/items_sold.json"
            market.parent.mkdir(parents=True)
            listing = {"title": "Sample item", "description": "Listing details", "price": "NT$100"}
            market.write_text(json.dumps({"items_selling_v2": [
                {**listing, "created_timestamp": 1623862257},
                {**listing, "created_timestamp": 1623862844},
                {**listing, "created_timestamp": 1623862847},
            ]}), encoding="utf-8")
            media_root = root / "your_activity_across_facebook/posts/media"
            media_root.mkdir(parents=True)
            photos = []
            for index, (stamp, payload) in enumerate(((1623862844, b"first"),
                                                       (1623862847, b"first"),
                                                       (1623862847, b"second"))):
                name = f"market{index}.jpg"
                (media_root / name).write_bytes(payload)
                photos.append({"uri": f"your_activity_across_facebook/posts/media/{name}",
                               "creation_timestamp": stamp})
            photos_path = root / "your_activity_across_facebook/posts/your_uncategorized_photos.json"
            photos_path.write_text(json.dumps({"other_photos_v2": photos}), encoding="utf-8")

            summary = build_package(root, Path(temporary) / "output")
            with zipfile.ZipFile(summary["package_path"]) as archive:
                entries = json.loads(archive.read("Journal.json"))["entries"]
            listings = [item for item in entries if "facebook:marketplace" in item["tags"]]
            self.assertEqual(len(listings), 1)
            self.assertEqual(len(listings[0]["photos"]), 2)
            self.assertIn("Listing details", listings[0]["text"])
            self.assertEqual(len(entries), 2)
            self.assertEqual(summary["source_counts"]["marketplace_listings"], 3)

    @unittest.skipUnless(shutil.which("magick"), "ImageMagick is needed for visual deduplication")
    def test_marketplace_reencoded_photo_is_not_repeated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "facebook"
            self._write_json(root, "your_posts_1.json", [{"timestamp": 1623614000}])
            market = root / "your_activity_across_facebook/facebook_marketplace/items_sold.json"
            market.parent.mkdir(parents=True)
            market.write_text(json.dumps({"items_selling_v2": [{
                "created_timestamp": 1623614570, "title": "Sample listing",
            }]}), encoding="utf-8")
            media_root = root / "your_activity_across_facebook/posts/media"
            media_root.mkdir(parents=True)
            photos = []
            for index, quality in enumerate((95, 75)):
                name = f"listing{index}.jpg"
                subprocess.run(["magick", "-size", "64x64", "gradient:", "-quality", str(quality),
                                str(media_root / name)], check=True, capture_output=True)
                photos.append({"uri": f"your_activity_across_facebook/posts/media/{name}",
                               "creation_timestamp": 1623614506 + index})
            (media_root.parent / "your_uncategorized_photos.json").write_text(
                json.dumps({"other_photos_v2": photos}), encoding="utf-8")

            summary = build_package(root, Path(temporary) / "output")
            with zipfile.ZipFile(summary["package_path"]) as archive:
                entries = json.loads(archive.read("Journal.json"))["entries"]
            listing = next(item for item in entries if "facebook:marketplace" in item["tags"])
            self.assertEqual(len(listing["photos"]), 1)
            self.assertEqual(summary["source_counts"]["marketplace_visual_duplicate_photos"], 1)

    def test_unlinked_comment_does_not_clutter_day_one_timeline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "facebook"
            self._write_json(root, "your_posts_1.json", [{"timestamp": 1690000000, "data": [{"post": "A post"}]}])
            comments = root / "your_activity_across_facebook/comments_and_reactions/comments.json"
            comments.parent.mkdir(parents=True)
            comments.write_text(json.dumps({"comments_v2": [{
                "timestamp": 1690000100, "title": "Commented on a post",
                "data": [{"comment": {"timestamp": 1690000100, "author": "Example Author",
                                      "comment": "I donâ\x80\x99t know"}}],
            }]}), encoding="utf-8")

            summary = build_package(root, Path(temporary) / "output")
            with zipfile.ZipFile(summary["package_path"]) as archive:
                entries = json.loads(archive.read("Journal.json"))["entries"]
            self.assertEqual(len(entries), 1)
            self.assertEqual(summary["source_counts"]["unlinked_comments"], 1)
            self.assertNotIn("Example Author", entries[0]["text"])

    def test_album_photos_share_entry_and_keep_each_caption_and_date(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "facebook"
            self._write_json(root, "your_posts_1.json", [{"timestamp": 1690000000}])
            media_root = root / "your_activity_across_facebook/posts/media"
            media_root.mkdir(parents=True)
            photos = []
            for index, timestamp in enumerate((1690000300, 1690000100, 1690000200)):
                uri = f"your_activity_across_facebook/posts/media/album{index}.jpg"
                (media_root / f"album{index}.jpg").write_bytes(f"photo {index}".encode())
                photos.append({"uri": uri, "creation_timestamp": timestamp,
                               "title": "Sample album", "description": f"Caption {index}"})
            album = root / "your_activity_across_facebook/posts/album/1.json"
            album.parent.mkdir(parents=True)
            album.write_text(json.dumps({"name": "Sample album", "description": "Album note",
                                         "last_modified_timestamp": 1699999999,
                                         "photos": photos, "cover_photo": photos[0]}), encoding="utf-8")

            summary = build_package(root, Path(temporary) / "output")
            with zipfile.ZipFile(summary["package_path"]) as archive:
                entries = json.loads(archive.read("Journal.json"))["entries"]
            albums = [item for item in entries if "facebook:album" in item["tags"]]
            self.assertEqual(len(albums), 1)
            entry = albums[0]
            self.assertEqual(entry["creationDate"], "2023-07-22T04:28:20Z")
            self.assertEqual(len(entry["photos"]), 3)
            self.assertEqual(entry["photos"][0]["date"], "2023-07-22T04:28:20Z")
            self.assertIn("Album: Sample album", entry["text"])
            self.assertIn("Album note", entry["text"])
            for index in range(3):
                self.assertIn(f"Caption {index}", entry["text"])
            self.assertEqual(summary["attachment_count"], 3)

    def test_oversized_album_splits_at_day_one_limit_without_losing_photos(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "facebook"
            self._write_json(root, "your_posts_1.json", [{"timestamp": 1690000000}])
            media_root = root / "your_activity_across_facebook/posts/media"
            media_root.mkdir(parents=True)
            photos = []
            for index in range(31):
                name = f"photo{index}.jpg"
                (media_root / name).write_bytes(name.encode())
                photos.append({"uri": f"your_activity_across_facebook/posts/media/{name}",
                               "creation_timestamp": 1690000100 + index,
                               "description": f"Caption {index}"})
            album = root / "your_activity_across_facebook/posts/album/1.json"
            album.parent.mkdir(parents=True)
            album.write_text(json.dumps({"name": "Large album", "photos": photos,
                                         "last_modified_timestamp": 1699999999}), encoding="utf-8")

            summary = build_package(root, Path(temporary) / "output")
            with zipfile.ZipFile(summary["package_path"]) as archive:
                entries = json.loads(archive.read("Journal.json"))["entries"]
            albums = [item for item in entries if "facebook:album" in item["tags"]]
            self.assertEqual([len(item.get("photos", [])) for item in albums], [30, 1])
            self.assertEqual([item["creationDate"] for item in albums], ["2023-07-22T04:28:20Z"] * 2)
            self.assertIn("Part 1 of 2", albums[0]["text"])
            self.assertIn("Part 2 of 2", albums[1]["text"])
            self.assertEqual(summary["attachment_count"], 31)

    def test_album_caption_enriches_attached_post_without_duplicate_media(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "facebook"
            media_root = root / "your_activity_across_facebook/posts/media"
            media_root.mkdir(parents=True)
            first = "your_activity_across_facebook/posts/media/first.jpg"
            second = "your_activity_across_facebook/posts/media/second.jpg"
            (media_root / "first.jpg").write_bytes(b"first")
            (media_root / "second.jpg").write_bytes(b"second")
            self._write_json(root, "your_posts_1.json", [{"timestamp": 1690000000,
                "data": [{"post": "Trip post"}],
                "attachments": [{"data": [{"media": {"uri": first}}]}]}])
            album = root / "your_activity_across_facebook/posts/album/1.json"
            album.parent.mkdir(parents=True)
            album.write_text(json.dumps({"name": "Trip", "photos": [
                {"uri": first, "creation_timestamp": 1690000100, "description": "First caption"},
                {"uri": second, "creation_timestamp": 1690000200, "description": "Second caption"},
            ]}), encoding="utf-8")

            summary = build_package(root, Path(temporary) / "output")
            with zipfile.ZipFile(summary["package_path"]) as archive:
                entries = json.loads(archive.read("Journal.json"))["entries"]
            post = next(item for item in entries if "facebook:post" in item["tags"])
            grouped = next(item for item in entries if "facebook:album" in item["tags"])
            self.assertIn("First caption", post["text"])
            self.assertIn("Second caption", grouped["text"])
            self.assertEqual(len(post["photos"]), 1)
            self.assertEqual(len(grouped["photos"]), 1)
            self.assertEqual(summary["attachment_count"], 2)

    def test_post_over_media_limit_is_split_without_repeating_post_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "facebook"
            media_root = root / "your_activity_across_facebook/posts/media"
            media_root.mkdir(parents=True)
            attachments = []
            for index in range(31):
                name = f"post{index}.jpg"
                (media_root / name).write_bytes(name.encode())
                attachments.append({"media": {"uri": f"your_activity_across_facebook/posts/media/{name}"}})
            self._write_json(root, "your_posts_1.json", [{"timestamp": 1690000000,
                "url": "https://www.facebook.com/example-post",
                "data": [{"post": "Original post body"}],
                "attachments": [{"data": attachments}]}])

            summary = build_package(root, Path(temporary) / "output")
            with zipfile.ZipFile(summary["package_path"]) as archive:
                entries = json.loads(archive.read("Journal.json"))["entries"]
            self.assertEqual([len(entry["photos"]) for entry in entries], [30, 1])
            self.assertIn("Original post body", entries[0]["text"])
            self.assertNotIn("Original post body", entries[1]["text"])
            self.assertEqual(summary["attachment_count"], 31)
            self.assertEqual(summary["facebook_post_links"], 1)

    def _write_json(self, root: Path, name: str, payload: object) -> None:
        path = root / "your_activity_across_facebook" / "posts" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_post_photo_video_location_and_orphan_photo(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "facebook"
            photo = root / "your_activity_across_facebook/posts/media/photo.jpg"
            video = root / "your_activity_across_facebook/posts/media/video.mp4"
            orphan = root / "your_activity_across_facebook/posts/media/orphan.jpg"
            photo.parent.mkdir(parents=True)
            photo.write_bytes(b"photo fixture")
            video.write_bytes(b"video fixture")
            orphan.write_bytes(b"orphan fixture")
            self._write_json(root, "your_posts__check_ins__photos_and_videos_1.json", [
                {
                    "timestamp": 1690000000,
                    "data": [{"post": "Example post"}],
                    "attachments": [{"data": [
                        {"media": {"uri": "your_activity_across_facebook/posts/media/photo.jpg"}},
                        {"media": {"uri": "your_activity_across_facebook/posts/media/video.mp4"}},
                        {"place": {"name": "Synthetic location", "coordinate": {"latitude": 25.04, "longitude": 121.51}}},
                    ]}],
                }
            ])
            self._write_json(root, "your_uncategorized_photos.json", {"other_photos_v2": [
                {"creation_timestamp": 1690000100,
                 "uri": "your_activity_across_facebook/posts/media/orphan.jpg"}
            ]})

            summary = build_package(root, Path(temporary) / "output")

            self.assertEqual(summary["entry_count"], 2)
            self.assertEqual(summary["attachment_count"], 3)
            with zipfile.ZipFile(summary["package_path"]) as archive:
                self.assertIsNone(archive.testzip())
                journal = json.loads(archive.read("Journal.json"))
                self.assertEqual(len(journal["entries"]), 2)
                entry = journal["entries"][0]
                self.assertEqual(entry["timeZone"], "Etc/UTC")
                self.assertEqual(entry["location"], {"latitude": 25.04, "longitude": 121.51, "placeName": "Synthetic location"})
                self.assertIn("Example post", entry["text"])
                self.assertEqual(len(entry["photos"]), 1)
                self.assertEqual(len(entry["videos"]), 1)
                for kind, folder in (("photos", "photos"), ("videos", "videos")):
                    item = entry[kind][0]
                    self.assertIn(item["identifier"], entry["text"])
                    self.assertTrue(any(name.startswith(folder + "/" + item["md5"]) for name in archive.namelist()))
                self.assertIn("photos", journal["entries"][1])

    def test_missing_media_fails_without_package(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "facebook"
            self._write_json(root, "your_posts_1.json", [
                {"timestamp": 1690000000, "attachments": [
                    {"data": [{"media": {"uri": "missing.jpg"}}]}
                ]}
            ])
            output = Path(temporary) / "output"
            with self.assertRaisesRegex(ValueError, "missing media"):
                build_package(root, output)
            self.assertFalse((output / "facebook_dayone.zip").exists())

    def test_invalid_source_and_path_traversal_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "facebook"
            root.mkdir()
            (root / "start_here.html").write_text("<html></html>")
            with self.assertRaisesRegex(ValueError, "JSON post"):
                build_package(root, Path(temporary) / "output")
            self._write_json(root, "your_posts_1.json", [
                {"timestamp": 1690000000, "attachments": [
                    {"data": [{"media": {"uri": "../../outside.jpg"}}]}
                ]}
            ])
            with self.assertRaisesRegex(ValueError, "outside the export"):
                build_package(root, Path(temporary) / "output")

    def test_verifier_detects_media_byte_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary) / "mismatch.zip"
            with zipfile.ZipFile(package, "w") as archive:
                archive.writestr("photos/00000000000000000000000000000000.jpeg", b"different bytes")
            with self.assertRaisesRegex(ValueError, "media hash does not match"):
                verify_media_hashes(package)

    def test_unindexed_photo_is_packaged_with_explicit_date_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "facebook"
            photo = root / "your_activity_across_facebook" / "posts" / "media" / "unindexed.jpg"
            photo.parent.mkdir(parents=True)
            photo.write_bytes(b"unique unindexed fixture")
            self._write_json(root, "your_posts_1.json", [{"timestamp": 1690000000}])

            summary = build_package(root, Path(temporary) / "output")

            self.assertEqual(summary["entry_count"], 2)
            self.assertEqual(summary["unindexed_media_files"], 1)
            self.assertEqual(summary["estimated_date_entries"], 1)
            with zipfile.ZipFile(summary["package_path"]) as archive:
                entries = json.loads(archive.read("Journal.json"))["entries"]
            self.assertTrue(any("facebook:date-estimated" in item["tags"] for item in entries))

    def test_supplemental_html_post_and_external_video_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "facebook"
            others = Path(temporary) / "other"
            html = others / "html" / "your_activity_across_facebook/posts/your_posts_1.html"
            html.parent.mkdir(parents=True)
            video = others / "media-only" / "your_activity_across_facebook/posts/media/123_456_789_n_987.mp4"
            video.parent.mkdir(parents=True)
            video.write_bytes(b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00")
            external = "https://example.invalid/123_456_789_n.mp4"
            self._write_json(root, "your_posts_1.json", [
                {"timestamp": 1690000000, "data": [{"post": "Existing one"}],
                 "attachments": [{"data": [{"media": {"uri": external}}]}]},
                {"timestamp": 1690000100, "data": [{"post": "Existing two"}]},
            ])

            def card(date: str, body: str) -> str:
                return ('<div class="_3-95 _a6-g"><div class="_2ph_ _a6-p">'
                        '<div class="_2pin">Header</div><div class="_2ph_ _a6-h _a6-i">'
                        f'{body}</div><div class="_3-94 _a6-o"><a><div class="_a72d">'
                        f'{date}</div></a></div></div></div>')

            import datetime as dt
            first = dt.datetime.fromtimestamp(1690000000, dt.UTC) + dt.timedelta(hours=8)
            second = dt.datetime.fromtimestamp(1690000100, dt.UTC) + dt.timedelta(hours=8)
            newest = dt.datetime.fromtimestamp(1690000200, dt.UTC) + dt.timedelta(hours=8)
            fmt = "%b %d, %Y %I:%M:%S%p"
            html.write_text("<html><body>" + "".join([
                card(first.strftime(fmt), "Existing one"),
                card(second.strftime(fmt), "Existing two"),
                card(newest.strftime(fmt), "<div>Unique &amp;</div><div>preserved</div>"),
            ]) + "</body></html>", encoding="utf-8")

            summary = build_package(root, Path(temporary) / "output", additional_exports=others)

            self.assertEqual(summary["source_counts"]["supplemental_posts"], 1)
            self.assertEqual(summary["recovered_external_media_links"], 1)
            self.assertEqual(summary["external_media_links"], 0)
            with zipfile.ZipFile(summary["package_path"]) as archive:
                entries = json.loads(archive.read("Journal.json"))["entries"]
                self.assertEqual(len(entries), 3)
                self.assertEqual(entries[-1]["creationDate"], "2023-07-22T04:30:00Z")
                self.assertIn("Unique &\npreserved", entries[-1]["text"])
                self.assertEqual(len(entries[0]["videos"]), 1)
                self.assertIsNone(archive.testzip())

    def test_supplemental_html_without_matching_timezone_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "facebook"
            others = Path(temporary) / "other"
            html = others / "html" / "your_activity_across_facebook/posts/your_posts_1.html"
            html.parent.mkdir(parents=True)
            html.write_text('<div class="_3-95 _a6-g"><div class="_2ph_ _a6-p">'
                            '<div class="_3-94 _a6-o"><div class="_a72d">Sep 21, 2023 10:27:53pm</div>'
                            '</div></div></div>', encoding="utf-8")
            self._write_json(root, "your_posts_1.json", [{"timestamp": 1690000000}])
            with self.assertRaisesRegex(ValueError, "timezone"):
                build_package(root, Path(temporary) / "output", additional_exports=others)

    def test_matching_html_post_links_and_mentions_are_clickable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "facebook"
            others = Path(temporary) / "other"
            html = others / "html" / "posts" / "your_posts_1.html"
            html.parent.mkdir(parents=True)
            self._write_json(root, "your_posts_1.json", [
                {"timestamp": 1690000000, "data": [{"post": "Shared @[12345:2048:Sample Person]"}],
                 "attachments": [{"data": [{"external_context": {"url": ""}}]}]},
                {"timestamp": 1690000100, "data": [{"post": "Article"}],
                 "attachments": [{"data": [{"external_context": {"url": "https://example.org/article"}}]}]},
            ])
            html.write_text(
                '<div class="_3-95 _a6-g"><div class="_2ph_ _a6-p"><div class="_2ph_ _a6-h _a6-i">A</div>'
                '<a href="https://www.facebook.com/dyi/l/?l=first&amp;s=518"><div class="_a72d">Jul 22, 2023 12:26:40pm</div></a></div></div>'
                '<div class="_3-95 _a6-g"><div class="_2ph_ _a6-p"><div class="_2ph_ _a6-h _a6-i">B</div>'
                '<a href="https://www.facebook.com/dyi/l/?l=second&amp;s=518"><div class="_a72d">Jul 22, 2023 12:28:20pm</div></a></div></div>',
                encoding="utf-8",
            )

            summary = build_package(root, Path(temporary) / "output", additional_exports=others)

            self.assertEqual(summary["facebook_post_links"], 2)
            self.assertEqual(summary["shared_content_without_original_url"], 1)
            with zipfile.ZipFile(summary["package_path"]) as archive:
                entries = json.loads(archive.read("Journal.json"))["entries"]
            self.assertEqual(entries[0]["timeZone"], "Etc/GMT-8")
            self.assertIn("[Sample Person](https://www.facebook.com/profile.php?id=12345)", entries[0]["text"])
            self.assertNotIn("@[12345:2048:", entries[0]["text"])
            self.assertIn("[View this Facebook post](https://www.facebook.com/dyi/l/?l=first&s=518)", entries[0]["text"])
            self.assertIn("did not include the original link or media", entries[0]["text"])
            self.assertIn("[Open shared link](https://example.org/article)", entries[1]["text"])
            self.assertEqual(summary["latest_post_date"], "2023-07-22")

    def test_orphan_photo_description_mentions_are_clickable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "facebook"
            self._write_json(root, "your_posts_1.json", [{"timestamp": 1690000000}])
            media_root = root / "your_activity_across_facebook/posts/media"
            media_root.mkdir(parents=True)
            (media_root / "photo.jpg").write_bytes(b"photo")
            (media_root.parent / "your_uncategorized_photos.json").write_text(json.dumps({
                "other_photos_v2": [{
                    "uri": "your_activity_across_facebook/posts/media/photo.jpg",
                    "creation_timestamp": 1690000100,
                    "description": "Tagged @[12345:2048:Sample Person]",
                }],
            }), encoding="utf-8")

            summary = build_package(root, Path(temporary) / "output")
            with zipfile.ZipFile(summary["package_path"]) as archive:
                entries = json.loads(archive.read("Journal.json"))["entries"]
            photo = next(item for item in entries if "facebook:photo" in item["tags"])
            self.assertIn("[Sample Person](https://www.facebook.com/profile.php?id=12345)", photo["text"])
            self.assertNotIn("@[12345:2048:", photo["text"])

    def test_media_caption_mentions_are_clickable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "facebook"
            media = root / "your_activity_across_facebook" / "posts" / "media" / "video.mp4"
            media.parent.mkdir(parents=True)
            media.write_bytes(b"video")
            self._write_json(root, "your_posts_1.json", [{
                "timestamp": 1690000000,
                "attachments": [{"data": [{"media": {
                    "uri": "your_activity_across_facebook/posts/media/video.mp4",
                    "description": "Wonderful memories from @[12345:2048:Sample Person]'s year!",
                }}]}],
            }])

            summary = build_package(root, Path(temporary) / "output")

            with zipfile.ZipFile(summary["package_path"]) as archive:
                entries = json.loads(archive.read("Journal.json"))["entries"]
            self.assertIn("[Sample Person](https://www.facebook.com/profile.php?id=12345)", entries[0]["text"])
            self.assertNotIn("@[12345:2048:", entries[0]["text"])

    def test_ambiguous_timestamp_does_not_attach_wrong_html_link(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "facebook"
            others = Path(temporary) / "other"
            html = others / "html" / "posts" / "your_posts_1.html"
            html.parent.mkdir(parents=True)
            self._write_json(root, "your_posts_1.json", [
                {"timestamp": 1690000000, "data": [{"post": "One"}]},
                {"timestamp": 1690000000, "data": [{"post": "Two"}]},
                {"timestamp": 1690000100, "data": [{"post": "Three"}]},
            ])
            html.write_text(
                '<div class="_3-95 _a6-g"><div class="_2ph_ _a6-p"><div class="_2ph_ _a6-h _a6-i">One</div>'
                '<a href="https://www.facebook.com/dyi/l/?l=ambiguous"><div class="_a72d">Jul 22, 2023 12:26:40pm</div></a></div></div>'
                '<div class="_3-95 _a6-g"><div class="_2ph_ _a6-p"><div class="_2ph_ _a6-h _a6-i">Three</div>'
                '<a href="https://www.facebook.com/dyi/l/?l=third"><div class="_a72d">Jul 22, 2023 12:28:20pm</div></a></div></div>',
                encoding="utf-8",
            )

            summary = build_package(root, Path(temporary) / "output", additional_exports=others)

            self.assertEqual(summary["facebook_post_links"], 1)
            self.assertEqual(summary["ambiguous_html_post_links"], 1)
            with zipfile.ZipFile(summary["package_path"]) as archive:
                entries = json.loads(archive.read("Journal.json"))["entries"]
            self.assertNotIn("View this Facebook post", entries[0]["text"])
            self.assertNotIn("View this Facebook post", entries[1]["text"])
            self.assertIn("View this Facebook post", entries[2]["text"])

    def test_supplemental_same_path_variant_is_reported_not_substituted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "facebook"
            others = Path(temporary) / "other"
            media = Path("your_activity_across_facebook/posts/media/photo.jpg")
            (root / media).parent.mkdir(parents=True)
            (root / media).write_bytes(b"original")
            (others / "html" / media).parent.mkdir(parents=True)
            (others / "html" / media).write_bytes(b"alternate bytes")
            (others / "html" / media.parent / ".DS_Store").write_bytes(b"metadata")
            self._write_json(root, "your_posts_1.json", [{"timestamp": 1690000000,
                "attachments": [{"data": [{"media": {"uri": str(media)}}]}]}])

            summary = build_package(root, Path(temporary) / "output", additional_exports=others)

            self.assertEqual(summary["supplemental_media_variants"], 1)
            manifest = json.loads(Path(summary["manifest_path"]).read_text())
            self.assertEqual(manifest["supplemental_variant_paths"], [str(media)])
            with zipfile.ZipFile(summary["package_path"]) as archive:
                self.assertTrue(any(archive.read(name) == b"original" for name in archive.namelist() if name.startswith("photos/")))


if __name__ == "__main__":
    unittest.main()
