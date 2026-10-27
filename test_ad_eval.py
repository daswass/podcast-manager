import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ad_eval import (
    AD_MODEL, SCHEMA, WHISPER_MODEL, evaluate_production_pipeline,
    initialize_case, load_case, normalize, score,
)


class AdEvalScoreTest(unittest.TestCase):
    def test_perfect_span_has_perfect_coverage_and_boundaries(self):
        report = score([(10, 40)], [(10, 40)], boundary_tolerance=1)
        self.assertEqual(report["precision"], 1.0)
        self.assertEqual(report["recall"], 1.0)
        self.assertTrue(report["all_matched_boundaries_within_tolerance"])

    def test_overlap_reports_boundary_error_and_partial_scores(self):
        report = score([(12, 45)], [(10, 40)], boundary_tolerance=1)
        self.assertAlmostEqual(report["precision"], 28 / 33, places=4)
        self.assertAlmostEqual(report["recall"], 28 / 30, places=4)
        pair = report["boundary_pairs"][0]
        self.assertEqual(pair["start_error_seconds"], 2.0)
        self.assertEqual(pair["end_error_seconds"], 5.0)
        self.assertFalse(pair["both_edges_within_tolerance"])

    def test_missing_prediction_has_zero_recall(self):
        report = score([], [(10, 40)])
        self.assertEqual(report["precision"], 0.0)
        self.assertEqual(report["recall"], 0.0)
        self.assertEqual(report["f1"], 0.0)

    def test_empty_truth_and_prediction_is_correct(self):
        report = score([], [])
        self.assertEqual(report["precision"], 1.0)
        self.assertEqual(report["recall"], 1.0)

    def test_overlapping_and_touching_spans_are_unioned_for_metrics(self):
        spans = [(0, 10), (5, 15), (15, 20)]
        self.assertEqual(normalize(spans), [(0.0, 20.0)])
        report = score(spans, [(0, 20)])
        self.assertEqual(report["predicted_seconds"], 20.0)
        self.assertEqual(report["overlap_seconds"], 20.0)
        self.assertEqual(report["precision"], 1.0)
        self.assertEqual(report["recall"], 1.0)

    def test_boundary_assignment_maximizes_cardinality_before_overlap(self):
        report = score([(1, 18), (0, 8)], [(0, 10), (10, 18)])
        self.assertEqual(report["matched_spans"], 2)
        pairs = {(p["predicted"]["start"], p["reference"]["start"]) for p in report["boundary_pairs"]}
        self.assertEqual(pairs, {(0.0, 0.0), (1.0, 10.0)})

    def test_initialization_records_the_production_models(self):
        with tempfile.TemporaryDirectory() as directory:
            case_path = Path(directory) / "case.json"
            transcript = [{"start": 1, "end": 2, "text": "Sponsor read"}]
            with patch("ad_eval.transcribe_episode", return_value=transcript) as transcribe:
                initialize_case("episode.mp3", case_path, "Episode")
            transcribe.assert_called_once_with("episode.mp3")
            case = load_case(case_path)
        self.assertEqual(case["schema"], SCHEMA)
        self.assertEqual(case["production_models"], {"whisper": WHISPER_MODEL, "ad_agent": AD_MODEL})
        self.assertEqual(case["segments"], transcript)

    def test_production_evaluation_rejects_phrase_fallback(self):
        case = {
            "episode": {"audio_path": "episode.mp3"},
            "pipeline_info": {"chapters": []},
            "reference_ads": [],
        }

        def fallback_render(info, source, destination, segments, trace):
            trace.update({
                "phrase_fallback_used": True,
                "model_failure": "RuntimeError: model unavailable",
                "final_tightened_cut_spans": [(10, 20)],
            })
            return 60.0

        with patch("ad_eval.transcribe_episode", return_value=[]), patch(
            "podcastsToShokz.render_sliced_episode", side_effect=fallback_render
        ):
            with self.assertRaisesRegex(RuntimeError, "phrase fallback was used"):
                evaluate_production_pipeline(case, "rendered.mp3", 5.0)


if __name__ == "__main__":
    unittest.main()
