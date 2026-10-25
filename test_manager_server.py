import errno
import io
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from library import effective_cut_ads, feed_settings, load_library
from manager_server import _DRIVE_FILES_CACHE, _job_flags, _on_headphones, create_app
from podcastsToShokz import TMP_RUNNING_MARKER
from podcastsToShokz import load_podcast_feeds, select_feeds


class Finished:
    def __init__(self, code):
        self.code = code

    def poll(self):
        return self.code


class StillRunning:
    def poll(self):
        return None


def sample_library():
    return {
        "folders": [
            {"name": "News", "cut_ads": True},
            {"name": "Music", "cut_ads": False},
        ],
        "shows": [
            {
                "name": "Up First",
                "feed_id": 1,
                "folder": "News",
                "skip_first": 0,
                "skip_last": 0,
                "playback_speed": 1.4,
                "num_episodes": 1,
                "cut_ads": None,
            }
        ],
    }


class ManagerApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.shows = root / "shows.json"
        self.shows.write_text(json.dumps(sample_library(), indent=2) + "\n")
        self.drive = root / "SWIMBUDS"
        (self.drive / "News").mkdir(parents=True)
        self.marker = root / "podcast_downloader_running.txt"
        self.ledger = root / "ledger.json"
        self.started = []
        self.feed_episodes = {}
        self.feed_meta = {}
        self.meta_calls = 0
        self.app = create_app(
            shows_path=self.shows,
            drive_roots=[self.drive, root / "OpenSwim"],
            marker_paths=[self.marker],
            start_prepare=self._start,
            ledger_path=self.ledger,
            serve_ui=False,
            load_episodes=lambda feed_id: self.feed_episodes.get(int(feed_id), {}),
            load_feed=self._feed_meta,
        )
        self.client_cm = TestClient(self.app)
        self.client = self.client_cm.__enter__()

    def tearDown(self):
        self.client_cm.__exit__(None, None, None)
        self.tmp.cleanup()

    def _feed_meta(self, feed_id):
        self.meta_calls += 1
        return self.feed_meta.get(int(feed_id), {})

    def _start(self, feed_id):
        self.started.append(feed_id)
        return StillRunning(), None

    def test_prepare_is_refused_while_the_nightly_marker_is_live(self):
        self.marker.write_text(f"started=now\npid={os.getpid()}\nepoch=1\n")
        response = self.client.post("/api/shows/1/prepare")
        self.assertEqual(response.status_code, 409)
        self.assertIn("Nightly prepare", response.json()["error"])
        self.assertEqual(self.started, [])

    def _live_marker(self, path):
        path.write_text(f"started=now\npid={os.getpid()}\nepoch=1\n")

    def test_idle_library_has_no_running_job(self):
        self.assertIsNone(self.client.get("/api/library").json()["job"])

    def test_prepare_marker_shows_downloading(self):
        self._live_marker(self.marker)
        self.assertEqual(self.client.get("/api/library").json()["job"], "downloading")

    def test_drive_marker_shows_copying(self):
        self._live_marker(self.drive / "podcast_downloader_running.txt")
        self.assertEqual(self.client.get("/api/library").json()["job"], "copying")

    def test_one_copy_process_holding_both_markers_shows_copying(self):
        self._live_marker(self.marker)
        self._live_marker(self.drive / "podcast_downloader_running.txt")
        self.assertEqual(self.client.get("/api/library").json()["job"], "copying")

    def test_separate_prepare_and_copy_processes_show_both(self):
        downloader = subprocess.Popen(["sleep", "30"])
        copier = subprocess.Popen(["sleep", "30"])
        try:
            self.marker.write_text(f"started=now\npid={downloader.pid}\nepoch=1\n")
            (self.drive / "podcast_downloader_running.txt").write_text(f"started=now\npid={copier.pid}\nepoch=1\n")
            self.assertEqual(self.client.get("/api/library").json()["job"], "both")
        finally:
            downloader.kill()
            copier.kill()
            downloader.wait()
            copier.wait()

    def test_stale_copy_marker_is_idle(self):
        (self.drive / "podcast_downloader_running.txt").write_text("started=old\npid=99999999\nepoch=1\n")
        self.assertIsNone(self.client.get("/api/library").json()["job"])

    def test_tmp_marker_follows_the_process_command(self):
        class Idle:
            def running_feed(self):
                return None

        tmp = Path(TMP_RUNNING_MARKER)
        with patch("manager_server._marker_live", return_value=True), patch("manager_server._marker_command", return_value="python podcastsToShokz.py --prepare"):
            self.assertEqual(_job_flags(Idle(), [tmp], []), "downloading")
        with patch("manager_server._marker_live", return_value=True), patch("manager_server._marker_command", return_value="python podcastsToShokz.py"):
            self.assertEqual(_job_flags(Idle(), [tmp], []), "copying")

    def test_cut_ads_reprocesses_the_show_and_marks_it_preparing(self):
        calls = []

        def start(feed_id, force_slice=False):
            calls.append((feed_id, force_slice))
            return StillRunning(), None

        app = create_app(
            shows_path=self.shows,
            drive_roots=[self.drive],
            marker_paths=[self.marker],
            start_prepare=start,
            ledger_path=self.ledger,
            serve_ui=False,
            load_episodes=lambda feed_id: {},
            load_feed=lambda feed_id: {},
        )
        before = self.shows.read_text()
        with TestClient(app) as client:
            response = client.post("/api/shows/1/cut-ads")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, [(1, True)])
        self.assertEqual(response.json()["shows"][0]["status"], "preparing")
        self.assertEqual(response.json()["shows"][0]["cut_ads"], None)
        self.assertEqual(response.json()["job"], "downloading")
        self.assertEqual(self.shows.read_text(), before)

    def test_prepare_starts_when_nothing_else_is_running(self):
        response = self.client.post("/api/shows/1/prepare")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.started, [1])
        show = response.json()["shows"][0]
        self.assertEqual(show["status"], "preparing")
        self.assertTrue(response.json()["prepare_busy"])
        self.assertEqual(response.json()["job"], "downloading")

    def test_prepare_error_stays_on_the_show(self):
        log = Path(self.tmp.name) / "prepare.log"
        log.write_text("ERROR: Failed to prepare 'Up First' episode 9: boom\n")

        def fail(feed_id):
            self.started.append(feed_id)
            return Finished(1), log

        self.app = create_app(
            shows_path=self.shows,
            drive_roots=[self.drive],
            marker_paths=[self.marker],
            start_prepare=fail,
            ledger_path=self.ledger,
            serve_ui=False,
            load_feed=lambda feed_id: {},
            load_episodes=lambda feed_id: {},
        )
        with TestClient(self.app) as client:
            response = client.post("/api/shows/1/prepare")
            self.assertEqual(response.status_code, 200)
            self.assertIn("boom", response.json()["shows"][0]["error"])
            again = client.get("/api/library")
            self.assertIn("boom", again.json()["shows"][0]["error"])

    def test_pinning_a_show_then_inheriting_again(self):
        version = self.client.get("/api/library").json()["version"]
        pinned = self.client.patch("/api/shows/1", json={"version": version, "cut_ads": False})
        self.assertEqual(pinned.status_code, 200)
        self.assertIs(pinned.json()["shows"][0]["cut_ads"], False)
        inherited = self.client.patch(
            "/api/shows/1",
            json={"version": pinned.json()["version"], "cut_ads": None},
        )
        self.assertEqual(inherited.status_code, 200)
        show = inherited.json()["shows"][0]
        self.assertIsNone(show["cut_ads"])
        self.assertTrue(show["cut_ads_inherited"])
        self.assertTrue(show["effective_cut_ads"])

    def test_show_posted_is_the_newest_episode_date(self):
        library = json.loads(self.shows.read_text())
        library["shows"].append({
            "name": "The Daily",
            "feed_id": 2,
            "folder": "News",
            "skip_first": 0,
            "skip_last": 0,
            "playback_speed": 1.0,
            "num_episodes": 1,
            "cut_ads": None,
        })
        self.shows.write_text(json.dumps(library) + "\n")
        self.feed_episodes[1] = {"Old": 1_000, "New": 5_000}
        self.feed_episodes[2] = {"Only": 3_000}
        self.ledger.write_text(json.dumps({"episodes": {
            "1:9": {"feed_id": 1, "published": 4_000},
            "2:9": {"feed_id": 2, "published": 9_000},
        }}))
        shows = {show["feed_id"]: show for show in self.client.get("/api/library").json()["shows"]}
        self.assertEqual(shows[1]["posted"], 5_000)
        self.assertEqual(shows[2]["posted"], 9_000)
        self.assertEqual([show["feed_id"] for show in self.client.get("/api/library").json()["shows"]], [1, 2])

    def test_show_image_and_site_come_from_feed_metadata(self):
        self.feed_meta[1] = {
            "image": "https://cdn.example/art.jpg",
            "url": "https://www.upfirst.example/show",
        }
        show = self.client.get("/api/library").json()["shows"][0]
        self.assertEqual(show["image"], "https://cdn.example/art.jpg")
        self.assertEqual(show["url"], "https://www.upfirst.example/show")
        calls = self.meta_calls
        again = self.client.get("/api/library").json()["shows"][0]
        self.assertEqual(again["image"], "https://cdn.example/art.jpg")
        self.assertEqual(self.meta_calls, calls)

    def test_missing_or_unsafe_show_links_are_omitted(self):
        self.feed_meta[1] = {"image": "javascript:alert(1)", "url": "  "}
        show = self.client.get("/api/library").json()["shows"][0]
        self.assertIsNone(show["image"])
        self.assertIsNone(show["url"])

    @patch("manager_server.index")
    def test_feed_metadata_uses_artwork_and_the_show_site(self, index):
        from manager_server import _load_show_meta

        index.podcastByFeedId.return_value = {
            "feed": {
                "artwork": "https://cdn.example/art.jpg",
                "image": "https://cdn.example/other.jpg",
                "link": "https://www.example.com/show",
                "url": "https://feeds.example/rss",
            }
        }
        self.assertEqual(
            _load_show_meta(1),
            {"image": "https://cdn.example/art.jpg", "url": "https://www.example.com/show"},
        )

    def test_stale_save_is_refused(self):
        version = self.client.get("/api/library").json()["version"]
        self.shows.write_text(self.shows.read_text().replace("Up First", "Morning"))
        response = self.client.patch(
            "/api/shows/1",
            json={"version": version, "skip_first": 5},
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn("Morning", self.shows.read_text())
        self.assertNotIn('"skip_first": 5', self.shows.read_text())

    def test_duplicate_feed_is_refused(self):
        version = self.client.get("/api/library").json()["version"]
        response = self.client.post(
            "/api/shows",
            json={"version": version, "name": "Copy", "feed_id": 1, "folder": "News"},
        )
        self.assertEqual(response.status_code, 400)
        library = json.loads(self.shows.read_text())
        self.assertEqual(len(library["shows"]), 1)

    def test_folder_with_shows_cannot_be_deleted(self):
        version = self.client.get("/api/library").json()["version"]
        response = self.client.delete("/api/folders/News", params={"version": version})
        self.assertEqual(response.status_code, 400)
        self.assertIn("News", self.shows.read_text())

    def test_invalid_library_is_left_in_place(self):
        self.shows.write_text("{")
        response = self.client.get("/api/library")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.shows.read_text(), "{")
        refused = self.client.post("/api/folders", json={"version": "none", "name": "Heroic"})
        self.assertEqual(refused.status_code, 422)
        self.assertEqual(self.shows.read_text(), "{")

    def test_folder_order_is_saved_and_used_on_the_headphones(self):
        (self.drive / "Music").mkdir()
        (self.drive / "Music" / "Heldeep.mp3").write_bytes(b"a")
        (self.drive / "News" / "Up First.mp3").write_bytes(b"b")
        (self.drive / "Alpha").mkdir()
        (self.drive / "Alpha" / "Notes.mp3").write_bytes(b"c")
        (self.drive / "Field").mkdir()
        (self.drive / "Field" / "Outside.mp3").write_bytes(b"d")
        before = [folder["name"] for folder in self.client.get("/api/headphones").json()["volumes"][0]["folders"]]
        self.assertEqual(before, ["News", "Music", "Alpha", "Field"])
        version = self.client.get("/api/library").json()["version"]
        moved = self.client.patch("/api/folders/Music", json={"version": version, "index": 0})
        self.assertEqual(moved.status_code, 200)
        self.assertEqual([folder["name"] for folder in moved.json()["folders"]], ["Music", "News"])
        after = [folder["name"] for folder in self.client.get("/api/headphones").json()["volumes"][0]["folders"]]
        self.assertEqual(after, ["Music", "News", "Alpha", "Field"])

    def test_headphone_delete_rejects_paths_outside_the_drive(self):
        secret = Path(self.tmp.name) / "secret.mp3"
        secret.write_bytes(b"keep")
        response = self.client.request(
            "DELETE",
            "/api/headphones",
            json={"volume": "SWIMBUDS", "path": "../secret.mp3"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(secret.read_bytes(), b"keep")

    def test_missing_headphone_file_returns_404_and_appledouble_files_stay_hidden(self):
        episode = self.drive / "News" / "Up First.mp3"
        episode.write_bytes(b"audio")
        (self.drive / "News" / "._Up First.mp3").write_bytes(b"hidden")
        (self.drive / "podcast_downloader_running.txt").write_text("pid=1\n")
        listing = self.client.get("/api/headphones").json()
        files = listing["volumes"][0]["folders"][0]["files"]
        self.assertEqual([item["name"] for item in files], ["Up First"])
        self.assertIsNone(files[0]["show"])
        self.assertEqual(files[0]["mtime"], int(episode.stat().st_mtime))
        gone = self.client.request(
            "DELETE",
            "/api/headphones",
            json={"volume": "SWIMBUDS", "path": "News/Up First.mp3"},
        )
        self.assertEqual(gone.status_code, 200)
        self.assertFalse(episode.exists())
        self.assertFalse((self.drive / "News" / "._Up First.mp3").exists())
        missing = self.client.request(
            "DELETE",
            "/api/headphones",
            json={"volume": "SWIMBUDS", "path": "News/Up First.mp3"},
        )
        self.assertEqual(missing.status_code, 404)
        marker = self.client.request(
            "DELETE",
            "/api/headphones",
            json={"volume": "SWIMBUDS", "path": "podcast_downloader_running.txt"},
        )
        self.assertEqual(marker.status_code, 400)
        self.assertTrue((self.drive / "podcast_downloader_running.txt").exists())

    def test_delete_updates_the_library_badge_without_waiting_for_the_cache(self):
        self.feed_episodes[1] = {"Latest_Show": 5_000}
        episode = self.drive / "News" / "Up First - Latest_Show.mp3"
        episode.write_bytes(b"audio")
        _DRIVE_FILES_CACHE.clear()
        self.assertIs(self.client.get("/api/library").json()["shows"][0]["on_headphones"], True)
        gone = self.client.request(
            "DELETE",
            "/api/headphones",
            json={"volume": "SWIMBUDS", "path": "News/Up First - Latest_Show.mp3"},
        )
        self.assertEqual(gone.status_code, 200)
        self.assertFalse(episode.exists())
        self.assertIs(self.client.get("/api/library").json()["shows"][0]["on_headphones"], False)

    def test_delete_reports_a_busy_file_and_leaves_it_in_place(self):
        episode = self.drive / "News" / "Up First.mp3"
        episode.write_bytes(b"audio")
        with patch("manager_server._commit_unlink", side_effect=OSError(errno.EBUSY, "Resource busy")):
            response = self.client.request(
                "DELETE",
                "/api/headphones",
                json={"volume": "SWIMBUDS", "path": "News/Up First.mp3"},
            )
        self.assertEqual(response.status_code, 409)
        self.assertIn("Resource busy", response.json()["error"])
        self.assertTrue(episode.exists())

    def test_headphone_titles_turn_underscores_into_words(self):
        episode = self.drive / "News" / "Up First - Are_Private_Equity_Firms_Plundering_the_U.S._Economy.mp3"
        episode.write_bytes(b"audio")
        os.utime(episode, (1_700_000_000, 1_700_000_000))
        loose = self.drive / "News" / "Notes_from_the_pool.mp3"
        loose.write_bytes(b"audio")
        files = self.client.get("/api/headphones").json()["volumes"][0]["folders"][0]["files"]
        by_name = {item["name"]: item for item in files}
        titled = by_name["Are Private Equity Firms Plundering the U.S. Economy"]
        self.assertEqual(titled["show"], "Up First")
        self.assertEqual(titled["mtime"], 1_700_000_000)
        self.assertIn("Are_Private_Equity", titled["filename"])
        self.assertIsNone(by_name["Notes from the pool"]["show"])
        self.assertNotIn("size", titled)

    def test_headphone_posted_date_uses_the_saved_episode(self):
        filename = "Up First - The_Morning_News.mp3"
        (self.drive / "News" / filename).write_bytes(b"audio")
        self.ledger.write_text(json.dumps({"episodes": {
            "1:5": {
                "feed_id": 1,
                "podcast_name": "Up First",
                "title": "The Morning News?",
                "published": 1_700_000_000,
                "sliced_path": f"/elsewhere/{filename}",
            }
        }}))
        self.feed_episodes[1] = {"The_Morning_News": 1_800_000_000}
        files = self.client.get("/api/headphones").json()["volumes"][0]["folders"][0]["files"]
        self.assertEqual(files[0]["posted"], 1_700_000_000)

    def test_headphone_files_are_newest_posted_first(self):
        folder = self.drive / "News"
        names = {
            "older": "Up First - Older_Show.mp3",
            "newer": "Up First - Newer_Show.mp3",
            "tied_early": "Up First - Tied_Early.mp3",
            "tied_late": "Up First - Tied_Late.mp3",
            "unposted": "Loose_notes.mp3",
        }
        mtimes = {"older": 100, "newer": 50, "tied_early": 200, "tied_late": 300, "unposted": 9_000}
        posted = {"older": 1_000, "newer": 5_000, "tied_early": 3_000, "tied_late": 3_000}
        episodes = {}
        for key, filename in names.items():
            path = folder / filename
            path.write_bytes(b"audio")
            os.utime(path, (mtimes[key], mtimes[key]))
            if key in posted:
                episodes[f"1:{key}"] = {
                    "feed_id": 1,
                    "podcast_name": "Up First",
                    "title": key,
                    "published": posted[key],
                    "sliced_path": f"/elsewhere/{filename}",
                }
        self.ledger.write_text(json.dumps({"episodes": episodes}))
        files = self.client.get("/api/headphones").json()["volumes"][0]["folders"][0]["files"]
        self.assertEqual(
            [item["filename"] for item in files],
            [names["newer"], names["tied_late"], names["tied_early"], names["older"], names["unposted"]],
        )

    def test_headphone_posted_date_uses_the_show_feed_when_the_file_is_new(self):
        (self.drive / "News" / "Up First - Zelenskyy_Mending_Ties.mp3").write_bytes(b"audio")
        self.feed_episodes[1] = {"Zelenskyy_Mending_Ties": 1_729_488_000}
        files = self.client.get("/api/headphones").json()["volumes"][0]["folders"][0]["files"]
        self.assertEqual(files[0]["show"], "Up First")
        self.assertEqual(files[0]["posted"], 1_729_488_000)

    def test_headphone_duration_comes_from_the_audio_metadata(self):
        episode = self.drive / "News" / "Up First - Short_One.mp3"
        episode.write_bytes(b"audio")
        with patch("manager_server._probe_duration", return_value=95) as probe:
            files = self.client.get("/api/headphones").json()["volumes"][0]["folders"][0]["files"]
        self.assertEqual(files[0]["duration"], 95)
        self.assertEqual(files[0]["show"], "Up First")
        probe.assert_called_once()
        with patch("manager_server._probe_duration", return_value=1) as again:
            cached = self.client.get("/api/headphones").json()["volumes"][0]["folders"][0]["files"]
        self.assertEqual(cached[0]["duration"], 95)
        again.assert_not_called()

    def test_library_marks_the_latest_episode_on_the_mounted_drive(self):
        self.feed_episodes[1] = {"Older_Show": 1_000, "Latest_Show": 5_000}
        (self.drive / "News" / "Up First - Older_Show.mp3").write_bytes(b"audio")
        missing = self.client.get("/api/library").json()
        self.assertEqual(missing["headphone_volume"], "SWIMBUDS")
        self.assertIs(missing["shows"][0]["on_headphones"], False)
        (self.drive / "News" / "Up First - Latest_Show.mp3").write_bytes(b"audio")
        _DRIVE_FILES_CACHE.clear()
        present = self.client.get("/api/library").json()
        self.assertIs(present["shows"][0]["on_headphones"], True)

    def test_library_uses_a_newer_ledger_title_than_the_feed(self):
        self.feed_episodes[1] = {"Feed_Story": 1_000}
        self.ledger.write_text(json.dumps({"episodes": {
            "1:9": {
                "feed_id": 1,
                "title": "Ledger Story",
                "published": 9_000,
            }
        }}))
        (self.drive / "News" / "Up First - Feed_Story.mp3").write_bytes(b"audio")
        (self.drive / "Music" / "Up First - Ledger_Story.mp3").parent.mkdir()
        (self.drive / "Music" / "Up First - Ledger_Story.mp3").write_bytes(b"audio")
        _DRIVE_FILES_CACHE.clear()
        show = self.client.get("/api/library").json()["shows"][0]
        self.assertIs(show["on_headphones"], True)

    def test_library_hides_headphone_presence_when_nothing_is_plugged_in(self):
        app = create_app(
            shows_path=self.shows,
            drive_roots=[Path(self.tmp.name) / "Missing"],
            marker_paths=[self.marker],
            ledger_path=self.ledger,
            serve_ui=False,
            load_episodes=lambda feed_id: {"Latest": 1},
            load_feed=lambda feed_id: {},
        )
        with TestClient(app) as client:
            body = client.get("/api/library").json()
        self.assertIsNone(body["headphone_volume"])
        self.assertIsNone(body["shows"][0]["on_headphones"])

    def test_open_swim_is_the_drive_that_counts_when_both_are_plugged_in(self):
        open_swim = Path(self.tmp.name) / "OpenSwim"
        (open_swim / "News").mkdir(parents=True)
        (self.drive / "News" / "Up First - Latest_Show.mp3").write_bytes(b"audio")
        self.feed_episodes[1] = {"Latest_Show": 5_000}
        app = create_app(
            shows_path=self.shows,
            drive_roots=[self.drive, open_swim],
            marker_paths=[self.marker],
            ledger_path=self.ledger,
            serve_ui=False,
            load_episodes=lambda feed_id: self.feed_episodes.get(int(feed_id), {}),
            load_feed=self._feed_meta,
        )
        with TestClient(app) as client:
            body = client.get("/api/library").json()
        self.assertEqual(body["headphone_volume"], "OpenSwim")
        self.assertIs(body["shows"][0]["on_headphones"], False)

    def test_ledger_status_uses_the_newest_episode(self):
        self.ledger.write_text(json.dumps({
            "episodes": {
                "1:10": {"feed_id": 1, "published": 10, "sliced": False, "trimmed_path": "/old.mp3"},
                "1:20": {"feed_id": 1, "published": 20, "sliced": True, "sliced_path": "/new.mp3"},
            }
        }))
        show = self.client.get("/api/library").json()["shows"][0]
        self.assertEqual(show["status"], "sliced")
        self.assertTrue(show["cut_ads_inherited"])
        self.assertTrue(show["effective_cut_ads"])

    def test_archive_plays_local_episodes_and_refuses_other_paths(self):
        root = self.ledger.parent
        sliced = root / "sliced" / "News" / "Up First - Morning.mp3"
        trimmed = root / "trimmed" / "Music" / "Heldeep Radio - Night.mp3"
        original = root / "originals" / "1" / "9.mp3"
        for path, content in ((sliced, b"sliced-audio"), (trimmed, b"trimmed-audio"), (original, b"original-audio")):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        now = time.time()
        os.utime(sliced, (now, now))
        os.utime(trimmed, (now + 5, now + 5))

        listed = self.client.get("/api/archive")
        self.assertEqual(listed.status_code, 200)
        episodes = listed.json()["episodes"]
        self.assertEqual([episode["path"] for episode in episodes], [
            "trimmed/Music/Heldeep Radio - Night.mp3",
            "sliced/News/Up First - Morning.mp3",
        ])
        self.assertEqual(episodes[1]["show"], "Up First")
        self.assertEqual(episodes[1]["name"], "Morning")
        self.assertIn("duration", episodes[1])
        audio = self.client.get("/api/archive/audio", params={"path": episodes[1]["path"]})
        self.assertEqual(audio.status_code, 200)
        self.assertEqual(audio.content, b"sliced-audio")
        blocked = self.client.get("/api/archive/audio", params={"path": "sliced/../../secrets.mp3"})
        self.assertEqual(blocked.status_code, 400)
        outside_root = Path(self.tmp.name + "-outside")
        outside = outside_root / "On Headphones.mp3"
        outside.parent.mkdir()
        self.addCleanup(lambda: shutil.rmtree(outside_root, ignore_errors=True))
        outside.write_bytes(b"headphone-audio")
        link = root / "sliced" / "News" / "Up First - Linked.mp3"
        link.symlink_to(outside)
        linked = self.client.get("/api/archive/audio", params={"path": "sliced/News/Up First - Linked.mp3"})
        self.assertEqual(linked.status_code, 400)
        self.assertEqual(outside.read_bytes(), b"headphone-audio")
        self.assertTrue(_on_headphones(Path("/Volumes/SWIMBUDS/News/Episode.mp3")))
        self.assertTrue(_on_headphones(Path("/Volumes/OpenSwim/Music/Episode.mp3")))
        self.assertFalse(_on_headphones(root / "sliced" / "News" / "Up First - Morning.mp3"))

    def test_archive_includes_audio_length(self):
        root = self.ledger.parent
        episode = root / "sliced" / "News" / "Up First - Measured.mp3"
        episode.parent.mkdir(parents=True, exist_ok=True)
        episode.write_bytes(b"sliced-audio")
        with patch("manager_server._probe_duration", return_value=754) as probe:
            listed = self.client.get("/api/archive")
        self.assertEqual(listed.status_code, 200)
        match = next(item for item in listed.json()["episodes"] if item["name"] == "Measured")
        self.assertEqual(match["duration"], 754)
        self.assertNotIn("_duration_path", match)
        probe.assert_called_once()
        with patch("manager_server._probe_duration", return_value=1) as again:
            cached = self.client.get("/api/archive")
        again_match = next(item for item in cached.json()["episodes"] if item["name"] == "Measured")
        self.assertEqual(again_match["duration"], 754)
        again.assert_not_called()


class ShowListTests(unittest.TestCase):
    def test_seed_inherits_folder_cut_ads(self):
        library = load_library(Path(__file__).resolve().parent / "shows.json")
        folders = {folder["name"]: folder["cut_ads"] for folder in library["folders"]}
        self.assertEqual(
            folders,
            {
                "BioHack": True,
                "Finance": True,
                "News": True,
                "Second Takes": True,
                "Heroic": True,
                "Music": False,
            },
        )
        self.assertEqual(len(library["shows"]), 24)
        heroic = next(show for show in library["shows"] if show["feed_id"] == 842919)
        self.assertIs(heroic["cut_ads"], False)
        self.assertTrue(all(show["cut_ads"] is None for show in library["shows"] if show is not heroic))
        huberman = next(show for show in library["shows"] if show["feed_id"] == 1365758)
        clublife = next(show for show in library["shows"] if show["feed_id"] == 41388)
        deep_house = next(show for show in library["shows"] if show["feed_id"] == 1105989)
        self.assertTrue(effective_cut_ads(library, huberman))
        self.assertFalse(effective_cut_ads(library, clublife))
        self.assertNotIn("slice", feed_settings(huberman, library, "http://example.test/huberman"))
        self.assertFalse(feed_settings(clublife, library, "http://example.test/clublife")["slice"])
        self.assertEqual(deep_house["num_episodes"], 2)
        self.assertEqual(huberman["skip_first"], 120)
        self.assertEqual(huberman["playback_speed"], 1.5)

    def test_select_feeds_keeps_one_show(self):
        feeds = [("Up First", {"id": 1}), ("Heldeep Radio", {"id": 2})]
        self.assertEqual(select_feeds(feeds, "2"), [("Heldeep Radio", {"id": 2})])

    def test_select_feeds_rejects_an_unknown_show(self):
        with self.assertRaises(SystemExit) as raised:
            select_feeds([("Up First", {"id": 1})], 9)
        self.assertEqual(raised.exception.code, 1)

    def test_invalid_shows_file_stops_the_job_without_rewriting_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "shows.json"
            path.write_text("{")
            with patch("podcastsToShokz.SHOWS_PATH", path):
                with redirect_stdout(io.StringIO()):
                    with self.assertRaises(SystemExit) as raised:
                        load_podcast_feeds()
            self.assertEqual(raised.exception.code, 1)
            self.assertEqual(path.read_text(), "{")
