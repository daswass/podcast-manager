import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pydub.generators import Sine

from pydub import AudioSegment

from podcastsToShokz import playback_filters, process_audio
import podcastsToShokz


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


class IdentTest(unittest.TestCase):
    def test_ident_names_the_folder_the_show_and_the_episode(self):
        self.assertEqual(
            podcastsToShokz.ident_phrase("News", "Up First", "Hegseth impeachment blocked."),
            "News. Up First. Hegseth impeachment blocked.",
        )

    def test_ident_skips_a_blank_part(self):
        self.assertEqual(podcastsToShokz.ident_phrase("Music", "Heldeep Radio", ""), "Music. Heldeep Radio.")

    def test_speak_ident_uses_the_lessac_high_piper_voice(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "ident.wav"
            with patch("podcastsToShokz.PiperVoice", create=True) as voice_cls, patch(
                "podcastsToShokz.piper_model_path",
                create=True,
                return_value=Path("en_US-lessac-high.onnx"),
            ):
                voice = voice_cls.load.return_value

                def synthesize(text, wav_file):
                    wav_file.setnchannels(1)
                    wav_file.setsampwidth(2)
                    wav_file.setframerate(22050)
                    wav_file.writeframes(b"\x00\x00" * 220)

                voice.synthesize_wav.side_effect = synthesize
                podcastsToShokz.speak_ident("News. Up First.", dest)
            loaded = Path(str(voice_cls.load.call_args.args[0])).name
            self.assertEqual(loaded, "en_US-lessac-high.onnx")
            self.assertEqual(voice.synthesize_wav.call_args.args[0], "News. Up First.")
            self.assertGreater(dest.stat().st_size, 0)

    def test_prepend_puts_the_spoken_ident_ahead_of_the_episode(self):
        with tempfile.TemporaryDirectory() as tmp:
            episode = Path(tmp) / "episode.mp3"
            Sine(440).to_audio_segment(duration=500).export(episode, format="mp3").close()
            spoken = []

            def speak(phrase, dest):
                spoken.append(phrase)
                Sine(880).to_audio_segment(duration=400).export(dest, format="wav").close()

            podcastsToShokz.prepend_ident(
                {"category": "News", "podcast_name": "Up First", "episode_title": "Morning"},
                episode,
                speak=speak,
            )
            joined = AudioSegment.from_mp3(episode)
        self.assertEqual(spoken, ["News. Up First. Morning."])
        self.assertGreater(len(joined), 1400)
