import json
import unittest

from pydub import AudioSegment
from pydub.generators import Sine

from ad_detect import (
    _deterministic_paid_fallback,
    _finish_break,
    _verified_paid_has_commercial_grounding,
    candidate_windows,
    chapter_spans,
    choose_cut_spans,
    classify_transcript,
    cover_neighboring_spots,
    measure_trim,
    parse_model_spans,
    parse_verified_candidate,
    tighten_spans,
    transcript_windows,
    with_show_trim,
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

    def _line_response(self, prompt, key, line_number):
        lines = [line for line in prompt.splitlines() if line[:1].isdigit()]
        if line_number is None or not 1 <= line_number <= len(lines):
            return "{}"
        text = lines[line_number - 1].split(". ", 1)[-1]
        sponsor = prompt.split("The paid advertiser is ", 1)[1].split(". Verified evidence:", 1)[0]
        return json.dumps({"sponsor": sponsor, key: line_number, "evidence_quote": text})

    def _verified(self, sponsor, evidence):
        return json.dumps({
            "classification": "paid_ad",
            "sponsor": sponsor,
            "evidence_quote": evidence,
        })

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
            if "Classify one candidate" in prompt:
                return self._verified("Rora", "Rora as a sponsor")
            if "last line of this same offer" in prompt:
                lines = [line for line in prompt.splitlines() if line[:1].isdigit()]
                if len(lines) <= 3 and any("Again" in line for line in lines):
                    return self._line_response(prompt, "end_line", len(lines))
                return self._line_response(
                    prompt, "end_line", self._line_number(prompt, "rora.com slash Huberman for a discount")
                )
            if "where THAT read starts" in prompt:
                return self._line_response(prompt, "start_line", self._line_number(prompt, "thrilled to have Rora"))
            if "problem stated immediately" in prompt:
                return self._line_response(prompt, "start_line", self._line_number(prompt, "Tap water"))
            return self._scan_rora()

        self.assertEqual(classify_transcript(segments, duration=300, complete=complete), [(10, 58)])

    def test_second_pass_keeps_the_seed_when_the_bound_fails(self):
        segments = [
            {"start": 20, "end": 30, "text": "I am thrilled to have Rora as a sponsor of the show"},
            {"start": 30, "end": 40, "text": "Back to the conversation about leaders"},
        ]

        def complete(prompt):
            if "Classify one candidate" in prompt:
                return self._verified("Rora", "Rora as a sponsor")
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
            if "Classify one candidate" in prompt:
                if "Candidate: 45.0-55.0" in prompt:
                    return self._verified("Eight Sleep", "brought to us by Eight Sleep")
                return self._verified("Rora", "Rora as a sponsor")
            if "ads" not in prompt and "Transcript" not in prompt:
                return _response([
                    {"start": 20, "end": 30, "quote": "thrilled to have Rora as a sponsor"},
                    {"start": 45, "end": 55, "quote": "brought to us by Eight Sleep"},
                ])
            if "last line of this same offer" in prompt:
                lines = [line for line in prompt.splitlines() if line[:1].isdigit()]
                if len(lines) <= 3 and "brought to us by Eight Sleep" not in lines[0]:
                    return self._line_response(prompt, "end_line", 2 if "Again" in lines[1] else 1)
                if lines and "brought to us by Eight Sleep" in lines[0]:
                    return self._line_response(prompt, "end_line", self._line_number(prompt, "eightsleep.com slash Huberman"))
                return self._line_response(prompt, "end_line", self._line_number(prompt, "rora.com slash Huberman for a discount"))
            if "where THAT read starts" in prompt:
                if "Eight Sleep" in prompt and "Tap water" in prompt:
                    return self._line_response(prompt, "start_line", self._line_number(prompt, "thrilled to have Rora"))
                return self._line_response(
                    prompt, "start_line", self._line_number(prompt, "thrilled to have Rora")
                    or self._line_number(prompt, "brought to us by Eight Sleep")
                )
            if "problem stated immediately" in prompt:
                eight = self._line_number(prompt, "brought to us by Eight Sleep")
                if eight is not None:
                    return self._line_response(prompt, "start_line", eight)
                return self._line_response(prompt, "start_line", self._line_number(prompt, "Tap water"))
            return _response([
                {"start": 20, "end": 30, "quote": "thrilled to have Rora as a sponsor"},
                {"start": 45, "end": 55, "quote": "brought to us by Eight Sleep"},
            ])

        self.assertEqual(
            classify_transcript(segments, duration=300, complete=complete),
            [(10, 80)],
        )

    def test_a_bound_longer_than_four_minutes_keeps_the_seed(self):
        segments = [
            {"start": 0, "end": 10, "text": "I am thrilled to have Rora as a sponsor of the show"},
            {"start": 10, "end": 300, "text": "A very long stretch of the interview continues here"},
        ]

        def complete(prompt):
            if "Classify one candidate" in prompt:
                return self._verified("Rora", "Rora as a sponsor")
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
            {"start": 0, "end": 40, "text": "This episode is sponsored by Acme mats today"},
            {"start": 40, "end": 90, "text": "Welcome back to the interview about sleep"},
        ]

        def complete(prompt):
            if "Classify one candidate" in prompt:
                return self._verified("Acme", "sponsored by Acme mats")
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


