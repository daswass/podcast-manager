import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pydub.generators import Sine

from podcastsToShokz import playback_filters, process_audio


class PlaybackFilterTest(unittest.TestCase):
    def test_a_speed_change_levels_in_the_same_encode(self):
        self.assertEqual(
            playback_filters(1.4, cut=False),
            "atempo=1.4,loudnorm=I=-14:TP=-1.5:LRA=11",
        )

    def test_an_ad_cut_at_normal_speed_levels(self):
        self.assertEqual(
            playback_filters(1.0, cut=True),
            "loudnorm=I=-14:TP=-1.5:LRA=11",
        )

    def test_a_trim_without_a_speed_change_is_not_leveled(self):
        self.assertEqual(playback_filters(1.0, cut=False), "")


class ProcessAudioTest(unittest.TestCase):
    def test_speed_change_asks_for_one_leveled_encode(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "episode.mp3"
            Sine(440).to_audio_segment(duration=200).export(path, format="mp3").close()
            with patch("podcastsToShokz.export_mp3") as export:
                process_audio(
                    {"playback_speed": 1.4, "skip_first": 0, "skip_last": 0},
                    str(path),
                    apply_trim=False,
                    cut=False,
                )
            self.assertEqual(export.call_args.args[2], "atempo=1.4,loudnorm=I=-14:TP=-1.5:LRA=11")

    def test_trim_only_leaves_the_file_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "episode.mp3"
            Sine(440).to_audio_segment(duration=200).export(path, format="mp3").close()
            original = path.read_bytes()
            process_audio(
                {"playback_speed": 1.0, "skip_first": 0, "skip_last": 0},
                str(path),
                apply_trim=True,
                cut=False,
            )
            self.assertEqual(path.read_bytes(), original)
