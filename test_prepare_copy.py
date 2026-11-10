import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

import podcastsToShokz


FEED = [("Huberman Lab", {"id": 10, "category": "BioHack", "num_episodes": 2, "force_latest": 0})]


class MountedDriveTest(unittest.TestCase):
    def test_open_swim_wins_when_both_drives_are_mounted(self):
        mounted = {"/Volumes/OpenSwim/", "/Volumes/SWIMBUDS/"}
        with patch("podcastsToShokz.os.path.isdir", side_effect=lambda path: path in mounted):
            self.assertEqual(podcastsToShokz.mounted_headphone_drive(), "/Volumes/OpenSwim/")

    def test_swimbuds_is_used_when_it_is_the_only_drive(self):
        with patch("podcastsToShokz.os.path.isdir", side_effect=lambda path: path == "/Volumes/SWIMBUDS/"):
            self.assertEqual(podcastsToShokz.mounted_headphone_drive(), "/Volumes/SWIMBUDS/")

    def test_nothing_is_mounted(self):
        with patch("podcastsToShokz.os.path.isdir", return_value=False):
            self.assertIsNone(podcastsToShokz.mounted_headphone_drive())


class CopyPreparedShowTest(unittest.TestCase):
    def test_copies_the_show_onto_the_mounted_drive(self):
        ledger = object()
        support = Path("/tmp/podcast-manager-support")
        with (
            patch("podcastsToShokz.mounted_headphone_drive", return_value="/Volumes/SWIMBUDS/"),
            patch("podcastsToShokz.os.path.exists", return_value=False),
            patch("podcastsToShokz.run_sync") as sync,
            patch("podcastsToShokz.refresh_needs_copy") as refresh,
            patch("podcastsToShokz._sync_dependencies", return_value={"prune": object()}) as deps,
            patch("podcastsToShokz.note_copy") as note,
        ):
            podcastsToShokz.copy_prepared_show(FEED, ledger, support)
        note.assert_called_once_with(None, None)
        sync.assert_called_once_with("/Volumes/SWIMBUDS/", FEED, ledger, deps.return_value)
        deps.assert_called_once_with(support, "/Volumes/SWIMBUDS/")
        refresh.assert_called_once_with(ledger, support / "needs_copy")

    def test_skips_the_copy_when_no_headphones_are_plugged_in(self):
        with (
            patch("podcastsToShokz.mounted_headphone_drive", return_value=None),
            patch("podcastsToShokz.run_sync") as sync,
        ):
            podcastsToShokz.copy_prepared_show(FEED, object(), Path("/tmp/podcast-manager-support"))
        sync.assert_not_called()

    def test_skips_the_copy_while_the_drive_is_already_being_updated(self):
        with (
            patch("podcastsToShokz.mounted_headphone_drive", return_value="/Volumes/OpenSwim/"),
            patch("podcastsToShokz.os.path.exists", return_value=True),
            patch("podcastsToShokz.running_marker_is_stale", return_value=False),
            patch("podcastsToShokz.run_sync") as sync,
        ):
            podcastsToShokz.copy_prepared_show(FEED, object(), Path("/tmp/podcast-manager-support"))
        sync.assert_not_called()


class CopyStatusTest(unittest.TestCase):
    def test_note_copy_records_the_episode_and_clears_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            podcastsToShokz.note_copy("Up First", "Morning edition", tmp)
            status_file = Path(tmp) / "copying.txt"
            self.assertTrue(status_file.is_file())
            self.assertEqual(
                podcastsToShokz.read_copy_status(tmp),
                {"show": "Up First", "title": "Morning edition"},
            )
            podcastsToShokz.note_copy("Up First", "Line one\nLine two", tmp)
            self.assertEqual(podcastsToShokz.read_copy_status(tmp)["title"], "Line one Line two")
            podcastsToShokz.note_copy(None, None, tmp)
            self.assertIsNone(podcastsToShokz.read_copy_status(tmp))
            self.assertFalse(status_file.exists())

    def test_missing_status_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(podcastsToShokz.read_copy_status(tmp))


