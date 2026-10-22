import unittest

from pydub import AudioSegment

from ad_slices import cut_ads, find_ad_spans


class FindAdSpansTest(unittest.TestCase):
    def test_no_sponsor_language_returns_no_spans(self):
        segments = [
            {"start": 0, "end": 10, "text": "Welcome to the show"},
            {"start": 10, "end": 40, "text": "Today we talk about sleep"},
        ]
        self.assertEqual(find_ad_spans(segments), [])

    def test_sponsor_read_ends_at_welcome_back(self):
        segments = [
            {"start": 0, "end": 5, "text": "Hello"},
            {"start": 5, "end": 20, "text": "This episode is sponsored by Acme"},
            {"start": 20, "end": 40, "text": "Use the code at checkout"},
            {"start": 40, "end": 50, "text": "Welcome back to the interview"},
        ]
        self.assertEqual(find_ad_spans(segments), [(5, 40)])

    def test_unclosed_ad_is_capped(self):
        segments = [
            {"start": 10, "end": 20, "text": "Brought to you by Mats"},
            {"start": 20, "end": 4000, "text": "The rest of the interview"},
        ]
        self.assertEqual(find_ad_spans(segments, max_ad_seconds=180), [(10, 190)])

    def test_two_separate_ads(self):
        segments = [
            {"start": 0, "end": 10, "text": "Sponsored by Acme"},
            {"start": 10, "end": 15, "text": "And we're back"},
            {"start": 100, "end": 110, "text": "A quick word from our friends at Beta"},
            {"start": 110, "end": 130, "text": "Now back to the show"},
        ]
        self.assertEqual(find_ad_spans(segments), [(0, 10), (100, 110)])


class CutAdsTest(unittest.TestCase):
    def test_removes_the_middle_span(self):
        audio = AudioSegment.silent(duration=3000)
        cut = cut_ads(audio, [(1, 2)])
        self.assertEqual(len(cut), 2000)

    def test_empty_spans_keep_the_audio(self):
        audio = AudioSegment.silent(duration=1500)
        self.assertEqual(len(cut_ads(audio, [])), 1500)


if __name__ == "__main__":
    unittest.main()
