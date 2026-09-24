from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import project365_visual_crop as visual


class VisualCropTests(unittest.TestCase):
    def test_model_preflight_requires_a_loaded_vision_instance(self) -> None:
        model = {"models": [{"key": "qwen/qwen3-vl-8b", "capabilities": {"vision": True},
                              "loaded_instances": []}]}
        with mock.patch.object(visual.urllib.request, "urlopen",
                               return_value=io.BytesIO(json.dumps(model).encode())):
            with self.assertRaisesRegex(ValueError, "Load qwen"):
                visual.loaded_model()
        model["models"][0]["loaded_instances"] = [{"id": "project365-qwen3-vl-8b"}]
        with mock.patch.object(visual.urllib.request, "urlopen",
                               return_value=io.BytesIO(json.dumps(model).encode())):
            self.assertEqual(visual.loaded_model(), "project365-qwen3-vl-8b")

    def test_oriented_proxy_crop_maps_to_oriented_source_pixels(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            candidate = Path(temp_dir) / "portrait.jpg"
            candidate.write_bytes(b"fixture")
            option = {"x": 0, "y": 149, "size": 902, "angle": 0.0,
                      "rank_score": 0.5, "image": "option.jpg"}
            with (
                mock.patch.object(visual, "ensure_vision_binary"),
                mock.patch.object(visual, "proxy_image", return_value=(902, 1200)),
                mock.patch.object(visual, "run", return_value="2320 3088"),
                mock.patch.object(visual, "analyze", return_value={}),
                mock.patch.object(visual, "candidates_for", return_value=[option]),
                mock.patch.object(visual, "score_candidates"),
                mock.patch.object(visual, "shortlist", return_value=[option]),
            ):
                crop = visual.estimate_visual_crop(candidate, model="qwen-test")
            self.assertEqual((crop["candidate_width"], crop["candidate_height"]), (2320, 3088))
            self.assertEqual((crop["x"], crop["y"], crop["size"]), (0, 384, 2320))
            self.assertEqual(crop["_estimate_method"], "vision_fallback")

    def test_model_receives_three_separately_labeled_local_images(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            folder = Path(temp_dir)
            choices = []
            for label in "ABC":
                name = f"{label}.jpg"
                (folder / name).write_bytes(b"jpeg")
                choices.append({"image": name})
            response = {"choices": [{"message": {"content": '{"choice":"B"}'}}]}

            def answer(request, timeout):
                self.assertEqual(request.full_url, visual.MODEL_BASE + "/chat/completions")
                payload = json.loads(request.data)
                parts = payload["messages"][0]["content"]
                self.assertEqual(sum(part["type"] == "image_url" for part in parts), 3)
                self.assertEqual([part["text"] for part in parts if part["type"] == "text"][1:],
                                 ["Image A:", "Image B:", "Image C:"])
                return io.BytesIO(json.dumps(response).encode())

            with mock.patch.object(visual.urllib.request, "urlopen", side_effect=answer):
                self.assertEqual(visual._model_choice(folder, choices, "qwen-test"), "B")


if __name__ == "__main__":
    unittest.main()