class FeedLoadTest(unittest.TestCase):
    def test_shows_load_from_the_library_without_calling_podcast_index(self):
        library = {
            "folders": [{"name": "BioHack", "cut_ads": True}],
            "shows": [{
                "name": "Ben Greenfield Life",
                "feed_id": 310787,
                "folder": "BioHack",
                "skip_first": 315,
                "skip_last": 30,
                "playback_speed": 1.5,
                "num_episodes": 1,
                "cut_ads": None,
            }],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "shows.json"
            path.write_text(json.dumps(library))
            with (
                patch("podcastsToShokz.SHOWS_PATH", path),
                patch("podcastsToShokz.index.podcastByFeedId", side_effect=AssertionError("network")),
            ):
                feeds = podcastsToShokz.load_podcast_feeds()
        self.assertEqual(feeds[0][0], "Ben Greenfield Life")
        self.assertEqual(feeds[0][1]["id"], 310787)

    def test_a_dropped_connection_is_retried(self):
        calls = {"count": 0}

        def flaky():
            calls["count"] += 1
            if calls["count"] < 3:
                raise requests.exceptions.ConnectionError("Failed to resolve 'api.podcastindex.org'")
            return {"items": []}

        with patch("podcastsToShokz.time.sleep"):
            result = podcastsToShokz.retry_api_call(flaky, max_retries=3, initial_delay=0)
        self.assertEqual(result, {"items": []})
        self.assertEqual(calls["count"], 3)

    def test_an_authorization_error_is_not_retried(self):
        calls = {"count": 0}

        def denied():
            calls["count"] += 1
            response = requests.Response()
            response.status_code = 401
            raise requests.exceptions.HTTPError("401", response=response)

        with patch("podcastsToShokz.time.sleep"):
            with self.assertRaises(requests.exceptions.HTTPError):
                podcastsToShokz.retry_api_call(denied, max_retries=3, initial_delay=0)
        self.assertEqual(calls["count"], 1)

    def test_a_failed_lookup_leaves_the_headphone_timestamp_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            drive = Path(tmp)
            with (
                patch("podcastsToShokz.load_podcast_feeds", return_value=[("Ben Greenfield Life", {"id": 310787})]),
                patch("podcastsToShokz.EpisodeLedger"),
                patch("podcastsToShokz.run_sync", return_value=False),
                patch("podcastsToShokz.prune_local_archive"),
                patch("podcastsToShokz.refresh_needs_copy"),
                patch("podcastsToShokz.update_last_run_timestamp") as stamp,
                patch("podcastsToShokz.support_dir", return_value=drive),
                patch("podcastsToShokz.note_copy"),
                patch("podcastsToShokz._sync_dependencies", return_value={}),
            ):
                podcastsToShokz._run_podcast_update(str(drive))
        stamp.assert_not_called()


class PrepareMainCopyTest(unittest.TestCase):
    def _run(self, only_feed_id):
        feeds = [("Huberman Lab", {"id": 1365758, "num_episodes": 1})]
        with (
            patch("podcastsToShokz.support_dir", return_value=Path("/tmp/podcast-manager-support")),
            patch("podcastsToShokz.acquire_running_marker", return_value=True),
            patch("podcastsToShokz.remove_running_marker"),
            patch("podcastsToShokz.load_podcast_feeds", return_value=feeds),
            patch("podcastsToShokz.EpisodeLedger"),
            patch("podcastsToShokz.run_prepare"),
            patch("podcastsToShokz.refresh_needs_copy"),
            patch("podcastsToShokz.copy_prepared_show") as copy,
        ):
            podcastsToShokz.prepare_main(only_feed_id=only_feed_id)
        return copy

    def test_one_show_prepare_copies_after_the_local_file_is_ready(self):
        copy = self._run(1365758)
        copy.assert_called_once()
        self.assertEqual(copy.call_args.args[0][0][0], "Huberman Lab")

    def test_nightly_prepare_does_not_copy(self):
        copy = self._run(None)
        copy.assert_not_called()

    def test_cut_ads_marks_the_latest_episode_and_still_copies(self):
        feeds = [("Huberman Lab", {"id": 1365758, "num_episodes": 1, "slice": False})]
        with (
            patch("podcastsToShokz.support_dir", return_value=Path("/tmp/podcast-manager-support")),
            patch("podcastsToShokz.acquire_running_marker", return_value=True),
            patch("podcastsToShokz.remove_running_marker"),
            patch("podcastsToShokz.load_podcast_feeds", return_value=feeds),
            patch("podcastsToShokz.EpisodeLedger"),
            patch("podcastsToShokz.run_prepare") as prepare,
            patch("podcastsToShokz.refresh_needs_copy"),
            patch("podcastsToShokz.copy_prepared_show") as copy,
        ):
            podcastsToShokz.prepare_main(only_feed_id=1365758, force_slice=True)
        info = prepare.call_args.args[0][0][1]
        self.assertTrue(info["force_slice_latest"])
        self.assertFalse(info["slice"])
        copy.assert_called_once()


class CalibrateTrimTest(unittest.TestCase):
    def test_calibrate_saves_the_shortest_shared_trim(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "shows.json"
            path.write_text(json.dumps({
                "folders": [{"name": "News", "cut_ads": True}],
                "shows": [{
                    "name": "Up First",
                    "feed_id": 5,
                    "folder": "News",
                    "skip_first": 0,
                    "skip_last": 0,
                    "playback_speed": 1.0,
                    "num_episodes": 1,
                    "cut_ads": None,
                }],
            }))

            def fetch(feed_id, count):
                return [{"id": 1}, {"id": 2}, {"id": 3}]

            def prepare(episode):
                samples = {
                    1: (100, [(0, 30), (80, 100)]),
                    2: (200, [(0, 20), (170, 200)]),
                    3: (150, [(0, 25), (140, 150)]),
                }
                duration, spans = samples[episode["id"]]
                return {"title": str(episode["id"]), "duration": duration, "spans": spans}

            result = podcastsToShokz.calibrate_show_trim(
                5,
                shows_path=path,
                fetch_episodes=fetch,
                prepare_episode=prepare,
            )
            saved = json.loads(path.read_text())["shows"][0]
        self.assertEqual(result["skip_first"], 20)
        self.assertEqual(result["skip_last"], 10)
        self.assertEqual(saved["skip_first"], 20)
        self.assertEqual(saved["skip_last"], 10)


if __name__ == "__main__":
    unittest.main()
