from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from twitter_dayone_workflow import (
    TwitterReviewStore,
    _parse_byte_range,
    _build_candidates,
    _review_html,
    build_reviewed_package,
    verify_package,
)


class TwitterDayOneWorkflowTests(unittest.TestCase):
    def test_video_byte_ranges_are_bounded_and_reject_invalid_requests(self) -> None:
        self.assertEqual(_parse_byte_range("", 100), None)
        self.assertEqual(_parse_byte_range("bytes=10-19", 100), (10, 19))
        self.assertEqual(_parse_byte_range("bytes=90-", 100), (90, 99))
        self.assertEqual(_parse_byte_range("bytes=-10", 100), (90, 99))
        with self.assertRaisesRegex(ValueError, "Invalid media byte range"):
            _parse_byte_range("bytes=100-110", 100)

    def test_builds_complete_threads_and_quarantines_missing_context(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._archive(Path(temporary))
            self._write_tweets(root, [
                self._tweet("1", "Mon Jan 01 10:00:00 +0000 2024", "Root https://t.co/m1", media=[("m1", "photo")]),
                self._tweet("2", "Mon Jan 01 10:05:00 +0000 2024", "Follow-up", reply_to="1"),
                self._tweet("3", "Tue Jan 02 10:00:00 +0000 2024", "Contextless reply", reply_to="999"),
                self._tweet("4", "Wed Jan 03 10:00:00 +0000 2024", "RT @someone: reshared"),
                self._tweet("5", "Thu Jan 04 10:00:00 +0000 2024", "Standalone note"),
            ])
            (root / "data" / "tweets_media" / "1-m1.jpg").write_bytes(b"jpeg")

            result = _build_candidates(root)
            candidates = {candidate.root_post_id: candidate for candidate in result.candidates}

            self.assertEqual(set(candidates), {"1", "3", "4", "5"})
            self.assertEqual(candidates["1"].post_ids, ("1", "2"))
            self.assertEqual(candidates["1"].classification, "complete_thread")
            self.assertEqual(candidates["1"].default_decision, "pending")
            self.assertEqual(len(candidates["1"].media), 1)
            self.assertNotIn("https://t.co/m1", candidates["1"].body)
            self.assertIn("Follow-up", candidates["1"].body)
            self.assertEqual(candidates["3"].classification, "missing_context")
            self.assertEqual(candidates["3"].default_decision, "exclude")
            self.assertTrue(candidates["3"].auto_excluded)
            self.assertEqual(candidates["4"].classification, "retweet")
            self.assertTrue(candidates["4"].auto_excluded)
            self.assertEqual(result.summary["missing_context_posts"], 1)

    def test_uses_unique_note_tweet_text_without_guessing_ambiguous_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._archive(Path(temporary))
            self._write_tweets(root, [
                self._tweet("10", "Mon Jan 01 10:00:00 +0000 2024", "Short version"),
            ])
            self._write_js(root / "data" / "note-tweet.js", "window.YTD.note_tweet.part0 = ", [{
                "noteTweet": {
                    "noteTweetId": "note-10",
                    "createdAt": "2024-01-01T10:00:00.000Z",
                    "updatedAt": "2024-01-01T10:00:00.000Z",
                    "lifecycle": {"name": "Published"},
                    "core": {"text": "The complete long-form note", "urls": [], "mentions": [], "cashtags": [], "hashtags": []},
                }
            }])

            candidate = _build_candidates(root).candidates[0]

            self.assertEqual(candidate.body, "The complete long-form note")

    def test_preserves_paragraph_breaks_in_diary_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._archive(Path(temporary))
            self._write_tweets(root, [
                self._tweet("10", "Mon Jan 01 10:00:00 +0000 2024", "First paragraph.\n\nSecond paragraph."),
            ])

            candidate = _build_candidates(root).candidates[0]

            self.assertEqual(candidate.body, "First paragraph.\n\nSecond paragraph.")

    def test_associates_reversed_photo_entities_by_media_url(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._archive(Path(temporary))
            self._write_tweets(root, [
                self._tweet(
                    "1", "Mon Jan 01 10:00:00 +0000 2024", "Photos",
                    media=[("m2", "photo"), ("m1", "photo")],
                ),
            ])
            (root / "data" / "tweets_media" / "1-m1.jpg").write_bytes(b"photo one")
            (root / "data" / "tweets_media" / "1-m2.jpg").write_bytes(b"photo two")

            candidate = _build_candidates(root).candidates[0]

            self.assertEqual([media.media_id for media in candidate.media], ["1:m2", "1:m1"])
            self.assertEqual(
                [media.path.name for media in candidate.media],
                ["1-m2.jpg", "1-m1.jpg"],
            )

    def test_rejects_ambiguous_same_kind_media_associations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._archive(Path(temporary))
            self._write_tweets(root, [
                self._tweet(
                    "1", "Mon Jan 01 10:00:00 +0000 2024", "Photos",
                    media=[("entity-a", "photo"), ("entity-b", "photo")],
                ),
            ])
            (root / "data" / "tweets_media" / "1-local-a.jpg").write_bytes(b"photo one")
            (root / "data" / "tweets_media" / "1-local-b.jpg").write_bytes(b"photo two")

            with self.assertRaisesRegex(ValueError, "cannot be uniquely associated"):
                _build_candidates(root)

    def test_associates_videos_by_variant_url(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._archive(Path(temporary))
            tweet = self._tweet(
                "1", "Mon Jan 01 10:00:00 +0000 2024", "Videos",
                media=[("video-a", "video"), ("video-b", "video")],
            )
            entities = tweet["tweet"]["extended_entities"]["media"]
            entities[0]["video_info"]["variants"] = [
                {"content_type": "video/mp4", "url": "https://video.twimg.com/clip-b.mp4?tag=1"},
            ]
            entities[1]["video_info"]["variants"] = [
                {"content_type": "video/mp4", "url": "https://video.twimg.com/clip-a.mp4?tag=1"},
            ]
            self._write_tweets(root, [tweet])
            (root / "data" / "tweets_media" / "1-clip-a.mp4").write_bytes(b"video a")
            (root / "data" / "tweets_media" / "1-clip-b.mp4").write_bytes(b"video b")

            candidate = _build_candidates(root).candidates[0]

            self.assertEqual([media.media_id for media in candidate.media], ["1:video-a", "1:video-b"])
            self.assertEqual(
                [media.path.name for media in candidate.media],
                ["1-clip-b.mp4", "1-clip-a.mp4"],
            )

    def test_review_state_is_persistent_reversible_and_reopens_on_source_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._archive(Path(temporary))
            self._write_tweets(root, [self._tweet("1", "Mon Jan 01 10:00:00 +0000 2024", "First")])
            candidate = _build_candidates(root).candidates[0]
            store = TwitterReviewStore(Path(temporary) / "review.sqlite3")
            store.sync([candidate])

            store.review("twitter:1", decision="include", rating=5, title="A title", body="Edited", context_note="", reviewed=True)
            state = TwitterReviewStore(store.path).entry("twitter:1")
            self.assertEqual((state["decision"], state["rating"], state["title"], state["body"]), ("include", 5, "A title", "Edited"))
            self.assertTrue(state["reviewed"])

            changed = candidate.__class__(**{**candidate.__dict__, "body": "Source changed", "source_fingerprint": "changed"})
            store.sync([changed])
            reopened = store.entry("twitter:1")
            self.assertFalse(reopened["reviewed"])
            self.assertEqual(reopened["decision"], "include")
            self.assertEqual(reopened["body"], "Edited")

            store.review("twitter:1", decision="exclude", rating=2, title="", body="Edited", context_note="", reviewed=True)
            self.assertEqual(store.summary()["reviewed_excluded"], 1)

    def test_exports_only_reviewed_includes_with_media_first_day_one_layout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = self._archive(base)
            self._write_tweets(root, [
                self._tweet(
                    "1", "Mon Jan 01 10:00:00 +0000 2024", "A diary-worthy post https://t.co/m1 https://t.co/m2",
                    media=[("m1", "photo"), ("m2", "video")],
                ),
                self._tweet("2", "Tue Jan 02 10:00:00 +0000 2024", "Still pending"),
            ])
            (root / "data" / "tweets_media" / "1-m1.jpg").write_bytes(b"jpeg-bytes")
            (root / "data" / "tweets_media" / "1-m2.mp4").write_bytes(b"video-bytes")
            candidates = _build_candidates(root).candidates
            store = TwitterReviewStore(base / "review.sqlite3")
            store.sync(candidates)
            store.review(
                "twitter:1", decision="include", rating=5, title="A remembered day",
                body="Edited diary text", context_note="Why this mattered", reviewed=True,
            )

            summary = build_reviewed_package(store, base / "output", limit=20)
            verify_package(Path(summary["package_path"]))

            self.assertEqual(summary["entry_count"], 1)
            self.assertEqual(summary["photo_count"], 1)
            self.assertEqual(summary["video_count"], 1)
            with zipfile.ZipFile(summary["package_path"]) as archive:
                journal = json.loads(archive.read("Journal.json"))
                entry = journal["entries"][0]
                first_text = entry["text"].splitlines()[0]
                self.assertTrue(first_text.startswith("![](dayone-moment://"))
                self.assertIn("# A remembered day", entry["text"])
                self.assertIn("> **Context:** Why this mattered", entry["text"])
                self.assertIn("Edited diary text", entry["text"])
                self.assertEqual(len(entry["photos"]), 1)
                self.assertEqual(len(entry["videos"]), 1)
                self.assertIn("source:twitter", entry["tags"])

    def test_export_fails_when_no_item_has_explicit_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = self._archive(base)
            self._write_tweets(root, [self._tweet("1", "Mon Jan 01 10:00:00 +0000 2024", "Pending")])
            store = TwitterReviewStore(base / "review.sqlite3")
            store.sync(_build_candidates(root).candidates)

            with self.assertRaisesRegex(ValueError, "No reviewed Twitter entries are approved"):
                build_reviewed_package(store, base / "output", limit=20)
            self.assertFalse((base / "output").exists())

    def test_review_ui_exposes_context_rating_edit_and_test_export_controls(self) -> None:
        html = _review_html("csrf-token")

        self.assertIn("Day One preview", html)
        self.assertIn("Needs context", html)
        self.assertIn("All items", html)
        self.assertIn('id="ratingControls"', html)
        self.assertIn('id="entryTitle"', html)
        self.assertIn('id="entryBody"', html)
        self.assertIn('id="contextNote"', html)
        self.assertIn("Approve &amp; next", html)
        self.assertIn("Exclude &amp; next", html)
        self.assertIn("Build reviewed test ZIP", html)
        self.assertIn("/api/export", html)
        self.assertIn("http://127.0.0.1:8766/?step=import_hub", html)

    @staticmethod
    def _archive(base: Path) -> Path:
        root = base / "twitter-test"
        (root / "data" / "tweets_media").mkdir(parents=True)
        TwitterDayOneWorkflowTests._write_js(
            root / "data" / "account.js", "window.YTD.account.part0 = ",
            [{"account": {"username": "example", "accountId": "42", "email": "", "createdVia": "web", "createdAt": "", "accountDisplayName": "Example"}}],
        )
        TwitterDayOneWorkflowTests._write_js(
            root / "data" / "account-timezone.js", "window.YTD.account_timezone.part0 = ",
            [{"accountTimezone": {"accountId": "42", "timeZone": "Pacific Time (US & Canada)"}}],
        )
        TwitterDayOneWorkflowTests._write_js(root / "data" / "note-tweet.js", "window.YTD.note_tweet.part0 = ", [])
        return root

    @staticmethod
    def _tweet(source_id: str, created_at: str, text: str, *, reply_to: str = "", media: list[tuple[str, str]] | None = None) -> dict[str, object]:
        media_rows = []
        for media_id, kind in media or []:
            extension = "jpg" if kind == "photo" else "mp4"
            media_rows.append({
                "id_str": media_id, "type": kind,
                "url": f"https://t.co/{media_id}", "expanded_url": f"https://x.com/example/status/{source_id}/photo/1",
                "media_url": f"http://pbs.twimg.com/media/{media_id}.{extension}", "media_url_https": f"https://pbs.twimg.com/media/{media_id}.{extension}",
                "indices": [0, 1], "display_url": "pic.x.com/example", "sizes": {},
                **({"video_info": {"aspect_ratio": [16, 9], "variants": []}} if kind == "video" else {}),
            })
        tweet: dict[str, object] = {
            "id": source_id, "id_str": source_id, "created_at": created_at, "full_text": text,
            "retweeted": False, "truncated": False, "source": "web", "lang": "en",
            "favorite_count": "0", "retweet_count": "0", "favorited": False,
            "display_text_range": [0, len(text)], "edit_info": {"initial": {"editTweetIds": [source_id]}},
            "entities": {"hashtags": [], "symbols": [], "user_mentions": [], "urls": [], "media": media_rows},
        }
        if media_rows:
            tweet["extended_entities"] = {"media": media_rows}
        if reply_to:
            tweet.update({"in_reply_to_status_id": reply_to, "in_reply_to_status_id_str": reply_to,
                          "in_reply_to_user_id": "99", "in_reply_to_user_id_str": "99", "in_reply_to_screen_name": "someone"})
        return {"tweet": tweet}

    @staticmethod
    def _write_tweets(root: Path, tweets: list[dict[str, object]]) -> None:
        TwitterDayOneWorkflowTests._write_js(root / "data" / "tweets.js", "window.YTD.tweets.part0 = ", tweets)

    @staticmethod
    def _write_js(path: Path, prefix: str, payload: object) -> None:
        path.write_text(prefix + json.dumps(payload), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
