import tempfile
import unittest
from pathlib import Path

from library import (
    LibraryError,
    StaleLibrary,
    add_folder,
    add_show,
    delete_folder,
    effective_cut_ads,
    load_library,
    move_folder,
    remove_show,
    rename_folder,
    save_library,
    set_folder_cut_ads,
    update_show,
)


def sample():
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
            },
            {
                "name": "Heldeep Radio",
                "feed_id": 2,
                "folder": "Music",
                "skip_first": 45,
                "skip_last": 0,
                "playback_speed": 1.0,
                "num_episodes": 1,
                "cut_ads": None,
            },
        ],
    }


class LibraryTest(unittest.TestCase):
    def test_effective_cut_ads_inherits_the_folder_and_a_show_can_pin_it(self):
        library = sample()
        shows = {show["feed_id"]: show for show in library["shows"]}
        self.assertTrue(effective_cut_ads(library, shows[1]))
        self.assertFalse(effective_cut_ads(library, shows[2]))
        shows[2]["cut_ads"] = True
        self.assertTrue(effective_cut_ads(library, shows[2]))
        set_folder_cut_ads(library, "Music", True)
        self.assertTrue(effective_cut_ads(library, shows[1]))
        self.assertTrue(effective_cut_ads(library, shows[2]))

    def test_changing_the_folder_leaves_a_pinned_show_alone(self):
        library = sample()
        update_show(library, 1, cut_ads=False)
        set_folder_cut_ads(library, "News", False)
        pinned = next(show for show in library["shows"] if show["feed_id"] == 1)
        self.assertIs(pinned["cut_ads"], False)
        self.assertFalse(effective_cut_ads(library, pinned))

    def test_move_folder_reorders_and_clamps_the_index(self):
        library = sample()
        move_folder(library, "Music", 0)
        self.assertEqual([folder["name"] for folder in library["folders"]], ["Music", "News"])
        move_folder(library, "Music", 9)
        self.assertEqual([folder["name"] for folder in library["folders"]], ["News", "Music"])

    def test_save_replaces_the_file_atomically_and_refuses_a_stale_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "shows.json"
            version = save_library(path, sample(), None)
            self.assertTrue(path.exists())
            self.assertFalse(path.with_suffix(".json.tmp").exists())
            loaded = load_library(path)
            self.assertEqual(loaded["shows"][0]["name"], "Up First")
            with self.assertRaises(StaleLibrary):
                save_library(path, sample(), "stale")
            self.assertEqual(load_library(path)["shows"][0]["feed_id"], 1)
            again = save_library(path, loaded, version)
            self.assertNotEqual(again, "stale")

    def test_duplicate_feed_and_occupied_folder_are_refused(self):
        library = sample()
        with self.assertRaises(LibraryError):
            add_show(library, name="Other", feed_id=1, folder="News")
        with self.assertRaises(LibraryError):
            delete_folder(library, "News")
        remove_show(library, 2)
        delete_folder(library, "Music")
        self.assertEqual([folder["name"] for folder in library["folders"]], ["News"])

    def test_rename_moves_shows_and_invalid_json_is_left_in_place(self):
        library = sample()
        rename_folder(library, "News", "Briefings")
        self.assertEqual(library["shows"][0]["folder"], "Briefings")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "shows.json"
            path.write_text("{")
            before = path.read_text()
            with self.assertRaises(LibraryError):
                load_library(path)
            self.assertEqual(path.read_text(), before)

    def test_remove_show_drops_only_that_object(self):
        library = sample()
        remove_show(library, 1)
        self.assertEqual([show["feed_id"] for show in library["shows"]], [2])

    def test_new_folder_defaults_cut_ads_on(self):
        library = sample()
        add_folder(library, "Heroic")
        created = library["folders"][-1]
        self.assertEqual(created, {"name": "Heroic", "cut_ads": True})


if __name__ == "__main__":
    unittest.main()
