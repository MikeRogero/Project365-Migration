from __future__ import annotations

import datetime as dt
import io
import json
import socket
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from swarm_dayone_importer import Entry, Photo
from swarm_photo_backfill_review import (
    BackfillReviewState,
    BackfillDecisionStore,
    WIKIMEDIA_USER_AGENT,
    _commons_page_candidates,
    _google_maps_url,
    _google_maps_embed_url,
    _google_images_query,
    _google_images_url,
    _is_allowed_remote_image_url,
    _normalize_nominatim_address,
    _review_html,
    build_review_targets,
    validate_dropped_image,
)


class SwarmPhotoBackfillReviewTests(unittest.TestCase):
    def test_nominatim_address_normalization_omits_missing_parts_and_deduplicates(self) -> None:
        normalized = _normalize_nominatim_address({
            "address": {
                "house_number": "12",
                "road": "Main Street",
                "city": "Taipei",
                "state": "Taipei",
                "country": "Taiwan",
            }
        })

        self.assertEqual(normalized, {
            "address": "12 Main Street",
            "city": "Taipei",
            "state": "Taipei",
            "country": "Taiwan",
            "display": "12 Main Street, Taipei, Taiwan",
        })

        partial = _normalize_nominatim_address({
            "address": {"road": "Oak Road", "town": "Smallville"}
        })
        self.assertEqual(partial, {
            "address": "Oak Road",
            "city": "Smallville",
            "state": "",
            "country": "",
            "display": "Oak Road, Smallville",
        })
        self.assertEqual(_normalize_nominatim_address({"address": {}})["display"], "")

    def test_nominatim_address_prefers_locality_and_region_fallbacks(self) -> None:
        normalized = _normalize_nominatim_address({
            "address": {
                "village": "Village Name",
                "municipality": "Other Locality",
                "region": "Example Region",
                "country": "Example Country",
            }
        })

        self.assertEqual(normalized["city"], "Village Name")
        self.assertEqual(normalized["state"], "Example Region")
        self.assertEqual(normalized["display"], "Village Name, Example Region, Example Country")

    def test_google_images_query_joins_title_and_address_without_duplicate_title(self) -> None:
        self.assertEqual(
            _google_images_query("Cafe Example", {"display": "12 Main St, Example City"}),
            "Cafe Example, 12 Main St, Example City",
        )
        self.assertEqual(
            _google_images_query("Cafe Example", {"display": "Cafe Example, Example City"}),
            "Cafe Example, Example City",
        )
        self.assertEqual(
            _google_images_query("Cafe Example", {"display": ""}),
            "Cafe Example",
        )
        self.assertEqual(
            _google_images_query(
                "Tip for Cafe Example",
                {"display": "12 Main St, Example City"},
            ),
            "Cafe Example, 12 Main St, Example City",
        )
        self.assertEqual(
            _google_images_query("Tip for Cafe Example", {"display": ""}),
            "Cafe Example",
        )

    def test_google_images_url_encodes_the_exact_search_query(self) -> None:
        self.assertEqual(
            _google_images_url("Cafe Example, 台北"),
            "https://www.google.com/search?tbm=isch&q=Cafe+Example%2C+%E5%8F%B0%E5%8C%97",
        )
        self.assertEqual(_google_images_url(""), "https://www.google.com/search?tbm=isch")

    def test_location_lookup_is_entry_keyed_and_persistently_cached(self) -> None:
        first = self._entry("checkin:first", "checkin", 2020, venue_id="venue-1")
        second = self._entry("checkin:second", "checkin", 2021, venue_id="venue-2")
        second.latitude = 3.0
        second.longitude = 4.0

        class Response:
            def __init__(self, payload: dict) -> None:
                self.payload = payload

            def __enter__(self) -> Response:
                return self

            def __exit__(self, *_: object) -> None:
                return None

            def read(self) -> bytes:
                return json.dumps(self.payload).encode("utf-8")

        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            state = BackfillReviewState([first, second], state_dir)
            first_token = state.entry_payload("pending", 0)["entry"]["entry_token"]
            second_token = state.entry_payload("pending", 1)["entry"]["entry_token"]
            payloads = [
                {"address": {"road": "First Road", "city": "First City", "country": "Taiwan"}},
                {"address": {"road": "Second Road", "city": "Second City", "country": "Taiwan"}},
            ]
            with (
                mock.patch("swarm_photo_backfill_review.urllib.request.urlopen") as urlopen,
                mock.patch("swarm_photo_backfill_review.NOMINATIM_MIN_INTERVAL_SECONDS", 0),
            ):
                urlopen.side_effect = [Response(payloads[0]), Response(payloads[1])]
                first_location = state.location_for(first_token)
                second_location = state.location_for(second_token)

            self.assertEqual(urlopen.call_count, 2)
            first_request = urlopen.call_args_list[0].args[0]
            self.assertIn("Project365-Swarm-Representative-Photo-Review", first_request.get_header("User-agent"))
            self.assertIn("format=jsonv2", first_request.full_url)
            self.assertIn("addressdetails=1", first_request.full_url)
            self.assertEqual(first_location["address"], "First Road")
            self.assertEqual(second_location["address"], "Second Road")
            self.assertEqual(first_location["google_images_query"], "Example venue, First Road, First City, Taiwan")
            self.assertTrue(first_location["available"])
            self.assertTrue(first_location["attribution"])
            self.assertTrue((state_dir / "location_cache.json").is_file())

            reloaded = BackfillReviewState([first, second], state_dir)
            reloaded_first_token = reloaded.entry_payload("pending", 0)["entry"]["entry_token"]
            with mock.patch("swarm_photo_backfill_review.urllib.request.urlopen") as urlopen:
                cached_location = reloaded.location_for(reloaded_first_token)

            urlopen.assert_not_called()
            self.assertEqual(cached_location, first_location)

    def test_location_lookup_without_coordinates_is_unavailable_without_network(self) -> None:
        entry = self._entry("checkin:unlocated", "checkin", 2020, venue_id="venue-1")
        entry.latitude = None
        entry.longitude = None
        with tempfile.TemporaryDirectory() as temporary:
            state = BackfillReviewState([entry], Path(temporary))
            token = state.entry_payload("pending", 0)["entry"]["entry_token"]
            with mock.patch("swarm_photo_backfill_review.urllib.request.urlopen") as urlopen:
                location = state.location_for(token)

        urlopen.assert_not_called()
        self.assertFalse(location["available"])
        self.assertEqual(location["display"], "")
        self.assertEqual(location["google_images_query"], "Example venue")

    def test_entry_payload_exposes_exact_location_links(self) -> None:
        entry = self._entry("checkin:target", "checkin", 2020, venue_id="venue-1")
        entry.links = [("Foursquare venue", "https://foursquare.com/v/example/venue-1")]
        with tempfile.TemporaryDirectory() as temporary:
            state = BackfillReviewState([entry], Path(temporary))
            with mock.patch("swarm_photo_backfill_review.urllib.request.urlopen") as urlopen:
                initial_payload = state.entry_payload("pending", 0)["entry"]
            urlopen.assert_not_called()
            self.assertIsNone(initial_payload["location"])
            self.assertEqual(initial_payload["google_images_query"], "Example venue")

            cached_location = {
                "address": "12 Example Street", "city": "Example City",
                "state": "Example State", "country": "Example Country",
                "display": "12 Example Street, Example City, Example State, Example Country",
            }
            state._write_cached_location(entry, cached_location)
            with mock.patch("swarm_photo_backfill_review.urllib.request.urlopen") as urlopen:
                payload = state.entry_payload("pending", 0)["entry"]
            urlopen.assert_not_called()

        self.assertEqual(
            payload["google_maps_url"],
            "https://www.google.com/maps/search/?api=1&query=1.00000000%2C2.00000000",
        )
        self.assertEqual(
            payload["foursquare_url"],
            "https://foursquare.com/v/example/venue-1/photos",
        )
        self.assertEqual(
            payload["google_maps_embed_url"],
            "https://maps.google.com/maps?q=1.00000000%2C2.00000000&output=embed",
        )
        self.assertEqual(
            payload["google_images_query"],
            "Example venue, 12 Example Street, Example City, Example State, Example Country",
        )
        self.assertEqual(
            payload["google_images_url"],
            "https://www.google.com/search?tbm=isch&q="
            "Example+venue%2C+12+Example+Street%2C+Example+City%2C+"
            "Example+State%2C+Example+Country",
        )

    def test_reference_workspace_is_foursquare_first_and_uses_a_25_75_split(self) -> None:
        html = _review_html("csrf")

        self.assertIn("grid-template-columns:25% 75%", html)
        self.assertIn('data-reference="foursquare"', html)
        self.assertIn('data-reference="google"', html)
        self.assertLess(
            html.index('data-reference="foursquare"'),
            html.index('data-reference="google"'),
        )
        self.assertIn("Submit to Foursquare", html)
        self.assertIn('target="swarm-location-window"', html)
        self.assertIn('id="googleFrame"', html)
        self.assertIn('id="submitGoogleMaps"', html)
        self.assertIn("Submit to Google Maps", html)

    def test_google_images_is_a_third_tab_after_google_maps(self) -> None:
        html = _review_html("csrf")

        maps_tab = html.index('data-reference="google"')
        images_tab = html.index('data-reference="google-images"')
        self.assertLess(maps_tab, images_tab)
        self.assertIn("Google Images", html[images_tab:])
        self.assertIn('id="googleImagesPanel"', html)
        self.assertIn('id="googleImagesQuery"', html)
        self.assertIn('id="copyGoogleImages"', html)
        self.assertIn('id="openGoogleImages"', html)
        self.assertIn("Copy search", html)
        self.assertIn("Submit to Google Images", html)
        self.assertIn("document.getElementById('googleImagesPanel').hidden=name!=='google-images'", html)
        self.assertIn(
            '<a class="reference-tab" data-reference="google-images" role="tab" '
            'aria-selected="false" href="https://www.google.com/search?tbm=isch" '
            'target="swarm-google-images-window" rel="noopener" '
            'onclick="selectReference(\'google-images\');return false">Google Images</a>',
            html,
        )
        load_function = html[
            html.index("async function load(next=index)"):
            html.index("function candidateCard")
        ]
        self.assertNotIn("/api/location", load_function)

    def test_auto_submit_defaults_off_and_persists_the_user_choice(self) -> None:
        html = _review_html("csrf")

        self.assertIn('id="autoSubmit" type="checkbox"', html)
        self.assertNotIn('id="autoSubmit" type="checkbox" checked', html)
        self.assertIn(
            "let autoSubmit=localStorage.getItem(autoSubmitKey)==='true'",
            html,
        )
        self.assertIn("autoSubmitEl.checked=autoSubmit", html)
        self.assertIn(
            "localStorage.setItem(autoSubmitKey,String(autoSubmit))",
            html,
        )

    def test_entry_load_keeps_provider_but_only_submits_when_auto_enabled(self) -> None:
        html = _review_html("csrf")
        load_function = html[
            html.index("async function load(next=index)"):
            html.index("function candidateCard")
        ]
        select_function = html[
            html.index("async function selectReference"):
            html.index("function setAutoSubmit")
        ]
        submit_function = html[
            html.index("async function submitSelectedReference"):
            html.index("function syncFoursquareWindow")
        ]
        reference_pane_function = html[
            html.index("function updateReferencePane"):
            html.index("async function selectReference")
        ]

        self.assertNotIn("activeReference='foursquare'", load_function)
        self.assertIn("selectReference(activeReference,false)", load_function)
        self.assertIn("if(autoSubmit)await submitSelectedReference()", load_function)
        self.assertNotIn("await submitSelectedReference()", load_function.replace(
            "if(autoSubmit)await submitSelectedReference()", ""
        ))
        self.assertIn(
            "if(submit&&autoSubmit)",
            select_function,
        )
        self.assertIn(
            "if(activeReference==='foursquare')syncFoursquareWindow()",
            submit_function,
        )
        self.assertIn("else if(activeReference==='google')submitGoogleMaps()", submit_function)
        self.assertIn(
            "else if(activeReference==='google-images'){"
            "if(!windowIsOpen(googleImagesWindow))return;await loadGoogleImagesLocation();"
            "syncGoogleImagesWindow()}",
            submit_function,
        )
        self.assertIn("googleMapsEntryToken===current?.entry_token", reference_pane_function)
        self.assertNotIn("activeReference==='google')", reference_pane_function)

    def test_all_three_providers_have_manual_submit_paths(self) -> None:
        html = _review_html("csrf")
        google_images_submit = html[
            html.index("async function openGoogleImages"):
            html.index("async function chooseLocal")
        ]

        self.assertIn('onclick="openFoursquare()">Submit to Foursquare', html)
        self.assertIn('onclick="submitGoogleMaps()">Submit to Google Maps', html)
        self.assertIn('onclick="openGoogleImages()">Submit to Google Images', html)
        self.assertIn("Submit to Google Images to look up the address.", html)
        self.assertIn("window.open('about:blank'", html)
        self.assertIn("await loadGoogleImagesLocation()", google_images_submit)
        self.assertIn("syncGoogleImagesWindow()", google_images_submit)

    def test_foursquare_photo_deep_link_is_appended_exactly_once(self) -> None:
        entry = self._entry("checkin:target", "checkin", 2020, venue_id="venue-1")
        entry.links = [("Foursquare venue", "https://app.foursquare.com/v/example/venue-1/photos")]
        with tempfile.TemporaryDirectory() as temporary:
            state = BackfillReviewState([entry], Path(temporary))
            payload = state.entry_payload("pending", 0)["entry"]

        self.assertEqual(
            payload["foursquare_url"],
            "https://app.foursquare.com/v/example/venue-1/photos",
        )

    def test_remote_image_drop_accepts_public_https_hosts_and_rejects_private_targets(self) -> None:
        public_answer = [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443)),
        ]
        private_answer = [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("192.168.1.5", 443)),
        ]

        def resolve(hostname: str, *_: object, **__: object) -> list[tuple]:
            return private_answer if hostname == "internal.example" else public_answer

        with mock.patch("swarm_photo_backfill_review.socket.getaddrinfo", side_effect=resolve):
            self.assertTrue(_is_allowed_remote_image_url(
                "https://images.smartshanghai.com.cn/uploads/example.jpg"
            ))
            self.assertTrue(_is_allowed_remote_image_url(
                "https://unseen-public-source.example/photo.jpg"
            ))
            self.assertFalse(_is_allowed_remote_image_url(
                "http://unseen-public-source.example/photo.jpg"
            ))
            self.assertFalse(_is_allowed_remote_image_url(
                "https://user:password@unseen-public-source.example/photo.jpg"
            ))
            self.assertFalse(_is_allowed_remote_image_url(
                "https://unseen-public-source.example:8443/photo.jpg"
            ))
            self.assertFalse(_is_allowed_remote_image_url(
                "https://internal.example/private.jpg"
            ))
            self.assertFalse(_is_allowed_remote_image_url("https://127.0.0.1/private.jpg"))

    def test_google_maps_links_use_coordinates_or_search_venue_name(self) -> None:
        located = self._entry("checkin:located", "checkin", 2020, venue_id="venue-1")
        named = self._entry("checkin:named", "checkin", 2020, venue_id="venue-2")
        named.latitude = None
        named.longitude = None

        self.assertEqual(
            _google_maps_embed_url(located),
            "https://maps.google.com/maps?q=1.00000000%2C2.00000000&output=embed",
        )
        self.assertEqual(
            _google_maps_embed_url(named),
            "https://maps.google.com/maps?q=Example+venue&output=embed",
        )
        self.assertEqual(
            _google_maps_url(named),
            "https://www.google.com/maps/search/?api=1&query=Example+venue",
        )

        named.kind = "tip"
        named.title = "Tip for Example venue"
        self.assertEqual(
            _google_maps_embed_url(named),
            "https://maps.google.com/maps?q=Example+venue&output=embed",
        )
        self.assertEqual(
            _google_maps_url(named),
            "https://www.google.com/maps/search/?api=1&query=Example+venue",
        )

    def test_commons_search_omits_tip_for_prefix(self) -> None:
        entry = self._entry("tip:target", "tip", 2020, venue_id="venue-1")
        entry.title = "Tip for Example venue"
        with tempfile.TemporaryDirectory() as temporary:
            state = BackfillReviewState([entry], Path(temporary))
            token = state.entry_payload("pending", 0)["entry"]["entry_token"]
            with mock.patch.object(state, "_commons_query", return_value=[]) as query:
                self.assertEqual(state.commons_candidates(token), [])

        self.assertEqual(
            query.call_args_list[0].args[0]["gsrsearch"],
            'intitle:"Example venue"',
        )

    def test_commons_api_identifies_the_project_with_contact_url(self) -> None:
        payload = io.BytesIO(json.dumps({"query": {"pages": []}}).encode("utf-8"))
        with mock.patch(
            "swarm_photo_backfill_review.urllib.request.urlopen",
            return_value=payload,
        ) as urlopen:
            with mock.patch.object(BackfillReviewState, "__init__", return_value=None):
                state = BackfillReviewState([], Path("/tmp/unused"))
            self.assertEqual(state._commons_query({"generator": "search"}), [])

        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header("User-agent"), WIKIMEDIA_USER_AGENT)
        self.assertIn("Project365SwarmReviewBot/", WIKIMEDIA_USER_AGENT)
        self.assertIn(
            "https://github.com/MikeRogero/Project365-Migration",
            WIKIMEDIA_USER_AGENT,
        )

    def test_concurrent_commons_lookups_share_one_serial_cached_search(self) -> None:
        entry = self._entry("checkin:target", "checkin", 2020, venue_id="venue-1")
        first_query_started = threading.Event()
        release_first_query = threading.Event()
        calls: list[dict[str, str]] = []
        calls_lock = threading.Lock()

        def query(parameters: dict[str, str]) -> list[dict]:
            with calls_lock:
                calls.append(parameters)
                if len(calls) == 1:
                    first_query_started.set()
            release_first_query.wait(timeout=2)
            return []

        with tempfile.TemporaryDirectory() as temporary:
            state = BackfillReviewState([entry], Path(temporary))
            token = state.entry_payload("pending", 0)["entry"]["entry_token"]
            with mock.patch.object(state, "_commons_query", side_effect=query):
                results: list[list[dict]] = []
                first = threading.Thread(
                    target=lambda: results.append(state.commons_candidates(token))
                )
                second = threading.Thread(
                    target=lambda: results.append(state.commons_candidates(token))
                )
                first.start()
                self.assertTrue(first_query_started.wait(timeout=1))
                second.start()
                time.sleep(0.05)
                release_first_query.set()
                first.join(timeout=2)
                second.join(timeout=2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(results, [[], []])
        self.assertEqual(len(calls), 2)

    def test_commons_button_guards_duplicate_and_stale_requests(self) -> None:
        html = _review_html("csrf")
        find_function = html[
            html.index("async function findCommons"):
            html.index("async function chooseCommons")
        ]

        self.assertIn("if(!token||commonsRequestToken===token)return", find_function)
        self.assertIn("button.disabled=true", find_function)
        self.assertIn("if(current?.entry_token!==token)return", find_function)
        self.assertIn("finally", find_function)
        self.assertIn("On demand · 1–2 serial searches.", html)

    def test_review_uses_project_style_and_links_back_to_step_twelve(self) -> None:
        html = _review_html("csrf")

        self.assertIn("--bg:#f6f6f2", html)
        self.assertIn("← Project365 Control Panel", html)
        self.assertIn('href="http://127.0.0.1:8766/?step=import_hub"', html)

    def test_exact_venue_candidates_are_ranked_and_capped_without_comments(self) -> None:
        venue_id = "venue-1"
        target = self._entry("checkin:target", "checkin", 2020, venue_id=venue_id)
        missing_venue = self._entry("checkin:missing", "checkin", 2020, venue_id="")
        comment = self._entry("comment:one", "comment", 2020, venue_id=venue_id)
        sources = [
            self._entry("venue-photo:one", "venue-photo", 2010, venue_id=venue_id,
                        photos=[self._photo("venue-photo", "v", 2010)]),
            self._entry("checkin:near", "checkin", 2019, venue_id=venue_id,
                        photos=[self._photo("near", "checkin", 2019)]),
            self._entry("checkin:far", "checkin", 2012, venue_id=venue_id,
                        photos=[self._photo("far", "checkin", 2012)]),
            self._entry("tip:near", "tip", 2021, venue_id=venue_id,
                        photos=[self._photo("tip", "item", 2021)]),
            self._entry("checkin:fifth", "checkin", 2018, venue_id=venue_id,
                        photos=[self._photo("fifth", "checkin", 2018)]),
        ]

        targets = build_review_targets([target, missing_venue, comment, *sources])

        by_key = {item.entry.source_key: item for item in targets}
        self.assertEqual(set(by_key), {"checkin:target", "checkin:missing"})
        self.assertEqual([candidate.photo.source_id for candidate in by_key["checkin:target"].candidates], [
            "venue-photo", "near", "fifth", "far",
        ])
        self.assertEqual(by_key["checkin:missing"].candidates, ())

    def test_entry_payload_marks_candidates_within_an_inclusive_seven_day_window(self) -> None:
        venue_id = "venue-1"
        target = self._entry("checkin:target", "checkin", 2020, venue_id=venue_id)
        target.created_utc = dt.datetime(2020, 1, 8, 12, tzinfo=dt.UTC)
        source = self._entry(
            "checkin:source",
            "checkin",
            2019,
            venue_id=venue_id,
            photos=[
                self._photo("past-boundary", "checkin", 2019),
                self._photo("future-boundary", "checkin", 2019),
                self._photo("outside-window", "checkin", 2019),
            ],
        )
        source.photos = [
            replace(source.photos[0], created_utc=target.created_utc - dt.timedelta(days=7)),
            replace(source.photos[1], created_utc=target.created_utc + dt.timedelta(days=7)),
            replace(
                source.photos[2],
                created_utc=target.created_utc + dt.timedelta(days=7, seconds=1),
            ),
        ]

        with tempfile.TemporaryDirectory() as temporary:
            state = BackfillReviewState([target, source], Path(temporary))
            candidates = state.entry_payload("pending", 0)["entry"]["local_candidates"]

        same_visit_by_id = {
            candidate["source_url"].rsplit("/", 1)[-1]: candidate["same_visit"]
            for candidate in candidates
        }
        self.assertEqual(same_visit_by_id, {
            "past-boundary": True,
            "future-boundary": True,
            "outside-window": False,
        })

    def test_previous_manual_selection_for_same_venue_can_be_reused(self) -> None:
        first = self._entry("checkin:first", "checkin", 2020, venue_id="venue-1")
        repeat = self._entry("checkin:repeat", "checkin", 2021, venue_id="venue-1")
        unrelated = self._entry("checkin:unrelated", "checkin", 2022, venue_id="venue-2")

        with tempfile.TemporaryDirectory() as temporary:
            state = BackfillReviewState([first, repeat, unrelated], Path(temporary))
            first_token = state.entry_payload("all", 0)["entry"]["entry_token"]
            unrelated_token = state.entry_payload("all", 2)["entry"]["entry_token"]
            state.save_manual(first_token, "first.jpg", b"\xff\xd8\xfffirst-venue")
            state.save_manual(unrelated_token, "other.jpg", b"\xff\xd8\xffother-venue")

            repeat_payload = state.entry_payload("all", 1)["entry"]
            previous = repeat_payload["previous_candidates"]
            self.assertEqual(len(previous), 1)
            self.assertEqual(previous[0]["selected_for_date"], "2020-01-01")
            self.assertTrue(previous[0]["preview_url"].startswith("/media/"))

            state.select_previous(repeat_payload["entry_token"], previous[0]["repeat_token"])

            first_decision = state.store.get(first.source_key)
            repeat_decision = state.store.get(repeat.source_key)
            self.assertEqual(repeat_decision["status"], "selected")
            self.assertEqual(
                repeat_decision["candidate"]["relative_path"],
                first_decision["candidate"]["relative_path"],
            )

    def test_review_visually_distinguishes_same_visit_and_previous_choices(self) -> None:
        html = _review_html("csrf")

        self.assertIn(".candidate.same-visit", html)
        self.assertIn("candidate.same_visit?' same-visit':''", html)
        self.assertIn("Previously selected for this venue", html)
        self.assertIn("choosePrevious", html)

    def test_decisions_are_atomic_reversible_and_do_not_store_absolute_export_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "decisions.json"
            store = BackfillDecisionStore(path)
            candidate = {
                "kind": "export",
                "photo_source_id": "photo-1",
                "source_entry_key": "checkin:source",
                "source_url": "https://example.com/photo",
                "label": "Exact-venue photo from another visit",
            }

            store.select("checkin:target", candidate)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["decisions"]["checkin:target"]["status"], "selected")
            self.assertNotIn("/tmp", path.read_text(encoding="utf-8"))

            store.skip("checkin:target")
            self.assertEqual(store.get("checkin:target")["status"], "skipped")
            store.clear("checkin:target")
            self.assertIsNone(store.get("checkin:target"))

    def test_manual_drop_accepts_supported_image_magic_and_rejects_fake_image(self) -> None:
        self.assertEqual(validate_dropped_image("photo.jpg", b"\xff\xd8\xffpayload"), ".jpg")
        self.assertEqual(validate_dropped_image("", b"\xff\xd8\xffpayload"), ".jpg")
        self.assertEqual(
            validate_dropped_image("photo.png", b"\x89PNG\r\n\x1a\npayload"), ".png"
        )
        with self.assertRaisesRegex(ValueError, "valid JPEG, PNG, or HEIC"):
            validate_dropped_image("photo.jpg", b"not-an-image")

    def test_local_drop_reads_finder_file_items_and_selects_a_supported_image(self) -> None:
        html = _review_html("csrf")
        helper = html[
            html.index("function droppedFileLooksSupported"):
            html.index("function droppedImageUrl")
        ]
        drop_handler = html[
            html.index("async function handleDrop"):
            html.index("function wireDrop")
        ]

        self.assertIn("Array.from(transfer?.files||[])", helper)
        self.assertIn("Array.from(transfer?.items||[])", helper)
        self.assertIn("item.kind==='file'", helper)
        self.assertIn("item.getAsFile()", helper)
        self.assertIn("files.find(droppedFileLooksSupported)", helper)
        self.assertIn("const file=droppedImageFile(event.dataTransfer)", drop_handler)
        self.assertIn("Safari did not provide the dragged file", drop_handler)
        self.assertIn("describeDropTransfer(event.dataTransfer)", drop_handler)

    def test_failed_drop_reports_safaris_exposed_transfer_shape(self) -> None:
        html = _review_html("csrf")
        diagnostic = html[
            html.index("function describeDropTransfer"):
            html.index("function droppedImageUrl")
        ]

        self.assertIn("transfer?.types", diagnostic)
        self.assertIn("item.kind", diagnostic)
        self.assertIn("item.type", diagnostic)
        self.assertIn("transfer?.files", diagnostic)
        self.assertIn("file.name", diagnostic)
        self.assertIn("file.size", diagnostic)

    def test_local_drop_uses_target_phase_listener_and_has_file_picker_fallback(self) -> None:
        html = _review_html("csrf")
        drop_handler = html[
            html.index("async function uploadLocalFile"):
            html.index("function wireDrop")
        ]
        drop_wiring = html[
            html.index("function wireDrop"):
            html.index("filterEl.addEventListener")
        ]

        self.assertIn('id="localImageInput" class="local-image-input" type="file"', html)
        self.assertNotIn('id="localImageInput" class="local-image-input" type="file" hidden', html)
        self.assertIn("async function handleChosenFile(input)", drop_handler)
        self.assertIn("await uploadLocalFile(file)", drop_handler)
        self.assertIn("window.addEventListener(name", drop_wiring)
        self.assertIn("window.addEventListener('drop'", drop_wiring)
        self.assertNotIn("event.stopPropagation()", drop_wiring)
        self.assertNotIn("},true)", drop_wiring)

    def test_empty_safari_drop_points_to_the_file_picker(self) -> None:
        html = _review_html("csrf")
        drop_handler = html[
            html.index("async function handleDrop"):
            html.index("function dropTargetsReviewPane")
        ]

        self.assertIn("Safari did not provide the dragged file", drop_handler)
        self.assertIn("input.focus()", drop_handler)

    def test_safari_drop_uses_a_visible_native_file_input(self) -> None:
        html = _review_html("csrf")
        drop_wiring = html[
            html.index("function wireDrop"):
            html.index("filterEl.addEventListener")
        ]

        self.assertIn(".local-image-input", html)
        self.assertIn("display:block", html)
        self.assertIn("width:100%", html)
        self.assertIn("Drop the image file directly on the native control below", html)
        self.assertIn("event.target?.id==='localImageInput'", drop_wiring)

    def test_local_upload_copies_disk_backed_file_before_fetch(self) -> None:
        html = _review_html("csrf")
        upload_handler = html[
            html.index("async function uploadLocalFile"):
            html.index("async function handleChosenFile")
        ]

        self.assertIn("await file.arrayBuffer()", upload_handler)
        self.assertIn("new Blob([bytes]", upload_handler)
        self.assertIn("body:payload", upload_handler)
        self.assertNotIn("body:file", upload_handler)

    def test_manual_drop_persists_and_advances_the_pending_queue(self) -> None:
        first = self._entry("checkin:first", "checkin", 2020, venue_id="venue-1")
        second = self._entry("checkin:second", "checkin", 2021, venue_id="venue-2")
        payload = b"\xff\xd8\xffrepresentative-photo"
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            state = BackfillReviewState([first, second], state_dir)
            first_token = state.entry_payload("pending", 0)["entry"]["entry_token"]

            state.save_manual(first_token, "maya.jpg", payload)

            decision = state.store.get("checkin:first")
            next_pending = state.entry_payload("pending", 0)
            stored_path = state_dir / decision["candidate"]["relative_path"]
            self.assertEqual(decision["status"], "selected")
            self.assertEqual(decision["candidate"]["kind"], "manual")
            self.assertEqual(stored_path.read_bytes(), payload)
            self.assertEqual(next_pending["entry"]["title"], second.title)
            self.assertEqual(next_pending["count"], 1)

    def test_remote_image_drop_fetches_only_the_image_and_records_its_source(self) -> None:
        class Headers:
            @staticmethod
            def get_content_type() -> str:
                return "image/jpeg"

        class Response:
            headers = Headers()

            def __enter__(self) -> Response:
                return self

            def __exit__(self, *_: object) -> None:
                return None

            @staticmethod
            def geturl() -> str:
                return "https://images.smartshanghai.com.cn/uploads/example.jpg"

            @staticmethod
            def read(_: int) -> bytes:
                return b"\xff\xd8\xffremote-photo"

        entry = self._entry("checkin:target", "checkin", 2020, venue_id="venue-1")
        with tempfile.TemporaryDirectory() as temporary:
            state = BackfillReviewState([entry], Path(temporary))
            token = state.entry_payload("pending", 0)["entry"]["entry_token"]
            public_answer = [
                (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443)),
            ]
            with (
                mock.patch("swarm_photo_backfill_review.socket.getaddrinfo", return_value=public_answer),
                mock.patch("swarm_photo_backfill_review.urllib.request.build_opener") as build,
            ):
                build.return_value.open.return_value = Response()
                state.save_remote(
                    token,
                    "https://images.smartshanghai.com.cn/uploads/example.jpg",
                )

            candidate = state.store.get(entry.source_key)["candidate"]
            self.assertEqual(candidate["kind"], "manual")
            self.assertEqual(
                candidate["source_url"],
                "https://images.smartshanghai.com.cn/uploads/example.jpg",
            )

    def test_commons_candidates_require_allowed_license_and_attribution(self) -> None:
        def page(page_id: int, license_name: str) -> dict:
            return {
                "pageid": page_id,
                "title": f"File:Venue {page_id}.jpg",
                "imageinfo": [{
                    "thumburl": "https://upload.wikimedia.org/example.jpg",
                    "descriptionurl": f"https://commons.wikimedia.org/wiki/File:Venue_{page_id}.jpg",
                    "width": 1200,
                    "height": 800,
                    "extmetadata": {
                        "ObjectName": {"value": "Venue exterior"},
                        "Artist": {"value": "Example photographer"},
                        "LicenseShortName": {"value": license_name},
                        "LicenseUrl": {"value": "//creativecommons.org/licenses/by/4.0/"},
                    },
                }],
            }

        rows = _commons_page_candidates({
            "query": {"pages": [page(1, "CC BY 4.0"), page(2, "All rights reserved")]}
        })

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["author"], "Example photographer")
        self.assertEqual(rows[0]["license"], "CC BY 4.0")
        self.assertEqual(
            rows[0]["license_url"], "https://creativecommons.org/licenses/by/4.0/"
        )

    @staticmethod
    def _photo(source_id: str, related_kind: str, year: int) -> Photo:
        return Photo(
            source_id=source_id,
            created_utc=dt.datetime(year, 1, 1, tzinfo=dt.UTC),
            canonical_url=f"https://example.com/{source_id}",
            related_kind=related_kind,
            related_id="venue-1",
            path=Path(f"/{source_id}.jpg"),
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
            title="Example venue" if venue_id else "Swarm check-in",
            body="",
            latitude=1.0,
            longitude=2.0,
            venue_id=venue_id,
            photos=list(photos or []),
        )


if __name__ == "__main__":
    unittest.main()
