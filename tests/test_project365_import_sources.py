from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import project365_import_sources as sources


class ImportSourceSetupTests(unittest.TestCase):
    def test_setup_creates_only_selected_folders_and_persists_choices(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Source Data"
            config = Path(temporary) / "runtime" / "import_sources.json"
            state = sources.configure(root, config, ["facebook", "x_twitter"])

            self.assertTrue(state["configured"])
            self.assertEqual(set(state["enabled"]), {"facebook", "x_twitter"})
            self.assertTrue((root / "Facebook Data Export").is_dir())
            self.assertTrue((root / "Twitter Data Export").is_dir())
            self.assertFalse((root / "Swarm Data Export").exists())
            self.assertEqual(set(sources.status(root, config)["enabled"]), {"facebook", "x_twitter"})
            self.assertEqual(set(sources.configure(root, config, ["facebook", "x_twitter"])["enabled"]),
                             {"facebook", "x_twitter"})

    def test_setup_rejects_unknown_choices_without_creating_folders(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Source Data"
            config = Path(temporary) / "runtime" / "import_sources.json"
            with self.assertRaisesRegex(ValueError, "Unknown import source"):
                sources.configure(root, config, ["facebook", "../outside"])
            self.assertFalse(root.exists())
            self.assertFalse(config.exists())

    def test_existing_sources_default_enabled_before_first_setup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Source Data"
            config = Path(temporary) / "runtime" / "import_sources.json"
            (root / "Project365 Pro Export Zips").mkdir(parents=True)
            (root / "Facebook Data Export").mkdir()
            state = sources.status(root, config)
            self.assertFalse(state["configured"])
            self.assertEqual(set(state["enabled"]), {"project365", "facebook"})
            self.assertFalse(config.exists())

    def test_project365_nested_monthly_zips_are_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Source Data"
            config = Path(temporary) / "runtime" / "import_sources.json"
            archive = root / "Project365 Pro Export Zips" / "2013" / "2013-08.zip"
            archive.parent.mkdir(parents=True)
            archive.write_bytes(b"test")
            self.assertTrue(sources.status(root, config)["sources"]["project365"]["ready"])

    def test_configured_empty_disables_existing_without_deleting_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Source Data"
            config = Path(temporary) / "runtime" / "import_sources.json"
            existing = root / "Facebook Data Export"
            existing.mkdir(parents=True)
            sources.configure(root, config, [])
            self.assertEqual(sources.status(root, config)["enabled"], [])
            self.assertTrue(existing.is_dir())

    def test_invalid_config_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Source Data"
            config = Path(temporary) / "runtime" / "import_sources.json"
            config.parent.mkdir()
            config.write_text(json.dumps({"enabled": ["../outside"]}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Invalid import source settings"):
                sources.status(root, config)

    def test_facebook_and_x_resolve_only_unambiguous_extracted_exports(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Source Data"
            facebook = root / "Facebook Data Export"
            (facebook / "snapshot" / "your_activity_across_facebook" / "posts").mkdir(parents=True)
            (facebook / "snapshot" / "your_activity_across_facebook" / "posts" / "your_posts_1.json").write_text("[]")
            (facebook / "Other Facebook Data Exports" / "html-snapshot").mkdir(parents=True)
            x = root / "Twitter Data Export"
            (x / "archive" / "data").mkdir(parents=True)
            (x / "archive" / "data" / "tweets.js").write_text("[]")
            self.assertEqual(sources.resolve_facebook(facebook)[0], facebook / "snapshot")
            self.assertEqual(sources.resolve_facebook(facebook)[1], facebook / "Other Facebook Data Exports")
            self.assertEqual(sources.resolve_x(x), x / "archive")
            (facebook / "second" / "posts").mkdir(parents=True)
            (facebook / "second" / "posts" / "your_posts_1.json").write_text("[]")
            with self.assertRaisesRegex(ValueError, "multiple Facebook"):
                sources.resolve_facebook(facebook)


if __name__ == "__main__":
    unittest.main()
