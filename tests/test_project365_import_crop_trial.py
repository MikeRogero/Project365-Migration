from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import project365_import_crop_trial as importer


class CropTrialImportTests(unittest.TestCase):
    def test_feedback_uses_approved_model_or_named_option(self) -> None:
        choices = [{"x": 1}, {"x": 2}, {"x": 3}]
        record = {"entry_id": "entry", "shortlist": choices,
                  "model_review": {"status": "ok", "choice": "C"}}
        self.assertEqual(importer.reviewed_choice(record, {"entry_id": "entry", "verdict": "model_good"}), choices[2])
        self.assertEqual(importer.reviewed_choice(record, {"entry_id": "entry", "verdict": "a_better"}), choices[0])
        self.assertIsNone(importer.reviewed_choice(record, {"entry_id": "entry", "verdict": "needs_manual"}))
        with self.assertRaisesRegex(ValueError, "does not match"):
            importer.reviewed_choice(record, {"entry_id": "other", "verdict": "a_better"})

    def test_proxy_choice_maps_to_source_and_rejects_changed_photo(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            folder = Path(temp_dir)
            (folder / "01").mkdir()
            (folder / "01/original.jpg").write_bytes(b"trial-proxy")
            source = folder / "source.jpg"
            source.write_bytes(b"source")
            record = {"index": 1, "candidate_path": str(source), "proxy_dimensions": [902, 1200]}
            choice = {"x": 0, "y": 149, "size": 902, "angle": 0.0}

            def make_proxy(_source, output):
                output.write_bytes(b"trial-proxy")
                return 902, 1200

            with (mock.patch.object(importer, "proxy_image", side_effect=make_proxy),
                  mock.patch.object(importer, "run", return_value="2320 3088")):
                crop = importer.trial_crop(record, choice, folder)
            self.assertEqual((crop["x"], crop["y"], crop["size"]), (0, 384, 2320))
            self.assertEqual((crop["candidate_width"], crop["candidate_height"]), (2320, 3088))

            (folder / "01/original.jpg").write_bytes(b"changed")
            with (mock.patch.object(importer, "proxy_image", side_effect=make_proxy),
                  mock.patch.object(importer, "run", return_value="2320 3088")):
                with self.assertRaisesRegex(ValueError, "changed"):
                    importer.trial_crop(record, choice, folder)

    def test_import_skips_existing_crop_and_completes_group(self) -> None:
        state = mock.Mock()
        state._stage_missing_crop_estimate.side_effect = [True, False]
        prepared = [("a", "one", {}), ("b", "two", {})]
        self.assertEqual(importer.import_prepared(state, prepared), (1, 1))
        state._mark_estimate_batch_complete.assert_called_once_with(importer.BATCH_ID)


if __name__ == "__main__":
    unittest.main()
