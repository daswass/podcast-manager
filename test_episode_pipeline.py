import tempfile
import unittest
from pathlib import Path

from episode_ledger import EpisodeLedger, refresh_needs_copy
from episode_pipeline import prepare_plan, run_prepare, run_sync, sync_plan


def episode(episode_id=7, title="Episode"):
    return {
        "id": episode_id,
        "title": title,
        "enclosureUrl": f"https://cdn.example/{episode_id}.mp3",
        "datePublished": 1000 + episode_id,
    }


def feed(num_episodes=1):
    return [("Huberman Lab", {"id": 10, "category": "BioHack", "num_episodes": num_episodes, "force_latest": 0})]


class PlanTest(unittest.TestCase):
    def test_sync_copies_a_sliced_file_without_downloading(self):
        self.assertEqual(sync_plan({"sliced": True, "sliced_path": "/ready.mp3", "downloaded": True, "original_path": "/raw.mp3"}), "copy_sliced")

    def test_sync_trims_a_download_that_is_not_sliced(self):
        self.assertEqual(sync_plan({"downloaded": True, "original_path": "/raw.mp3", "sliced": False}), "trim_and_copy")

    def test_sync_downloads_when_nothing_is_stored(self):
        self.assertEqual(sync_plan(None), "download_trim_copy")

    def test_prepare_skips_a_finished_slice(self):
        self.assertEqual(prepare_plan({"sliced": True, "sliced_path": "/ready.mp3"}), "skip")

    def test_prepare_slices_an_existing_download(self):
        self.assertEqual(prepare_plan({"downloaded": True, "original_path": "/raw.mp3", "sliced": False}), "slice")

    def test_prepare_downloads_music_without_planning_a_slice(self):
        music = {"slice": False}
        self.assertEqual(prepare_plan(None, music), "download")
        self.assertEqual(
            prepare_plan({"downloaded": True, "original_path": "/raw.mp3", "sliced": False}, music),
            "skip",
        )


