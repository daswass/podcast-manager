import hashlib
import json
from pathlib import Path


class LibraryError(Exception):
    pass


class StaleLibrary(LibraryError):
    pass


def feed_settings(show, library, url):
    info = {
        "id": int(show["feed_id"]),
        "url": url,
        "category": show["folder"],
        "num_episodes": int(show["num_episodes"]),
        "skip_first": int(show["skip_first"]),
        "skip_last": int(show["skip_last"]),
        "playback_speed": float(show["playback_speed"]),
    }
    if not effective_cut_ads(library, show):
        info["slice"] = False
    return info


def effective_cut_ads(library, show):
    if show.get("cut_ads") is not None:
        return bool(show["cut_ads"])
    folder = _folder(library, show["folder"])
    return bool(folder["cut_ads"])


def add_folder(library, name, cut_ads=True):
    _validate_folder_name(library, name)
    library["folders"].append({"name": str(name).strip(), "cut_ads": bool(cut_ads)})
    return library


def rename_folder(library, old_name, new_name):
    _folder(library, old_name)
    cleaned = str(new_name or "").strip()
    if old_name == cleaned:
        return library
    _validate_folder_name(library, cleaned)
    for folder in library["folders"]:
        if folder["name"] == old_name:
            folder["name"] = cleaned
    for show in library["shows"]:
        if show["folder"] == old_name:
            show["folder"] = cleaned
    return library


def delete_folder(library, name):
    _folder(library, name)
    occupants = [show["name"] for show in library["shows"] if show["folder"] == name]
    if occupants:
        raise LibraryError(f"Folder '{name}' still has shows: {', '.join(occupants)}")
    library["folders"] = [folder for folder in library["folders"] if folder["name"] != name]
    return library


def set_folder_cut_ads(library, name, cut_ads):
    folder = _folder(library, name)
    folder["cut_ads"] = bool(cut_ads)
    return library


def move_folder(library, name, index):
    folder = _folder(library, name)
    remaining = [item for item in library["folders"] if item is not folder]
    index = max(0, min(int(index), len(remaining)))
    remaining.insert(index, folder)
    library["folders"] = remaining
    return library


def add_show(library, name, feed_id, folder, skip_first=0, skip_last=0, playback_speed=1.0, num_episodes=1, cut_ads=None):
    show = {
        "name": name,
        "feed_id": int(feed_id),
        "folder": folder,
        "skip_first": skip_first,
        "skip_last": skip_last,
        "playback_speed": playback_speed,
        "num_episodes": num_episodes,
        "cut_ads": cut_ads,
    }
    _validate_show(library, show, replacing=None)
    library["shows"].append(show)
    return library


def update_show(library, feed_id, **fields):
    show = _show(library, feed_id)
    updated = dict(show)
    updated.update(fields)
    _validate_show(library, updated, replacing=show["feed_id"])
    show.clear()
    show.update(updated)
    return library


def remove_show(library, feed_id):
    _show(library, feed_id)
    library["shows"] = [show for show in library["shows"] if show["feed_id"] != int(feed_id)]
    return library


def load_library(path):
    path = Path(path)
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise LibraryError(f"Could not read {path.name}: {error}") from error
    _validate_library(payload)
    return payload


def save_library(path, library, expected_version):
    path = Path(path)
    _validate_library(library)
    if path.exists():
        current = version_of(path.read_bytes())
        if expected_version != current:
            raise StaleLibrary("shows.json changed since it was loaded")
    text = json.dumps(library, indent=2) + "\n"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(text)
    temporary.replace(path)
    return version_of(path.read_bytes())


def version_of(raw):
    return hashlib.sha256(raw).hexdigest()


def _validate_library(library):
    if not isinstance(library, dict):
        raise LibraryError("Library must be an object")
    folders = library.get("folders")
    shows = library.get("shows")
    if not isinstance(folders, list) or not isinstance(shows, list):
        raise LibraryError("Library needs folders and shows lists")
    names = []
    for folder in folders:
        if not isinstance(folder, dict) or not str(folder.get("name") or "").strip():
            raise LibraryError("Every folder needs a name")
        if not isinstance(folder.get("cut_ads"), bool):
            raise LibraryError(f"Folder '{folder.get('name')}' needs cut_ads true or false")
        if folder["name"] in names:
            raise LibraryError(f"Folder '{folder['name']}' already exists")
        names.append(folder["name"])
    feed_ids = []
    for show in shows:
        _validate_show(library, show, replacing=None, seen=feed_ids)
        feed_ids.append(int(show["feed_id"]))


def _validate_show(library, show, replacing, seen=None):
    if not isinstance(show, dict) or not str(show.get("name") or "").strip():
        raise LibraryError("Every show needs a name")
    try:
        feed_id = int(show.get("feed_id"))
    except (TypeError, ValueError):
        raise LibraryError(f"Show '{show.get('name')}' needs a numeric feed id") from None
    if feed_id in (seen or []):
        raise LibraryError(f"Feed {feed_id} is already in the library")
    if replacing != feed_id:
        for existing in library.get("shows") or []:
            if int(existing["feed_id"]) == feed_id and existing is not show and int(existing["feed_id"]) != replacing:
                owner = existing["name"]
                raise LibraryError(f"Feed {feed_id} is already used by {owner}")
    if show.get("folder") not in {folder["name"] for folder in library.get("folders") or []}:
        raise LibraryError(f"Show '{show['name']}' is in an unknown folder '{show.get('folder')}'")
    for key in ("skip_first", "skip_last"):
        value = show.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise LibraryError(f"Show '{show['name']}' has a negative {key}")
    speed = show.get("playback_speed")
    if isinstance(speed, bool) or not isinstance(speed, (int, float)) or speed <= 0:
        raise LibraryError(f"Show '{show['name']}' needs a positive playback speed")
    count = show.get("num_episodes")
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise LibraryError(f"Show '{show['name']}' must keep at least 1 episode")
    if show.get("cut_ads") not in (True, False, None):
        raise LibraryError(f"Show '{show['name']}' cut_ads must be true, false, or null")


def _validate_folder_name(library, name):
    cleaned = str(name or "").strip()
    if not cleaned:
        raise LibraryError("A folder needs a name")
    if any(folder["name"] == cleaned for folder in library["folders"]):
        raise LibraryError(f"Folder '{cleaned}' already exists")


def _folder(library, name):
    for folder in library["folders"]:
        if folder["name"] == name:
            return folder
    raise LibraryError(f"Unknown folder '{name}'")


def _show(library, feed_id):
    for show in library["shows"]:
        if int(show["feed_id"]) == int(feed_id):
            return show
    raise LibraryError(f"Unknown show {feed_id}")
