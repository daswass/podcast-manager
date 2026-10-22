import json
import unittest

from pydub import AudioSegment
from pydub.generators import Sine

from ad_detect import (
    chapter_spans,
    choose_cut_spans,
    classify_transcript,
    parse_model_spans,
    tighten_spans,
    transcript_windows,
)


def _response(ads):
    return json.dumps({"ads": ads})


class ChapterSpanTest(unittest.TestCase):
    def test_sponsor_chapter_ends_at_the_next_chapter(self):
        chapters = [
            {"startTime": 0, "title": "Intro"},
            {"startTime": 65, "title": "Sponsor: BetterHelp"},
            {"startTime": 140, "title": "Interview"},
        ]
        self.assertEqual(chapter_spans(chapters, duration=600), [(65, 140)])

    def test_ad_title_uses_its_own_end_time(self):
        chapters = [{"startTime": 10, "endTime": 40, "title": "[Ad] Mats"}]
        self.assertEqual(chapter_spans(chapters, duration=600), [(10, 40)])

    def test_content_chapters_are_ignored(self):
        chapters = [
            {"startTime": 0, "title": "The Adaptation"},
            {"startTime": 30, "title": "Additional reading"},
        ]
        self.assertEqual(chapter_spans(chapters, duration=90), [])


class ModelSpanTest(unittest.TestCase):
    def window(self):
        return [
            {"start": 12, "end": 18, "text": "This episode is sponsored by Acme mats"},
            {"start": 18, "end": 30, "text": "Use the link in the show notes"},
            {"start": 30, "end": 48, "text": "Today we talk about sleep"},
        ]

    def test_keeps_a_span_whose_quote_is_in_the_window(self):
        raw = _response([{
            "start": 12,
            "end": 30,
            "quote": "This episode is sponsored by Acme mats",
        }])
        self.assertEqual(parse_model_spans(raw, self.window()), [(12, 30)])

    def test_drops_a_span_whose_quote_is_missing(self):
        raw = _response([{"start": 12, "end": 30, "quote": "brought to you by a brand we never said"}])
        self.assertEqual(parse_model_spans(raw, self.window()), [])

    def test_keeps_a_quote_whose_sentences_appear_in_order(self):
        window = [
            {"start": 10, "end": 14, "text": "To learn more, visit functionhealth.com slash Huberman"},
            {"start": 14, "end": 18, "text": "and use the code Huberman for a credit"},
            {"start": 18, "end": 22, "text": "Again, that's functionhealth.com slash Huberman."},
        ]
        raw = _response([{
            "start": 10,
            "end": 22,
            "quote": (
                "To learn more, visit functionhealth.com slash Huberman. "
                "Again, that's functionhealth.com slash Huberman."
            ),
        }])
        self.assertEqual(parse_model_spans(raw, window), [(10, 22)])

    def test_expand_keeps_the_span_when_the_first_sentence_matches(self):
        window = [
            {"start": 10, "end": 20, "text": "Function provides over 160 advanced lab tests"},
            {"start": 20, "end": 80, "text": "A membership is a dollar a day"},
        ]
        raw = (
            '{"ads": [{"start": 10, "end": 80, "quote": '
            '"Function provides over 160 advanced lab tests. This sentence was never spoken."'
        )
        self.assertEqual(parse_model_spans(raw, window, first_sentence_only=True), [(10, 80)])
        self.assertEqual(parse_model_spans(raw, window), [])

    def test_expand_span_grows_to_cover_the_quoted_sentences(self):
        window = [
            {"start": 10, "end": 14, "text": "I am thrilled to have Rora as a sponsor of this podcast"},
            {"start": 14, "end": 30, "text": "Rora makes the best filters and the water is delicious"},
            {"start": 30, "end": 40, "text": "Go to rora.com slash Huberman for a discount"},
        ]
        raw = _response([{
            "start": 10,
            "end": 14,
            "quote": (
                "I am thrilled to have Rora as a sponsor of this podcast. "
                "Rora makes the best filters and the water is delicious. "
                "Go to rora.com slash Huberman for a discount."
            ),
        }])
        self.assertEqual(parse_model_spans(raw, window, first_sentence_only=True), [(10, 40)])

    def _line_number(self, prompt, snippet):
        for line in prompt.splitlines():
            if snippet in line and line[:1].isdigit():
                return int(line.split(".", 1)[0])
        return None

    def _scan_rora(self):
        return _response([{
            "start": 20,
            "end": 30,
            "quote": "thrilled to have Rora as a sponsor",
        }])

    def test_line_choice_uses_segment_edges_and_the_repeated_url(self):
        segments = [
            {"start": 0, "end": 10, "text": "The interview continues about birds"},
            {"start": 10, "end": 20, "text": "Tap water often contains contaminants"},
            {"start": 20, "end": 30, "text": "I am thrilled to have Rora as a sponsor"},
            {"start": 30, "end": 40, "text": "Rora makes the best filters and the water is delicious"},
            {"start": 40, "end": 50, "text": "Go to rora.com slash Huberman for a discount"},
            {"start": 50, "end": 58, "text": "Again, that's rora.com slash Huberman"},
            {"start": 58, "end": 70, "text": "Back to the conversation about leaders"},
        ]

        def complete(prompt):
            if "last line of this same offer" in prompt:
                lines = [line for line in prompt.splitlines() if line[:1].isdigit()]
                if len(lines) <= 3 and any("Again" in line for line in lines):
                    return json.dumps({"end_line": len(lines)})
                return json.dumps({
                    "end_line": self._line_number(prompt, "rora.com slash Huberman for a discount"),
                })
            if "where THAT read starts" in prompt:
                return json.dumps({
                    "start_line": self._line_number(prompt, "thrilled to have Rora"),
                })
            if "problem stated immediately" in prompt:
                return json.dumps({"start_line": self._line_number(prompt, "Tap water")})
            return self._scan_rora()

        self.assertEqual(classify_transcript(segments, duration=300, complete=complete), [(10, 58)])

    def test_second_pass_keeps_the_seed_when_the_bound_fails(self):
        segments = [
            {"start": 20, "end": 30, "text": "I am thrilled to have Rora as a sponsor of the show"},
            {"start": 30, "end": 40, "text": "Back to the conversation about leaders"},
        ]

        def complete(prompt):
            if '"quote"' in prompt:
                return self._scan_rora()
            return "not json"

        self.assertEqual(classify_transcript(segments, duration=300, complete=complete), [(20, 30)])

    def test_back_to_back_reads_stay_separate(self):
        segments = [
            {"start": 10, "end": 20, "text": "Tap water often contains contaminants"},
            {"start": 20, "end": 30, "text": "I am thrilled to have Rora as a sponsor"},
            {"start": 30, "end": 40, "text": "Go to rora.com slash Huberman for a discount"},
            {"start": 40, "end": 44, "text": "Again, that's rora.com slash Huberman"},
            {"start": 45, "end": 55, "text": "Today's episode is also brought to us by Eight Sleep"},
            {"start": 55, "end": 80, "text": "Visit eightsleep.com slash Huberman to save two hundred dollars"},
            {"start": 80, "end": 90, "text": "Back to the conversation about leaders"},
        ]

        def complete(prompt):
            if "ads" not in prompt and "Transcript" not in prompt:
                return _response([
                    {"start": 20, "end": 30, "quote": "thrilled to have Rora as a sponsor"},
                    {"start": 45, "end": 55, "quote": "brought to us by Eight Sleep"},
                ])
            if "last line of this same offer" in prompt:
                lines = [line for line in prompt.splitlines() if line[:1].isdigit()]
                if len(lines) <= 3 and "brought to us by Eight Sleep" not in lines[0]:
                    return json.dumps({"end_line": 2 if "Again" in lines[1] else 1})
                if lines and "brought to us by Eight Sleep" in lines[0]:
                    return json.dumps({
                        "end_line": self._line_number(prompt, "eightsleep.com slash Huberman"),
                    })
                return json.dumps({
                    "end_line": self._line_number(prompt, "rora.com slash Huberman for a discount"),
                })
            if "where THAT read starts" in prompt:
                if "Eight Sleep" in prompt and "Tap water" in prompt:
                    return json.dumps({
                        "start_line": self._line_number(prompt, "thrilled to have Rora"),
                    })
                return json.dumps({
                    "start_line": self._line_number(prompt, "thrilled to have Rora")
                    or self._line_number(prompt, "brought to us by Eight Sleep"),
                })
            if "problem stated immediately" in prompt:
                eight = self._line_number(prompt, "brought to us by Eight Sleep")
                if eight is not None:
                    return json.dumps({"start_line": eight})
                return json.dumps({"start_line": self._line_number(prompt, "Tap water")})
            return _response([
                {"start": 20, "end": 30, "quote": "thrilled to have Rora as a sponsor"},
                {"start": 45, "end": 55, "quote": "brought to us by Eight Sleep"},
            ])

        self.assertEqual(
            classify_transcript(segments, duration=300, complete=complete),
            [(10, 44), (45, 80)],
        )

    def test_a_bound_longer_than_four_minutes_keeps_the_seed(self):
        segments = [
            {"start": 0, "end": 10, "text": "I am thrilled to have Rora as a sponsor of the show"},
            {"start": 10, "end": 300, "text": "A very long stretch of the interview continues here"},
        ]

        def complete(prompt):
            if "start_line" in prompt:
                return json.dumps({"reads": [{"start_line": 1, "end_line": 2}]})
            return _response([{
                "start": 0,
                "end": 10,
                "quote": "thrilled to have Rora as a sponsor",
            }])

        self.assertEqual(classify_transcript(segments, duration=600, complete=complete), [(0, 10)])

    def test_drops_a_span_longer_than_four_minutes(self):
        window = [
            {"start": 0, "end": 20, "text": "This episode is sponsored by Acme"},
            {"start": 20, "end": 300, "text": "The rest of a very long read"},
        ]
        raw = _response([{"start": 0, "end": 250, "quote": "This episode is sponsored by Acme"}])
        self.assertEqual(parse_model_spans(raw, window), [])

    def test_drops_a_pass_that_marks_more_than_a_third_of_the_episode(self):
        segments = [
            {"start": 0, "end": 30, "text": "This episode is sponsored by Acme mats today"},
            {"start": 30, "end": 90, "text": "Welcome back to the interview about sleep"},
        ]

        def complete(prompt):
            return _response([{"start": 0, "end": 50, "quote": "sponsored by Acme mats"}])

        self.assertEqual(classify_transcript(segments, duration=90, complete=complete), None)

    def test_windows_overlap(self):
        segments = [
            {"start": 0, "end": 10, "text": "open"},
            {"start": 500, "end": 510, "text": "middle"},
            {"start": 1100, "end": 1110, "text": "late"},
        ]
        windows = transcript_windows(segments, window_seconds=600, overlap_seconds=180)
        covered = [segment["text"] for window in windows for segment in window]
        self.assertGreaterEqual(covered.count("middle"), 2)
        self.assertIn("late", covered)