class PipelineTest(unittest.TestCase):
    def test_prepare_downloads_and_transcribes_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = EpisodeLedger(root / "ledger.json")
            calls = {"download": 0, "transcribe": 0, "timing": []}

            def download(url, dest):
                calls["download"] += 1
                Path(dest).parent.mkdir(parents=True, exist_ok=True)
                Path(dest).write_bytes(b"audio")

            def transcribe(path):
                calls["transcribe"] += 1
                return [{"start": 0, "end": 1, "text": "sponsored by Acme"}]

            def render_sliced(info, source, dest, segments):
                Path(dest).parent.mkdir(parents=True, exist_ok=True)
                Path(dest).write_bytes(b"sliced")
                return 90.0

            deps = {
                "fetch_episodes": lambda info: [episode()],
                "download": download,
                "transcribe": transcribe,
                "render_sliced": render_sliced,
                "original_path": lambda info, ep: root / "original.mp3",
                "sliced_path": lambda info, ep: root / "sliced.mp3",
                "record_timing": lambda row: calls["timing"].append(row),
                "safe_title": lambda title: title,
            }
            run_prepare(feed(), ledger, deps)
            run_prepare(feed(), ledger, deps)

            self.assertEqual(calls["download"], 1)
            self.assertEqual(calls["transcribe"], 1)
            self.assertEqual(len(calls["timing"]), 1)
            self.assertEqual(calls["timing"][0]["podcast"], "Huberman Lab")
            self.assertEqual(calls["timing"][0]["audio_seconds"], 90.0)
            self.assertGreaterEqual(calls["timing"][0]["transcribe_seconds"], 0)
            self.assertGreaterEqual(calls["timing"][0]["slice_seconds"], 0)
            stored = ledger.get(10, 7)
            self.assertTrue(stored["sliced"])
            self.assertFalse(stored["copied_sliced"])

    def test_prepare_passes_publisher_chapters_to_the_slicer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = EpisodeLedger(root / "ledger.json")
            seen = {}
            chapters = [{"startTime": 10, "title": "[Ad] Mats", "endTime": 40}]

            def download(url, dest):
                Path(dest).parent.mkdir(parents=True, exist_ok=True)
                Path(dest).write_bytes(b"audio")

            def render_sliced(info, source, dest, segments):
                seen["chapters"] = info.get("chapters")
                Path(dest).parent.mkdir(parents=True, exist_ok=True)
                Path(dest).write_bytes(b"sliced")
                return 30.0

            deps = {
                "fetch_episodes": lambda info: [episode()],
                "download": download,
                "transcribe": lambda path: [],
                "load_chapters": lambda episode: chapters,
                "render_sliced": render_sliced,
                "original_path": lambda info, ep: root / "original.mp3",
                "sliced_path": lambda info, ep: root / "sliced.mp3",
                "record_timing": lambda row: None,
                "safe_title": lambda title: title,
            }
            run_prepare(feed(), ledger, deps)
            self.assertEqual(seen["chapters"], chapters)

    def test_prepare_downloads_music_without_transcribing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = EpisodeLedger(root / "ledger.json")
            calls = {"download": 0, "transcribe": 0, "trim": 0}
            music = [("Heldeep Radio", {"id": 10, "category": "Music", "num_episodes": 1, "force_latest": 0, "slice": False})]

            def download(url, dest):
                calls["download"] += 1
                Path(dest).parent.mkdir(parents=True, exist_ok=True)
                Path(dest).write_bytes(b"audio")

            def render_trimmed(info, source, dest):
                calls["trim"] += 1
                Path(dest).parent.mkdir(parents=True, exist_ok=True)
                Path(dest).write_bytes(b"trimmed")

            deps = {
                "fetch_episodes": lambda info: [episode()],
                "download": download,
                "transcribe": lambda path: calls.__setitem__("transcribe", calls["transcribe"] + 1),
                "render_sliced": lambda info, source, dest, segments: (_ for _ in ()).throw(AssertionError("music was sliced")),
                "render_trimmed": render_trimmed,
                "original_path": lambda info, ep: root / "original.mp3",
                "sliced_path": lambda info, ep: root / "sliced.mp3",
                "trimmed_path": lambda info, ep: root / "trimmed.mp3",
                "record_timing": lambda row: None,
                "safe_title": lambda title: title,
            }
            run_prepare(music, ledger, deps)
            run_prepare(music, ledger, deps)

            self.assertEqual(calls["download"], 1)
            self.assertEqual(calls["transcribe"], 0)
            self.assertEqual(calls["trim"], 1)
            stored = ledger.get(10, 7)
            self.assertFalse(stored["sliced"])
            self.assertEqual(stored["trimmed_path"], str(root / "trimmed.mp3"))

    def test_sync_copies_sliced_audio_and_does_not_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sliced = root / "sliced.mp3"
            sliced.write_bytes(b"sliced-audio")
            drive = root / "drive"
            ledger = EpisodeLedger(root / "ledger.json")
            ledger.update(10, 7, downloaded=True, original_path=str(root / "raw.mp3"), sliced=True, sliced_path=str(sliced), copied_sliced=False, title="Episode")
            calls = {"download": 0, "trim": 0}

            def copy_file(src, dst):
                Path(dst).parent.mkdir(parents=True, exist_ok=True)
                Path(dst).write_bytes(Path(src).read_bytes())

            deps = {
                "fetch_episodes": lambda info: [episode()],
                "download": lambda url, dest: calls.__setitem__("download", calls["download"] + 1),
                "render_trimmed": lambda info, source, dest: calls.__setitem__("trim", calls["trim"] + 1),
                "copy_file": copy_file,
                "prune": lambda category_dir, podcast_name, keep: None,
                "original_path": lambda info, ep: root / "raw.mp3",
                "trimmed_path": lambda info, ep: root / "trimmed.mp3",
                "sliced_path": lambda info, ep: sliced,
                "drive_path": lambda info, ep: drive / "BioHack" / "Huberman Lab - Episode.mp3",
                "safe_title": lambda title: title,
            }
            run_sync(str(drive), feed(), ledger, deps)
            self.assertEqual(calls["download"], 0)
            self.assertEqual(calls["trim"], 0)
            self.assertEqual((drive / "BioHack" / "Huberman Lab - Episode.mp3").read_bytes(), b"sliced-audio")
            self.assertTrue(ledger.get(10, 7)["copied_sliced"])

    def test_sync_downloads_and_trims_when_the_episode_is_new(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            drive = root / "drive"
            ledger = EpisodeLedger(root / "ledger.json")
            calls = {"download": 0, "trim": 0}

            def download(url, dest):
                calls["download"] += 1
                Path(dest).parent.mkdir(parents=True, exist_ok=True)
                Path(dest).write_bytes(b"raw")

            def render_trimmed(info, source, dest):
                calls["trim"] += 1
                Path(dest).parent.mkdir(parents=True, exist_ok=True)
                Path(dest).write_bytes(b"trimmed")

            def copy_file(src, dst):
                Path(dst).parent.mkdir(parents=True, exist_ok=True)
                Path(dst).write_bytes(Path(src).read_bytes())

            deps = {
                "fetch_episodes": lambda info: [episode()],
                "download": download,
                "render_trimmed": render_trimmed,
                "copy_file": copy_file,
                "prune": lambda category_dir, podcast_name, keep: None,
                "original_path": lambda info, ep: root / "raw.mp3",
                "trimmed_path": lambda info, ep: root / "trimmed.mp3",
                "sliced_path": lambda info, ep: root / "sliced.mp3",
                "drive_path": lambda info, ep: drive / "BioHack" / "Huberman Lab - Episode.mp3",
                "safe_title": lambda title: title,
            }
            run_sync(str(drive), feed(), ledger, deps)
            self.assertEqual(calls["download"], 1)
            self.assertEqual(calls["trim"], 1)
            self.assertEqual((drive / "BioHack" / "Huberman Lab - Episode.mp3").read_bytes(), b"trimmed")
            stored = ledger.get(10, 7)
            self.assertTrue(stored["downloaded"])
            self.assertFalse(stored["sliced"])

    def test_needs_copy_flag_tracks_unsynced_slices(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = EpisodeLedger(root / "ledger.json")
            flag = root / "needs_copy"
            ledger.update(10, 7, sliced=True, sliced_path=str(root / "sliced.mp3"), copied_sliced=False)
            refresh_needs_copy(ledger, flag)
            self.assertTrue(flag.exists())
            ledger.update(10, 7, copied_sliced=True)
            refresh_needs_copy(ledger, flag)
            self.assertFalse(flag.exists())


if __name__ == "__main__":
    unittest.main()