class SponsorAwareRegressionTest(unittest.TestCase):
    def _paid(self, sponsor, evidence):
        return json.dumps({
            "classification": "paid_ad",
            "sponsor": sponsor,
            "evidence_quote": evidence,
        })

    def _negative(self, classification, evidence):
        return json.dumps({
            "classification": classification,
            "sponsor": "",
            "evidence_quote": evidence,
        })

    def _line_response(self, prompt, key, predicate, last=False):
        lines = [line for line in prompt.splitlines() if line[:1].isdigit()]
        matches = [(index, line.split(". ", 1)[-1]) for index, line in enumerate(lines, 1) if predicate(line)]
        if not matches:
            return "{}"
        index, text = matches[-1 if last else 0]
        sponsor = prompt.split("The paid advertiser is ", 1)[1].split(". Verified evidence:", 1)[0]
        return json.dumps({"sponsor": sponsor, key: index, "evidence_quote": text})

    def test_edge_windows_expose_distinct_preroll_advertisers(self):
        segments = [
            {"start": 0, "end": 28, "text": "Capital One paid sponsor offer"},
            {"start": 30, "end": 60, "text": "Apollo paid sponsor offer"},
            {"start": 500, "end": 510, "text": "Editorial body"},
        ]
        windows = candidate_windows(segments)
        edge = [[item["text"] for item in window] for window in windows if len(window) <= 2]
        self.assertTrue(any("Capital One paid sponsor offer" in window for window in edge))
        self.assertTrue(any("Apollo paid sponsor offer" in window for window in edge))

    def test_capital_one_preroll_and_apollo_start_are_both_preserved(self):
        segments = [
            {"start": 0, "end": 14, "text": "Capital One sponsors this episode"},
            {"start": 14, "end": 28, "text": "Capital One offer ends here"},
            {"start": 30, "end": 45, "text": "Apollo helps sales teams find buyers"},
            {"start": 45, "end": 60, "text": "Visit Apollo dot io for the sponsor offer"},
            {"start": 60, "end": 75, "text": "The interview begins"},
        ]

        def complete(prompt):
            if "candidate discovery" in prompt:
                return _response([
                    {"start": 0, "end": 28, "quote": "Capital One sponsors this episode"},
                    {"start": 45, "end": 60, "quote": "Apollo dot io for the sponsor offer"},
                ])
            if "Classify one candidate" in prompt:
                if "Candidate: 0.0-28.0" in prompt:
                    return self._paid("Capital One", "Capital One sponsors this episode")
                return self._paid("Apollo", "Apollo dot io for the sponsor offer")
            sponsor = "capital one" if "paid advertiser is Capital One" in prompt else "apollo"
            if "start_line" in prompt:
                return self._line_response(prompt, "start_line", lambda line: sponsor in line.lower())
            if "end_line" in prompt:
                return self._line_response(prompt, "end_line", lambda line: sponsor in line.lower(), last=True)
            return "{}"

        spans = classify_transcript(segments, 300, complete)
        self.assertTrue(any(start <= 0 and end >= 60 for start, end in spans))
        self.assertFalse(any(start <= 60 < end for start, end in spans))

    def test_opening_commercial_survives_discovery_omission(self):
        segments = [
            {"start": 0, "end": 10, "text": "Capital One checking has no fees or minimums"},
            {"start": 10, "end": 28, "text": "See Capital One dot com slash bank, member FDIC"},
            {"start": 57, "end": 61, "text": "Apollo dot io, sign up for free"},
            {"start": 64, "end": 75, "text": "Welcome to the news"},
        ]

        def complete(prompt):
            if "candidate discovery" in prompt:
                return _response([{
                    "start": 57,
                    "end": 61,
                    "quote": "Apollo dot io, sign up for free",
                }])
            if "Classify one candidate" in prompt:
                if "Candidate: 0.0-28.0" in prompt:
                    return self._paid("Capital One", "Capital One checking has no fees")
                return self._paid("Apollo", "Apollo dot io, sign up for free")
            sponsor = "capital one" if "paid advertiser is Capital One" in prompt else "apollo"
            if "start_line" in prompt:
                return self._line_response(
                    prompt, "start_line", lambda line: sponsor in line.lower()
                )
            if "end_line" in prompt:
                return self._line_response(
                    prompt, "end_line", lambda line: sponsor in line.lower(), last=True
                )
            return "{}"

        spans = classify_transcript(segments, 300, complete)
        self.assertTrue(any(start <= 0 and end >= 61 for start, end in spans))
        self.assertFalse(any(start < 0 or end > 61 for start, end in spans))

    def test_ringcentral_boundary_does_not_expand_into_credits(self):
        segments = [
            {"start": 0, "end": 20, "text": "Executive producer and editing credits"},
            {"start": 20, "end": 45, "text": "Thanks to our guest and production team"},
            {"start": 45, "end": 60, "text": "RingCentral is the paid sponsor"},
            {"start": 60, "end": 75, "text": "Visit RingCentral dot com for the offer"},
        ]

        def complete(prompt):
            if "candidate discovery" in prompt:
                return _response([{"start": 45, "end": 60, "quote": "RingCentral is the paid sponsor"}])
            if "Classify one candidate" in prompt:
                return self._paid("RingCentral", "RingCentral is the paid sponsor")
            if "start_line" in prompt:
                return self._line_response(prompt, "start_line", lambda line: "ringcentral" in line.lower())
            if "end_line" in prompt:
                return self._line_response(prompt, "end_line", lambda line: "ringcentral" in line.lower(), last=True)
            return "{}"

        self.assertEqual(classify_transcript(segments, 300, complete), [(45, 75)])

    def test_capital_one_postroll_stops_before_acast_cross_promo(self):
        segments = [
            {"start": 100, "end": 115, "text": "Capital One sponsors this closing break"},
            {"start": 115, "end": 130, "text": "Capital One offer ends now"},
            {"start": 130, "end": 150, "text": "An Acast cross-promo for another podcast"},
            {"start": 150, "end": 177, "text": "Follow that Acast show wherever you listen"},
        ]

        def complete(prompt):
            if "candidate discovery" in prompt:
                return _response([
                    {"start": 100, "end": 130, "quote": "Capital One sponsors this closing break"},
                    {"start": 130, "end": 177, "quote": "Acast cross-promo for another podcast"},
                ])
            if "Classify one candidate" in prompt:
                if "Candidate: 130.0-177.0" in prompt:
                    return self._negative("house_promo", "Acast cross-promo for another podcast")
                return self._paid("Capital One", "Capital One sponsors this closing break")
            if "start_line" in prompt:
                return self._line_response(prompt, "start_line", lambda line: "capital one" in line.lower())
            if "end_line" in prompt:
                return self._line_response(prompt, "end_line", lambda line: "capital one" in line.lower(), last=True)
            return "{}"

        self.assertEqual(classify_transcript(segments, 300, complete), [(100, 130)])

    def test_huberman_outro_candidate_is_rejected(self):
        segments = [{"start": 500, "end": 588, "text": "Huberman Lab credits and thank you for listening"}]

        def complete(prompt):
            if "candidate discovery" in prompt:
                return _response([{"start": 500, "end": 588, "quote": "Huberman Lab credits and thank you"}])
            return self._negative("credits_or_outro", "Huberman Lab credits and thank you")

        self.assertEqual(classify_transcript(segments, 600, complete), [])

    def test_intelligence_editorial_and_show_outro_candidates_are_rejected(self):
        segments = [
            {"start": 200, "end": 233, "text": "Our news analysis asks whether advertising influences voters"},
            {"start": 900, "end": 908, "text": "That is all from Intelligence Squared today"},
        ]

        def complete(prompt):
            if "candidate discovery" in prompt:
                return _response([
                    {"start": 200, "end": 233, "quote": "whether advertising influences voters"},
                    {"start": 900, "end": 908, "quote": "all from Intelligence Squared today"},
                ])
            if "Candidate: 200.0-233.0" in prompt:
                return self._negative("editorial", "whether advertising influences voters")
            return self._negative("credits_or_outro", "all from Intelligence Squared today")

        self.assertEqual(classify_transcript(segments, 1000, complete), [])

    def test_known_strong_midroll_sponsors_remain_paid(self):
        for index, sponsor in enumerate(("AG1", "LMNT", "Eight Sleep", "Bombas", "Odoo")):
            with self.subTest(sponsor=sponsor):
                start = 100 + index * 100
                text = f"{sponsor} is today's paid sponsor offer"
                segments = [{"start": start, "end": start + 30, "text": text}]

                def complete(prompt, sponsor=sponsor, text=text, start=start):
                    if "candidate discovery" in prompt:
                        return _response([{"start": start, "end": start + 30, "quote": text}])
                    if "Classify one candidate" in prompt:
                        return self._paid(sponsor, text)
                    return "{}"

                self.assertEqual(classify_transcript(segments, 1200, complete), [(start, start + 30)])

    def test_verifier_requires_an_exact_grounded_quote(self):
        window = [{"start": 10, "end": 20, "text": "Acme is the paid sponsor"}]
        candidate = {"start": 10, "end": 20, "quote": "acme is the paid sponsor"}
        raw = self._paid("Acme", "words not present in transcript")
        self.assertIsNone(parse_verified_candidate(raw, window, candidate))

    def test_verifier_cannot_borrow_evidence_from_an_adjacent_ad(self):
        window = [
            {"start": 0, "end": 10, "text": "We discuss consumer finance today"},
            {"start": 10, "end": 20, "text": "Acme is the paid sponsor offer"},
        ]
        candidate = {"start": 0, "end": 10, "quote": "discuss consumer finance today"}
        raw = self._paid("Acme", "Acme is the paid sponsor offer")
        self.assertIsNone(parse_verified_candidate(raw, window, candidate))

    def test_grounding_and_fallback_cannot_borrow_adjacent_commercial_cues(self):
        context = [
            {"start": 0, "end": 10, "text": "Try a simpler approach to budgeting"},
            {"start": 10, "end": 20, "text": "Visit Acme.com for the paid sponsor offer"},
        ]
        candidate = {"start": 0, "end": 10, "quote": "Try a simpler approach to budgeting"}
        self.assertFalse(_verified_paid_has_commercial_grounding(context, candidate))
        self.assertIsNone(_deterministic_paid_fallback(context, candidate))

    def test_explicit_sponsor_intro_survives_discovery_omission(self):
        segments = [
            {"start": 90, "end": 100, "text": "The interview continues"},
            {"start": 100, "end": 106, "text": "I'd like to acknowledge our sponsor, AG1"},
            {"start": 106, "end": 130, "text": "AG1 supports the offer at drink AG1 dot com"},
            {"start": 130, "end": 140, "text": "Back to the interview"},
        ]

        def complete(prompt):
            if "candidate discovery" in prompt:
                return _response([])
            if "Classify one candidate" in prompt:
                return self._paid("AG1", "acknowledge our sponsor, ag1")
            if "start_line" in prompt:
                return self._line_response(
                    prompt, "start_line", lambda line: "acknowledge our sponsor" in line.lower()
                )
            if "end_line" in prompt:
                return self._line_response(
                    prompt, "end_line", lambda line: "ag1" in line.lower(), last=True
                )
            return "{}"

        self.assertEqual(classify_transcript(segments, 300, complete), [(100, 130)])

    def test_tail_commercial_recovers_gap_after_explicit_outro(self):
        segments = [
            {"start": 0, "end": 20, "text": "Thank you for listening to the show"},
            {"start": 45, "end": 50, "text": "Get started in the Experian app"},
            {"start": 50, "end": 55, "text": "See Experian dot com for details"},
        ]

        def complete(prompt):
            if "candidate discovery" in prompt:
                return _response([{
                    "start": 45,
                    "end": 55,
                    "quote": "Get started in the Experian app",
                }])
            if "Classify one candidate" in prompt:
                return self._paid("Experian", "Get started in the Experian app")
            if "start_line" in prompt:
                return self._line_response(
                    prompt, "start_line", lambda line: "get started" in line.lower()
                )
            if "end_line" in prompt:
                return self._line_response(
                    prompt, "end_line", lambda line: "experian" in line.lower(), last=True
                )
            return "{}"

        self.assertEqual(classify_transcript(segments, 120, complete), [(20, 55)])

    def test_interior_commercial_does_not_delete_untranscribed_gap(self):
        segments = [
            {"start": 100, "end": 120, "text": "Thank you for listening to the show"},
            {"start": 145, "end": 150, "text": "Get started in the Experian app"},
            {"start": 150, "end": 155, "text": "See Experian dot com for details"},
            {"start": 240, "end": 250, "text": "The interview continues"},
        ]

        def complete(prompt):
            if "candidate discovery" in prompt:
                return _response([{
                    "start": 145,
                    "end": 155,
                    "quote": "Get started in the Experian app",
                }])
            if "Classify one candidate" in prompt:
                return self._paid("Experian", "Get started in the Experian app")
            if "start_line" in prompt:
                return self._line_response(prompt, "start_line", lambda line: "get started" in line.lower())
            if "end_line" in prompt:
                return self._line_response(prompt, "end_line", lambda line: "experian" in line.lower(), last=True)
            return "{}"

        self.assertEqual(classify_transcript(segments, 300, complete), [(145, 155)])

    def test_explicit_host_read_survives_verifier_false_negative(self):
        for sponsor in ("AG1", "Eight Sleep"):
            with self.subTest(sponsor=sponsor):
                segments = [
                    {"start": 90, "end": 100, "text": "The interview continues"},
                    {"start": 100, "end": 106, "text": f"I'd like to acknowledge our sponsor, {sponsor}."},
                    {"start": 106, "end": 125, "text": f"{sponsor} is a product I use every day."},
                    {"start": 125, "end": 135, "text": f"Visit {sponsor.replace(' ', '')}.com slash show for the offer."},
                    {"start": 136, "end": 145, "text": "Back to the interview"},
                ]

                def complete(prompt):
                    if "candidate discovery" in prompt:
                        return _response([])
                    if "Classify one candidate" in prompt:
                        return self._negative("editorial", "The interview continues")
                    if "start_line" in prompt:
                        return self._line_response(prompt, "start_line", lambda line: "acknowledge our sponsor" in line.lower())
                    if "end_line" in prompt:
                        return self._line_response(prompt, "end_line", lambda line: "offer" in line.lower(), last=True)
                    return "{}"

                self.assertEqual(classify_transcript(segments, 300, complete), [(100, 135)])

    def test_timestamp_zero_commercial_survives_verifier_false_negative(self):
        segments = [
            {"start": 0, "end": 10, "text": "Capital One checking accounts have no fees or minimums."},
            {"start": 10, "end": 28, "text": "Terms apply. See CapitalOne.com slash bank. Capital One member FDIC."},
            {"start": 57, "end": 61, "text": "Apollo.io, sign up for free."},
            {"start": 64, "end": 75, "text": "Welcome to the news."},
        ]

        def complete(prompt):
            if "candidate discovery" in prompt:
                return _response([])
            if "Classify one candidate" in prompt:
                return self._negative("editorial", "Welcome to the news")
            if "start_line" in prompt:
                return self._line_response(prompt, "start_line", lambda line: "capital one checking" in line.lower())
            if "end_line" in prompt:
                return self._line_response(prompt, "end_line", lambda line: "member fdic" in line.lower())
            return "{}"

        self.assertEqual(classify_transcript(segments, 300, complete), [(0, 28)])

    def test_commercial_fallback_still_rejects_cross_promo_and_outro(self):
        segments = [
            {"start": 100, "end": 120, "text": "That's all for this episode. Thank you for listening."},
            {"start": 120, "end": 145, "text": "Acast has another podcast we recommend."},
            {"start": 145, "end": 165, "text": "Follow the show wherever you listen at acast.com."},
        ]

        def complete(prompt):
            if "candidate discovery" in prompt:
                return _response([{"start": 120, "end": 165, "quote": "Acast has another podcast we recommend"}])
            return self._negative("house_promo", "Acast has another podcast we recommend")

        self.assertEqual(classify_transcript(segments, 300, complete), [])

    def test_tail_problem_first_commercial_recovers_after_explicit_outro(self):
        segments = [
            {"start": 0, "end": 20, "text": "Thank you for your interest in science."},
            {"start": 45, "end": 50, "text": "Over 200 subscriptions are cancelable."},
            {"start": 50, "end": 55, "text": "Get started in the Experian app."},
            {"start": 55, "end": 60, "text": "See Experian.com for details."},
        ]

        def complete(prompt):
            if "candidate discovery" in prompt:
                return _response([{"start": 50, "end": 60, "quote": "Get started in the Experian app"}])
            if "Classify one candidate" in prompt:
                return self._paid("Experian", "Get started in the Experian app")
            if "start_line" in prompt:
                return self._line_response(prompt, "start_line", lambda line: "subscriptions are cancelable" in line.lower())
            if "end_line" in prompt:
                return self._line_response(prompt, "end_line", lambda line: "experian.com" in line.lower(), last=True)
            return "{}"

        self.assertEqual(classify_transcript(segments, 120, complete), [(20, 60)])

    def test_explicit_postroll_expands_past_truncated_model_end(self):
        segments = [
            {"start": 100, "end": 110, "text": "This episode is brought to you by Capital One."},
            {"start": 110, "end": 120, "text": "Capital One deployed an AI car shopping tool."},
            {"start": 120, "end": 130, "text": "It schedules test drives and estimates trade-in value."},
            {"start": 130, "end": 140, "text": "That's technology at Capital One."},
            {"start": 140, "end": 150, "text": "Acast powers the world's best podcasts."},
        ]

        def complete(prompt):
            if "candidate discovery" in prompt:
                return _response([{"start": 100, "end": 110, "quote": "brought to you by Capital One"}])
            if "Classify one candidate" in prompt:
                return self._paid("Capital One", "brought to you by Capital One")
            if "start_line" in prompt:
                return self._line_response(prompt, "start_line", lambda line: "brought to you" in line.lower())
            if "end_line" in prompt:
                return self._line_response(prompt, "end_line", lambda line: "deployed an ai" in line.lower())
            return "{}"

        self.assertEqual(classify_transcript(segments, 200, complete), [(100, 140)])


    def test_cta_only_marks_still_cover_the_sponsor_read(self):
        segments = [
            {"start": 50.7, "end": 53.5, "text": "You're listening to The Human Upgrade with Dave Asprey."},
            {"start": 57.2, "end": 64.8, "text": "You've spent your career building a retirement nest egg."},
            {"start": 64.8, "end": 78.2, "text": "Meet the Simplify Target 15 Distribution ETF, paid monthly."},
            {"start": 78.2, "end": 88.6, "text": "Investing involves risk, including loss of principle. Carefully consider the fund before investing."},
            {"start": 88.9, "end": 95.5, "text": "Visit simplify.us slash nest egg to learn more. Please read the prospectus carefully before you invest."},
            {"start": 95.8, "end": 98.3, "text": "Distributor, Forsythe Financial Services, LLC."},
            {"start": 99.5, "end": 107.4, "text": "You get home late. You're hungry."},
            {"start": 107.6, "end": 134.0, "text": "ButcherBox delivers grass-fed beef to your door."},
            {"start": 134.0, "end": 146.6, "text": "Go to butcherbox.com slash human upgrade for 20 dollars off your first box."},
            {"start": 146.8, "end": 149.4, "text": "Be sure to use the link so they know we sent you and you get the discount."},
            {"start": 150.0, "end": 156.0, "text": "This episode is recorded in the rainforests of the Amazon."},
        ]

        def complete(prompt):
            if "candidate discovery" in prompt:
                return _response([
                    {"start": 64.8, "end": 78.2, "quote": "Meet the Simplify Target 15 Distribution ETF"},
                    {"start": 134.0, "end": 146.6, "quote": "Go to butcherbox.com slash human upgrade"},
                ])
            if "Classify one candidate" in prompt:
                if "simplify target" in prompt.lower():
                    return self._paid("Simplify", "Meet the Simplify Target 15 Distribution ETF")
                if "butcherbox.com" in prompt.lower():
                    return self._paid("ButcherBox", "Go to butcherbox.com slash human upgrade")
                return self._negative("editorial", "You're listening to The Human Upgrade")
            if "start_line" in prompt:
                if "paid advertiser is ButcherBox" in prompt:
                    return self._line_response(prompt, "start_line", lambda line: "butcherbox.com" in line.lower())
                return self._line_response(prompt, "start_line", lambda line: "simplify target" in line.lower())
            if "end_line" in prompt:
                if "paid advertiser is ButcherBox" in prompt:
                    return self._line_response(prompt, "end_line", lambda line: "butcherbox.com" in line.lower(), last=True)
                return self._line_response(prompt, "end_line", lambda line: "simplify.us" in line.lower(), last=True)
            return "{}"

        spans = classify_transcript(segments, 600, complete)
        self.assertIsNotNone(spans)

        def covers(second):
            return any(start <= second < end for start, end in spans)

        self.assertFalse(covers(52))
        self.assertTrue(covers(60))
        self.assertTrue(covers(92))
        self.assertTrue(covers(104))
        self.assertTrue(covers(140))
        self.assertFalse(covers(152))

    def test_midroll_problem_setup_is_cut_with_the_offer(self):
        segments = [
            {"start": 10, "end": 16, "text": "Where does peak span happen in that world?"},
            {"start": 16, "end": 28, "text": "Your skin doesn't suddenly get old. Maybe your skin is drier."},
            {"start": 28, "end": 40, "text": "The new Rich Cream by One Skin targets these changes."},
            {"start": 40, "end": 48, "text": "For a limited time, try it with 15% off at oneskin.co slash dave."},
            {"start": 48.2, "end": 55, "text": "The science is advancing so dramatically."},
        ]

        def complete(prompt):
            if "candidate discovery" in prompt:
                return _response([{
                    "start": 40,
                    "end": 48,
                    "quote": "For a limited time, try it with 15% off at oneskin.co slash dave.",
                }])
            if "Classify one candidate" in prompt:
                return self._paid(
                    "One Skin",
                    "For a limited time, try it with 15% off at oneskin.co slash dave.",
                )
            if "start_line" in prompt:
                return self._line_response(prompt, "start_line", lambda line: "15% off" in line.lower())
            if "end_line" in prompt:
                return self._line_response(prompt, "end_line", lambda line: "15% off" in line.lower(), last=True)
            return "{}"

        spans = classify_transcript(segments, 300, complete)

        def covers(second):
            return any(start <= second < end for start, end in spans)

        self.assertFalse(covers(12))
        self.assertTrue(covers(20))
        self.assertTrue(covers(32))
        self.assertTrue(covers(44))
        self.assertFalse(covers(50))


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


