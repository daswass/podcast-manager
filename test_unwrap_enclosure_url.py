import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from podcastsToShokz import download_episode_file, unwrap_enclosure_url

BEN_GREENFIELD = (
    "https://mgln.ai/e/p1606/prfx.byspotify.com/e/pscrb.fm/rss/p/"
    "traffic.omny.fm/d/clips/e58478bf-2dc2-4cb0-b7e9-afb301219b9a/"
    "3e7cc436-2b1b-42c0-9170-afc701069980/"
    "36465935-8a4c-45e6-a84a-b4e30141c65e/audio.mp3"
    "?utm_source=Podcast&in_playlist=5ba357f9-00a2-4cfb-ad41-afc7010699a1"
)

THE_DAILY = (
    "https://dts.podtrac.com/redirect.mp3/pdst.fm/e/pfx.vpixl.com/6qj4J/"
    "pscrb.fm/rss/p/tracking.swap.fm/track/4jdbJYYRKqK96EGqOzQb/"
    "nyt.simplecastaudio.com/03d8b493-87fc-4bd1-931f-8a8e9b945d8a/"
    "episodes/5d4af67f-8924-4298-8fd7-19a04e793c81/audio/128/default.mp3"
    "?aid=rss_feed&feed=54nAGcIl"
)


class UnwrapEnclosureUrlTest(unittest.TestCase):
    def test_ben_greenfield_drops_spotify_prefixes(self):
        self.assertEqual(
            unwrap_enclosure_url(BEN_GREENFIELD),
            "https://traffic.omny.fm/d/clips/e58478bf-2dc2-4cb0-b7e9-afb301219b9a/"
            "3e7cc436-2b1b-42c0-9170-afc701069980/"
            "36465935-8a4c-45e6-a84a-b4e30141c65e/audio.mp3"
            "?utm_source=Podcast&in_playlist=5ba357f9-00a2-4cfb-ad41-afc7010699a1",
        )

    def test_the_daily_drops_pscrb_and_keeps_the_working_tracker(self):
        self.assertEqual(
            unwrap_enclosure_url(THE_DAILY),
            "https://tracking.swap.fm/track/4jdbJYYRKqK96EGqOzQb/"
            "nyt.simplecastaudio.com/03d8b493-87fc-4bd1-931f-8a8e9b945d8a/"
            "episodes/5d4af67f-8924-4298-8fd7-19a04e793c81/audio/128/default.mp3"
            "?aid=rss_feed&feed=54nAGcIl",
        )

    def test_direct_media_url_is_unchanged(self):
        url = "https://traffic.megaphone.fm/SCIM2817913031.mp3"
        self.assertEqual(unwrap_enclosure_url(url), url)

    def test_download_identifies_itself_so_acast_returns_audio(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "episode.mp3"
            response = MagicMock()
            response.iter_content.return_value = [b"a" * 2048]
            response.__enter__.return_value = response
            with patch("podcastsToShokz.requests.get", return_value=response) as get:
                download_episode_file("https://sphinx.acast.com/p/acast/s/show/e/episode/media.mp3", dest)
            headers = get.call_args.kwargs["headers"]
            self.assertNotIn("python-requests", headers["User-Agent"])
            self.assertTrue(headers["User-Agent"])
            self.assertEqual(dest.read_bytes(), b"a" * 2048)

    def test_spotify_prefix_without_pscrb_is_removed(self):
        url = "https://prfx.byspotify.com/e/traffic.omny.fm/d/clips/audio.mp3"
        self.assertEqual(
            unwrap_enclosure_url(url),
            "https://traffic.omny.fm/d/clips/audio.mp3",
        )


if __name__ == "__main__":
    unittest.main()
