from __future__ import annotations

import unittest

from project365_crop_trial import crop_at, crop_fits, proposed_angles, shortlist


class CropTrialTests(unittest.TestCase):
    def test_zero_degree_center_square_uses_full_short_edge(self) -> None:
        self.assertEqual(crop_at(600, 400, 1200, 800, 0), (200, 0, 800))

    def test_rotated_square_has_no_uncovered_source_corners(self) -> None:
        x, y, size = crop_at(600, 400, 1200, 800, 1.5)
        self.assertTrue(crop_fits(x, y, size, 1200, 800, 1.5))
        self.assertLess(size, 800)

    def test_horizon_angle_only_used_inside_small_correction_range(self) -> None:
        self.assertEqual(proposed_angles({"horizon_degrees": 8.75}), [0, -1.5, 1.5])
        self.assertIn(1.2, proposed_angles({"horizon_degrees": 1.2}))

    def test_shortlist_includes_baseline_position_and_straightening(self) -> None:
        candidates = [
            {"name": "center", "angle": 0, "rank_score": 0.1},
            {"name": "objects", "angle": 0, "rank_score": 0.3},
            {"name": "objects", "angle": 1.5, "rank_score": 0.2},
        ]
        self.assertEqual(shortlist(candidates), candidates)


if __name__ == "__main__":
    unittest.main()
