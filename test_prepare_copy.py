import unittest
from pathlib import Path
from unittest.mock import patch

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
        ):
            podcastsToShokz.copy_prepared_show(FEED, ledger, support)
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


if __name__ == "__main__":
    unittest.main()