class ChooseCutTest(unittest.TestCase):
    def test_model_spans_are_cut_without_a_phrase_match(self):
        self.assertEqual(choose_cut_spans([(5, 40), (200, 230)], [(100, 130)], []), [(100, 130)])

    def test_keeps_a_publisher_chapter_without_other_detectors(self):
        self.assertEqual(choose_cut_spans([], [], [(10, 40)]), [(10, 40)])

    def test_phrase_span_survives_when_the_model_did_not_run(self):
        self.assertEqual(choose_cut_spans([(5, 40)], None, []), [(5, 40)])

    def test_phrase_only_span_is_dropped_after_the_model_runs(self):
        self.assertEqual(choose_cut_spans([(5, 40)], [], []), [])

    def test_model_span_that_meets_a_chapter_is_kept(self):
        chosen = choose_cut_spans(
            phrase_spans=[],
            model_spans=[(8, 50)],
            chapter_spans=[(10, 40)],
        )
        self.assertEqual(chosen, [(8, 50)])


class TightenSpanTest(unittest.TestCase):
    def test_snaps_edges_to_a_silence_gap(self):
        loud = Sine(220).to_audio_segment(duration=2000, volume=-6)
        audio = loud + AudioSegment.silent(duration=400) + loud
        tightened = tighten_spans(audio, [(1.5, 2.8)])
        self.assertEqual(len(tightened), 1)
        start, end = tightened[0]
        self.assertAlmostEqual(start, 2.0, delta=0.15)
        self.assertAlmostEqual(end, 2.4, delta=0.15)

    def test_snaps_an_edge_to_a_loudness_jump(self):
        quiet = Sine(220).to_audio_segment(duration=2000, volume=-28)
        loud = Sine(220).to_audio_segment(duration=4000, volume=-6)
        tightened = tighten_spans(quiet + loud, [(1.6, 5.5)])
        start, end = tightened[0]
        self.assertAlmostEqual(start, 2.0, delta=0.15)
        self.assertAlmostEqual(end, 5.5, delta=0.15)

    def test_uniform_audio_keeps_the_original_edge(self):
        audio = Sine(220).to_audio_segment(duration=4000, volume=-6)
        self.assertEqual(tighten_spans(audio, [(1, 2)]), [(1, 2)])


if __name__ == "__main__":
    unittest.main()