class ShowTrimTest(unittest.TestCase):
    def test_trim_extends_a_detected_ad_that_runs_past_the_boundary(self):
        self.assertEqual(
            with_show_trim([(18, 28)], 100, skip_first=20, skip_last=10),
            [(0, 28), (90, 100)],
        )

    def test_trim_is_applied_when_the_model_finds_nothing(self):
        self.assertEqual(with_show_trim([], 380, skip_first=20, skip_last=10), [(0, 20), (370, 380)])


class NeighboringSpotTest(unittest.TestCase):
    def test_a_verified_spot_covers_the_other_messages_in_the_break(self):
        segments = [
            {"start": 60, "end": 70, "text": "This message comes from Capital One."},
            {"start": 70, "end": 78, "text": "What's in your wallet? Terms apply."},
            {"start": 79, "end": 90, "text": "This message comes from the Natural Resources Defense Council."},
            {"start": 90, "end": 100, "text": "Protect America's air at nrdc.org slash first."},
            {"start": 102, "end": 112, "text": "This message comes from Betterment."},
            {"start": 112, "end": 120, "text": "Learn more at Betterment.com."},
            {"start": 124, "end": 132, "text": "Oil companies are reporting another quarter of profits."},
        ]

        def complete(prompt):
            if "candidate discovery" in prompt:
                return _response([{"start": 60, "end": 78, "quote": "This message comes from Capital One."}])
            if "Classify one candidate" in prompt:
                if "Candidate: 60.0-78.0" in prompt or "capital one" in prompt.lower():
                    return json.dumps({
                        "classification": "paid_ad",
                        "sponsor": "Capital One",
                        "evidence_quote": "This message comes from Capital One.",
                    })
                return json.dumps({"classification": "editorial", "sponsor": "", "evidence_quote": "news"})
            return "{}"

        spans = classify_transcript(segments, 200, complete)
        self.assertTrue(any(start <= 60 and end >= 120 for start, end in spans))
        self.assertFalse(any(start <= 124 < end for start, end in spans))

    def test_a_produced_spot_covers_the_next_spot_until_the_interview_returns(self):
        segments = [
            {"start": 10, "end": 20, "text": "Call 1-833-589-PAIN or visit Genavix.com."},
            {"start": 20.4, "end": 28, "text": "The answer is Ecolab."},
            {"start": 28, "end": 36, "text": "Ecolab. Now every location is your best location."},
            {"start": 36.4, "end": 42, "text": "Martin, it's nice to talk to you again."},
        ]
        covered = cover_neighboring_spots(segments, [(10, 20)])
        self.assertEqual(covered, [(10, 36)])

    def test_a_code_and_url_host_read_is_cut_when_the_model_returns_nothing(self):
        segments = [
            {"start": 100, "end": 112, "text": "Every unit of matter contains quantum energy."},
            {"start": 112, "end": 126, "text": "Try it free at quantumupgrade.io and use the code BEN15."},
            {"start": 126, "end": 136, "text": "That's quantumupgrade.io. Use the code BEN15."},
            {"start": 142, "end": 152, "text": "Rory Vaden runs a group called Brand Builders."},
        ]
        spans = classify_transcript(segments, 400, lambda prompt: "{}")
        self.assertTrue(any(start <= 112 and end >= 136 for start, end in spans))
        self.assertFalse(any(start <= 142 < end for start, end in spans))

    def test_a_branded_preroll_is_cut_before_the_host_starts(self):
        segments = [
            {"start": 0, "end": 8, "text": "Morningstar helps clear the way for investors."},
            {"start": 8, "end": 16, "text": "Helping bring clarity across public and private markets."},
            {"start": 16, "end": 24, "text": "Morningstar. Nothing in your way."},
            {"start": 27, "end": 34, "text": "Hey listeners, it's Saturday. I'm Jack Pitcher."},
        ]
        spans = classify_transcript(segments, 120, lambda prompt: "{}")
        self.assertTrue(any(start <= 1 and 20 <= end <= 26 for start, end in spans))
        self.assertFalse(any(start <= 27 < end for start, end in spans))

    def test_a_branded_preroll_continues_through_credit_ratings_to_the_tagline(self):
        segments = [
            {"start": 0, "end": 8, "text": "Morningstar helps investors with data and credit ratings."},
            {"start": 8, "end": 16, "text": "So you can move forward with conviction."},
            {"start": 17.5, "end": 24, "text": "Morningstar. Nothing in your way."},
            {"start": 27, "end": 34, "text": "Hey listeners, it's Saturday. I'm Jack Pitcher."},
        ]
        spans = classify_transcript(segments, 120, lambda prompt: "{}")
        self.assertTrue(any(start <= 1 and 20 <= end <= 26 for start, end in spans))
        self.assertFalse(any(start <= 27 < end for start, end in spans))

    def test_a_cold_open_without_a_tagline_is_left_alone(self):
        segments = [
            {"start": 0, "end": 12, "text": "I met a man who said I used to be hundreds of pounds heavier."},
            {"start": 12, "end": 24, "text": "What happened is my best friend's house burnt down and he had kids."},
            {"start": 24, "end": 36, "text": "I realized that if my house caught fire I could not carry anyone."},
            {"start": 40, "end": 48, "text": "Welcome to the show with your host."},
        ]
        self.assertEqual(classify_transcript(segments, 200, lambda prompt: "{}"), [])

    def test_a_cold_open_that_repeats_an_ordinary_word_is_left_alone(self):
        segments = [
            {"start": 0, "end": 8, "text": "My name is Ben Greenfield, and on this episode of the Boundless Life podcast."},
            {"start": 8, "end": 16, "text": "And so it's not suddenly I have more willpower."},
            {"start": 16, "end": 24, "text": "It's not suddenly I'm a disciplined person."},
            {"start": 28, "end": 36, "text": "Welcome to the show with your host."},
        ]
        self.assertEqual(classify_transcript(segments, 200, lambda prompt: "{}"), [])

    def test_the_next_spot_stays_cut_after_the_first_url(self):
        segments = [
            {"start": 10, "end": 20, "text": "Visit Genavix.com and use the code PAIN."},
            {"start": 20.4, "end": 28, "text": "The answer is Ecolab."},
            {"start": 28, "end": 36, "text": "Ecolab. Now every location is your best location."},
            {"start": 36.4, "end": 42, "text": "Martin, it's nice to talk to you again."},
        ]
        spans = classify_transcript(segments, 200, lambda prompt: "{}")
        self.assertTrue(any(start <= 20 and end >= 36 for start, end in spans))
        self.assertFalse(any(start <= 36.4 < end for start, end in spans))

    def test_news_after_the_offer_is_left_in(self):
        segments = [
            {"start": 60, "end": 70, "text": "This message comes from Capital One."},
            {"start": 70, "end": 80, "text": "Learn more at CapitalOne.com. Terms apply."},
            {"start": 82, "end": 92, "text": "Oil companies are reporting another quarter of sky-high profits."},
            {"start": 92, "end": 100, "text": "Thanks for being with us, Willa."},
            {"start": 100, "end": 106, "text": "Thanks for having me."},
        ]
        self.assertEqual(_finish_break(segments, (60, 106)), (60, 80))

    def test_a_long_pause_keeps_the_tease_before_the_closing_break(self):
        segments = [
            {"start": 954, "end": 965, "text": "Tomorrow on the Sunday Story, the president's party loses seats."},
            {"start": 965, "end": 978, "text": "Thank you for listening and for supporting your local NPR station."},
            {"start": 999, "end": 1010, "text": "Stay tuned for the following announcements and messages from our sponsors."},
            {"start": 1048, "end": 1053, "text": "Use offer code DIFFERENCE for 50% off select plans."},
        ]
        self.assertEqual(_finish_break(segments, (954, 1053)), (999, 1053))

    def test_a_message_before_a_long_break_is_cut_on_its_own(self):
        segments = [
            {"start": 1961, "end": 1964, "text": "When we come back, we'll have some more questions."},
            {"start": 1967, "end": 1971, "text": "This message comes from Mercury, a lead sponsor of Machine Gods."},
            {"start": 1971, "end": 1976, "text": "Mercury is radically different banking. It is fast and simple."},
            {"start": 1976, "end": 1985, "text": "You can pay bills and set spending limits."},
            {"start": 1985.8, "end": 1991.7, "text": "Loved by entrepreneurs, Mercury is banking designed for your high standards."},
            {"start": 1991.9, "end": 1995, "text": "Visit Mercury.com to learn more and apply online in minutes."},
            {"start": 1995, "end": 2002, "text": "Banking services provided through Choice Financial Group, members FDIC."},
            {"start": 2002, "end": 2006, "text": "This message comes from LinkedIn ads."},
            {"start": 2006, "end": 2086, "text": "Advertise on LinkedIn. Just go to linkedin.com slash NPR pod. Terms apply."},
            {"start": 2087, "end": 2094, "text": "Welcome back. Kevin, what's next up?"},
        ]

        def complete(prompt):
            if "candidate discovery" in prompt:
                return _response([{"start": 2002, "end": 2086, "quote": "This message comes from LinkedIn ads."}])
            if "Classify one candidate" in prompt:
                if "linkedin" in prompt.lower():
                    return json.dumps({
                        "classification": "paid_ad",
                        "sponsor": "LinkedIn",
                        "evidence_quote": "This message comes from LinkedIn ads.",
                    })
                return json.dumps({"classification": "editorial", "sponsor": "", "evidence_quote": "not an ad"})
            return "{}"

        spans = classify_transcript(segments, 2200, complete)
        self.assertTrue(any(start <= 1967 and end >= 2002 for start, end in spans))
        self.assertFalse(any(start <= 1961 < end for start, end in spans))
        self.assertFalse(any(start <= 2087 < end for start, end in spans))

    def test_the_host_after_the_sponsor_url_is_left_in(self):
        segments = [
            {"start": 3057, "end": 3062, "text": "This message comes from Capella University."},
            {"start": 3062, "end": 3070, "text": "Earn your degree at your pace without putting life on pause."},
            {"start": 3070, "end": 3072, "text": "Learn more at capella.edu."},
            {"start": 3073, "end": 3080, "text": "I'm a little underslept today because I saw Phoebe Bridgers."},
            {"start": 3080, "end": 3111, "text": "It was a phone-free show at Chase Center."},
        ]

        def complete(prompt):
            if "candidate discovery" in prompt:
                return _response([{
                    "start": 3057,
                    "end": 3111,
                    "quote": "This message comes from Capella University. Learn more at capella.edu.",
                }])
            if "Classify one candidate" in prompt:
                return json.dumps({
                    "classification": "paid_ad",
                    "sponsor": "Capella",
                    "evidence_quote": "Learn more at capella.edu.",
                })
            return "{}"

        spans = classify_transcript(segments, 3200, complete)
        self.assertTrue(any(start <= 3057 and 3070 <= end <= 3073 for start, end in spans))
        self.assertFalse(any(start <= 3073 < end for start, end in spans))

    def test_a_later_sponsor_stays_cut_through_its_own_url(self):
        segments = [
            {"start": 3052, "end": 3055, "text": "Just go to linkedin.com slash nprpod."},
            {"start": 3055, "end": 3057, "text": "Terms and conditions apply."},
            {"start": 3057, "end": 3060, "text": "This message comes from Capella University."},
            {"start": 3060, "end": 3069, "text": "That spark you feel is your drive for more."},
            {"start": 3069, "end": 3071, "text": "Learn more at capella.edu."},
            {"start": 3073, "end": 3080, "text": "I'm a little underslept today because I saw Phoebe Bridgers."},
        ]
        self.assertEqual(_finish_break(segments, (3052, 3080)), (3052, 3071))

    def test_host_words_on_the_sponsor_intro_line_are_left_in(self):
        segments = [
            {
                "start": 2973.52,
                "end": 2980.48,
                "text": "love to hear those too this message comes from doppel a lead sponsor of machine gods",
            },
            {"start": 2981.02, "end": 3003.32, "text": "Learn more at doppel.com."},
        ]
        start, end = _finish_break(segments, (2973.52, 3003.32))
        self.assertGreater(start, 2975)
        self.assertLess(start, 2977)
        self.assertEqual(end, 3003.32)

    def test_the_signoff_before_the_closing_ads_is_left_in(self):
        segments = [
            {"start": 3180, "end": 3186, "text": "Email hey at machine gods dot fm or leave a comment on our socials."},
            {"start": 3186, "end": 3192, "text": "Please don't forget to like and subscribe and until next time stay blessed."},
            {"start": 3192, "end": 3200, "text": "This message comes from Capella University."},
            {"start": 3200, "end": 3208, "text": "Learn more at capella.edu."},
            {"start": 3208, "end": 3216, "text": "This message comes from Easy Cater."},
            {"start": 3216, "end": 3224, "text": "Learn more at easycater.com."},
        ]

        self.assertEqual(_finish_break(segments, (3180, 3224)), (3192, 3224))

    def test_an_offer_does_not_swallow_the_guest_introduction(self):
        segments = [
            {"start": 10, "end": 20, "text": "Try cacao bliss at trycacaobliss.com slash ben and get six free gifts."},
            {"start": 20, "end": 28, "text": "That's trycacaobliss.com slash ben."},
            {"start": 28.4, "end": 40, "text": "It's not often that I get to talk to a psychologist who helps people get stuff done."},
            {"start": 40, "end": 52, "text": "The show notes are at bengreenfieldlife.com slash rory."},
            {"start": 52, "end": 58, "text": "Let's do this. Rory, I don't remember how we met."},
        ]
        spans = classify_transcript(segments, 200, lambda prompt: "{}")
        self.assertTrue(any(start <= 20 and end >= 28 for start, end in spans))
        self.assertFalse(any(start <= 28.4 < end for start, end in spans))


class MeasureTrimTest(unittest.TestCase):
    def test_measure_trim_uses_the_shortest_shared_edge(self):
        episodes = [
            {"duration": 100, "spans": [(0, 30), (80, 100)]},
            {"duration": 200, "spans": [(0, 20), (170, 200)]},
            {"duration": 150, "spans": [(0, 25), (140, 150)]},
        ]
        self.assertEqual(measure_trim(episodes), {"skip_first": 20, "skip_last": 10})

    def test_measure_trim_stays_zero_when_an_episode_starts_with_the_show(self):
        episodes = [
            {"duration": 100, "spans": [(0, 30), (90, 100)]},
            {"duration": 100, "spans": [(40, 70)]},
        ]
        self.assertEqual(measure_trim(episodes), {"skip_first": 0, "skip_last": 0})


if __name__ == "__main__":
    unittest.main()
