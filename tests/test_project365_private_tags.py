from __future__ import annotations

import shutil
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

from project365_original_picker import PickerState
from project365_private_tags import read_private_tag, write_private_tag


XMP = '''<x:xmpmeta xmlns:x="adobe:ns:meta/" xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns:dk="http://www.digikam.org/ns/1.0/" xmlns:lr="http://ns.adobe.com/lightroom/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/"><rdf:RDF><rdf:Description rdf:about=""><dk:TagsList><rdf:Seq><rdf:li>People/Example</rdf:li></rdf:Seq></dk:TagsList><lr:hierarchicalSubject><rdf:Bag><rdf:li>People|Example</rdf:li></rdf:Bag></lr:hierarchicalSubject><dc:subject><rdf:Bag><rdf:li>Example</rdf:li></rdf:Bag></dc:subject></rdf:Description></rdf:RDF></x:xmpmeta>'''


class PrivateTagTests(unittest.TestCase):
    def test_reads_and_updates_sidecar_without_losing_other_tags(self) -> None:
        if shutil.which("exiftool") is None:
            self.skipTest("ExifTool is required for XMP writes")
        with tempfile.TemporaryDirectory() as directory:
            photo = Path(directory) / "working-copy.jpg"
            photo.write_bytes(b"test-photo")
            photo.with_name(photo.name + ".xmp").write_text(XMP)
            self.assertFalse(read_private_tag(photo))
            write_private_tag(photo, True)
            self.assertTrue(read_private_tag(photo))
            sidecar = photo.with_name(photo.name + ".xmp").read_text()
            self.assertIn("People/Example", sidecar)
            self.assertIn("People|Example", sidecar)
            write_private_tag(photo, False)
            self.assertFalse(read_private_tag(photo))
            self.assertIn("People/Example", photo.with_name(photo.name + ".xmp").read_text())

    def test_missing_sidecar_is_not_assumed_public(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            photo = Path(directory) / "working-copy.jpg"
            photo.write_bytes(b"test-photo")
            with self.assertRaises(FileNotFoundError):
                read_private_tag(photo)

    def test_enabling_private_creates_digikam_compatible_sidecar(self) -> None:
        if shutil.which("exiftool") is None:
            self.skipTest("ExifTool is required for XMP writes")
        with tempfile.TemporaryDirectory() as directory:
            photo = Path(directory) / "working-copy.jpg"
            photo.write_bytes(b"test-photo")
            sidecar = photo.with_name(photo.name + ".xmp")

            self.assertTrue(write_private_tag(photo, True))
            self.assertTrue(read_private_tag(photo))
            text = sidecar.read_text()
            self.assertIn("http://www.digikam.org/ns/1.0/", text)
            self.assertIn("http://ns.adobe.com/lightroom/1.0/", text)
            self.assertIn("http://purl.org/dc/elements/1.1/", text)
            self.assertGreaterEqual(text.count(">Private<"), 3)

    def test_disabling_private_does_not_create_missing_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            photo = Path(directory) / "working-copy.jpg"
            photo.write_bytes(b"test-photo")
            sidecar = photo.with_name(photo.name + ".xmp")

            self.assertFalse(write_private_tag(photo, False))
            self.assertFalse(sidecar.exists())

    def test_enabling_private_does_not_replace_malformed_existing_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            photo = Path(directory) / "working-copy.jpg"
            photo.write_bytes(b"test-photo")
            sidecar = photo.with_name(photo.name + ".xmp")
            sidecar.write_text("<invalid")
            state = object.__new__(PickerState)

            with (
                mock.patch.object(state, "_working_copies_for_private_tag", return_value=[photo]),
                self.assertRaises(ET.ParseError),
            ):
                state.set_private("entry", True)
            self.assertEqual(sidecar.read_text(), "<invalid")

    def test_entry_privacy_covers_all_working_copies(self) -> None:
        if shutil.which("exiftool") is None:
            self.skipTest("ExifTool is required for XMP writes")
        with tempfile.TemporaryDirectory() as directory:
            photos = [Path(directory) / f"copy-{number}.jpg" for number in (1, 2)]
            for photo in photos:
                photo.write_bytes(b"test-photo")
                photo.with_name(photo.name + ".xmp").write_text(XMP)
            write_private_tag(photos[1], True)
            state = object.__new__(PickerState)
            with mock.patch.object(state, "_working_copies_for_private_tag", return_value=photos):
                self.assertTrue(state.private_status("entry")["private"])
                state.set_private("entry", False)
                self.assertFalse(any(read_private_tag(photo) for photo in photos))
                state.set_private("entry", True)
                self.assertTrue(all(read_private_tag(photo) for photo in photos))
                photos[1].with_name(photos[1].name + ".xmp").unlink()
                status = state.private_status("entry")
                self.assertTrue(status["available"])
                self.assertTrue(status["private"])
                self.assertEqual(status["missing_sidecar_count"], 1)
                self.assertIn("one working copy has no sidecar", status["message"])
                state.set_private("entry", False)
                self.assertFalse(read_private_tag(photos[0]))
                self.assertFalse(photos[1].with_name(photos[1].name + ".xmp").exists())
                state.set_private("entry", True)
                self.assertTrue(all(read_private_tag(photo) for photo in photos))

    def test_missing_sidecars_are_quiet_when_entry_is_not_private(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            photos = [Path(directory) / f"copy-{number}.jpg" for number in (1, 2)]
            for photo in photos:
                photo.write_bytes(b"test-photo")
            state = object.__new__(PickerState)
            with mock.patch.object(state, "_working_copies_for_private_tag", return_value=photos):
                status = state.private_status("entry")
                self.assertTrue(status["available"])
                self.assertFalse(status["private"])
                self.assertEqual(status["message"], "")
                self.assertEqual(status["missing_sidecar_count"], 2)

    def test_private_status_uses_readable_working_copy_error(self) -> None:
        state = object.__new__(PickerState)
        with mock.patch.object(
            state,
            "_working_copies_for_private_tag",
            side_effect=FileNotFoundError("No current working copies for entry"),
        ):
            status = state.private_status("entry")
        self.assertFalse(status["available"])
        self.assertIsNone(status["private"])
        self.assertEqual(status["message"], "Private is unavailable until working copies are generated.")
