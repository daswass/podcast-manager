import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from episode_ledger import EpisodeLedger, prune_local_archive, support_dir
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
    version_of,
)
from podcastsToShokz import (
    RUNNING_MARKER_NAME,
    SHOWS_PATH,
    TMP_RUNNING_MARKER,
    index,
    running_marker_is_stale,
    episode_filename,
    safe_episode_title,
)

REPO = Path(__file__).resolve().parent
AUDIO_SUFFIXES = {".mp3", ".m4a", ".m4b", ".aac", ".wav", ".flac", ".ogg"}
_DURATION_CACHE = {}
_DURATION_LOCK = threading.Lock()
_EPISODE_CACHE = {}
_EPISODE_LOCK = threading.Lock()
_DRIVE_FILES_CACHE = {}
_DRIVE_FILES_TTL = 2
_HEADPHONE_LOCK = threading.Lock()


class NewFolder(BaseModel):
    version: str
    name: str


class PatchFolder(BaseModel):
    version: str
    name: str | None = None
    cut_ads: bool | None = None
    index: int | None = None


class NewShow(BaseModel):
    version: str
    name: str
    feed_id: int
    folder: str
    skip_first: int = 0
    skip_last: int = 0
    playback_speed: float = 1.0
    num_episodes: int = 1
    cut_ads: bool | None = None


class PatchShow(BaseModel):
    version: str
    name: str | None = None
    folder: str | None = None
    skip_first: int | None = None
    skip_last: int | None = None
    playback_speed: float | None = None
    num_episodes: int | None = None
    cut_ads: bool | None = None


class ResolveQuery(BaseModel):
    query: str


class DeleteHeadphoneFile(BaseModel):
    volume: str
    path: str


class PrepareJobs:
    def __init__(self):
        self.feed_id = None
        self.process = None
        self.log_path = None
        self.errors = {}

    def running_feed(self):
        self._reap()
        if self.process is None:
            return None
        return self.feed_id

    def begin(self, feed_id, process, log_path):
        self.errors.pop(int(feed_id), None)
        self.feed_id = int(feed_id)
        self.process = process
        self.log_path = log_path

    def error_for(self, feed_id):
        self._reap()
        return self.errors.get(int(feed_id))

    def _reap(self):
        if self.process is None:
            return
        code = self.process.poll()
        if code is None:
            return
        text = ""
        if self.log_path is not None and Path(self.log_path).exists():
            text = Path(self.log_path).read_text(errors="replace")
        message = _prepare_error(text, code)
        if message:
            self.errors[self.feed_id] = message
        else:
            self.errors.pop(self.feed_id, None)
        self.process = None
        self.feed_id = None
        self.log_path = None


def create_app(
    shows_path=None,
    drive_roots=None,
    marker_paths=None,
    start_prepare=None,
    ledger_path=None,
    archive_root=None,
    static_dir=None,
    serve_ui=True,
    load_episodes=None,
    load_feed=None,
):
    shows_path = Path(shows_path or SHOWS_PATH)
    if drive_roots is None:
        drive_roots = [Path("/Volumes/SWIMBUDS"), Path("/Volumes/OpenSwim")]
    drive_roots = [Path(path) for path in drive_roots]
    if marker_paths is None:
        marker_paths = [
            support_dir() / RUNNING_MARKER_NAME,
            Path(TMP_RUNNING_MARKER),
        ]
    marker_paths = [Path(path) for path in marker_paths]
    ledger_path = Path(ledger_path or (support_dir() / "ledger.json"))
    archive_root = Path(archive_root) if archive_root is not None else ledger_path.parent
    web_root = None
    if serve_ui:
        web_root = Path(static_dir) if static_dir is not None else REPO / "web" / "dist"
    if start_prepare is None:
        start_prepare = _start_prepare_process
    if load_episodes is None:
        load_episodes = _load_feed_episodes
    if load_feed is None:
        load_feed = _load_show_meta
    meta_cache = {}
    meta_lock = threading.Lock()

    jobs = PrepareJobs()
    app = FastAPI()

    def view():
        library = load_library(shows_path)
        running = jobs.running_feed()
        reason = _prepare_block_reason(jobs, marker_paths)
        episodes = _ledger_episodes(ledger_path)
        shows = []
        feed_ids = [int(show["feed_id"]) for show in library["shows"]]
        metas = _show_metas(feed_ids, load_feed, meta_cache, meta_lock)
        _warm_episode_cache(feed_ids, load_episodes)
        headphone = _active_headphone_root(drive_roots)
        headphone_files = _headphone_filenames(headphone)
        for show in library["shows"]:
            feed_id = int(show["feed_id"])
            if running == feed_id:
                status = "preparing"
                error = None
            else:
                status = _ledger_status(episodes, feed_id)
                error = jobs.error_for(feed_id)
            latest_file = _latest_episode_filename(show["name"], feed_id, episodes, load_episodes)
            if headphone is None or latest_file is None:
                on_headphones = None
            else:
                on_headphones = latest_file in headphone_files
            shows.append({
                "name": show["name"],
                "feed_id": feed_id,
                "folder": show["folder"],
                "skip_first": show["skip_first"],
                "skip_last": show["skip_last"],
                "playback_speed": show["playback_speed"],
                "num_episodes": show["num_episodes"],
                "cut_ads": show["cut_ads"],
                "effective_cut_ads": effective_cut_ads(library, show),
                "cut_ads_inherited": show.get("cut_ads") is None,
                "status": status,
                "error": error,
                "image": metas[feed_id]["image"],
                "url": metas[feed_id]["url"],
                "posted": _latest_posted(feed_id, episodes, load_episodes),
                "on_headphones": on_headphones,
            })
        return {
            "version": version_of(shows_path.read_bytes()),
            "folders": library["folders"],
            "shows": shows,
            "prepare_busy": reason is not None,
            "prepare_reason": reason,
            "headphone_volume": headphone.name if headphone is not None else None,
            "job": _job_flags(jobs, marker_paths, drive_roots),
        }

    def library_response():
        try:
            return view()
        except LibraryError as error:
            return JSONResponse({"error": str(error)}, status_code=422)

    def change(version, mutate):
        try:
            library = load_library(shows_path)
        except LibraryError as error:
            return JSONResponse({"error": str(error)}, status_code=422)
        try:
            mutate(library)
            save_library(shows_path, library, version)
        except StaleLibrary as error:
            return JSONResponse({"error": str(error)}, status_code=409)
        except LibraryError as error:
            return JSONResponse({"error": str(error)}, status_code=400)
        return library_response()

    @app.get("/api/library")
    def get_library():
        return library_response()

    @app.post("/api/folders")
    def post_folder(body: NewFolder):
        def mutate(library):
            add_folder(library, body.name, cut_ads=True)
        return change(body.version, mutate)

    @app.patch("/api/folders/{name:path}")
    def patch_folder(name: str, body: PatchFolder):
        fields = body.model_dump(exclude_unset=True)

        def mutate(library):
            current = name
            if "cut_ads" in fields:
                set_folder_cut_ads(library, current, fields["cut_ads"])
            if "name" in fields:
                rename_folder(library, current, fields["name"])
                current = str(fields["name"]).strip()
            if "index" in fields:
                move_folder(library, current, fields["index"])

        return change(body.version, mutate)

    @app.delete("/api/folders/{name:path}")
    def remove_folder(name: str, version: str):
        def mutate(library):
            delete_folder(library, name)
        return change(version, mutate)

    @app.post("/api/shows")
    def post_show(body: NewShow):
        fields = body.model_dump(exclude_unset=True)

        def mutate(library):
            add_show(
                library,
                body.name,
                body.feed_id,
                body.folder,
                skip_first=body.skip_first,
                skip_last=body.skip_last,
                playback_speed=body.playback_speed,
                num_episodes=body.num_episodes,
                cut_ads=fields.get("cut_ads"),
            )
        return change(body.version, mutate)

    @app.patch("/api/shows/{feed_id}")
    def patch_show(feed_id: int, body: PatchShow):
        fields = body.model_dump(exclude_unset=True)
        fields.pop("version", None)

        def mutate(library):
            update_show(library, feed_id, **fields)

        return change(body.version, mutate)

    @app.delete("/api/shows/{feed_id}")
    def delete_show(feed_id: int, version: str):
        def mutate(library):
            remove_show(library, feed_id)
        return change(version, mutate)

    @app.get("/api/search")
    def search(q: str = ""):
        query = q.strip()
        if not query:
            return {"results": []}
        try:
            result = index.search(query, clean=True)
        except Exception as error:
            return JSONResponse({"error": f"Podcast Index search failed: {error}"}, status_code=502)
        results = []
        for feed in (result or {}).get("feeds") or []:
            if feed.get("id") is None:
                continue
            results.append({
                "title": feed.get("title") or "",
                "author": feed.get("author") or "",
                "feed_id": int(feed["id"]),
                "url": feed.get("url") or "",
            })
            if len(results) == 12:
                break
        return {"results": results}

    @app.post("/api/resolve")
    def resolve(body: ResolveQuery):
        query = body.query.strip()
        if not query:
            return JSONResponse({"error": "Enter a feed URL or a Podcast Index id"}, status_code=400)
        try:
            if query.isdigit():
                result = index.podcastByFeedId(query)
            else:
                result = index.podcastByFeedUrl(query)
        except Exception as error:
            return JSONResponse({"error": f"Podcast Index lookup failed: {error}"}, status_code=502)
        feed = _one_feed(result)
        if feed is None:
            return JSONResponse({"error": "Podcast Index did not return a feed"}, status_code=404)
        return feed

    @app.get("/api/headphones")
    def headphones():
        try:
            library = load_library(shows_path)
        except LibraryError:
            library = {"folders": [], "shows": []}
        shows = library.get("shows") or []
        show_names = [show["name"] for show in shows]
        shows_by_name = {show["name"]: int(show["feed_id"]) for show in shows}
        folder_order = [folder["name"] for folder in library.get("folders") or []]
        volumes = []
        with _HEADPHONE_LOCK:
            for root in drive_roots:
                if not root.is_dir():
                    continue
                volumes.append(
                    _volume_listing(root, show_names, shows_by_name, ledger_path, load_episodes, folder_order)
                )
        return {"volumes": volumes}

    @app.delete("/api/headphones")
    def delete_headphone(body: DeleteHeadphoneFile):
        reason = _live_marker_reason(marker_paths)
        if reason:
            return JSONResponse({"error": reason}, status_code=409)
        root = _volume_root(drive_roots, body.volume)
        if root is None or not root.is_dir():
            return JSONResponse({"error": f"{body.volume} is not connected"}, status_code=404)
        try:
            target = _file_on_volume(root, body.path)
        except ValueError as error:
            return JSONResponse({"error": str(error)}, status_code=400)
        if not target.exists():
            return JSONResponse({"error": "That file is already gone"}, status_code=404)
        if not target.is_file():
            return JSONResponse({"error": "That path is not a file"}, status_code=400)
        try:
            with _HEADPHONE_LOCK:
                _commit_unlink(target)
                _DRIVE_FILES_CACHE.pop(str(root), None)
        except OSError as error:
            detail = error.strerror or str(error)
            return JSONResponse({"error": f"Could not remove that file from {body.volume}: {detail}"}, status_code=409)
        return {"ok": True}

    @app.post("/api/shows/{feed_id}/prepare")
    def prepare_show(feed_id: int):
        try:
            library = load_library(shows_path)
        except LibraryError as error:
            return JSONResponse({"error": str(error)}, status_code=422)
        if not any(int(show["feed_id"]) == int(feed_id) for show in library["shows"]):
            return JSONResponse({"error": f"Unknown show {feed_id}"}, status_code=404)
        reason = _prepare_block_reason(jobs, marker_paths)
        if reason:
            return JSONResponse({"error": reason}, status_code=409)
        process, log_path = start_prepare(int(feed_id))
        jobs.begin(feed_id, process, log_path)
        return library_response()

    @app.post("/api/shows/{feed_id}/cut-ads")
    def cut_show_ads(feed_id: int):
        try:
            library = load_library(shows_path)
        except LibraryError as error:
            return JSONResponse({"error": str(error)}, status_code=422)
        if not any(int(show["feed_id"]) == int(feed_id) for show in library["shows"]):
            return JSONResponse({"error": f"Unknown show {feed_id}"}, status_code=404)
        reason = _prepare_block_reason(jobs, marker_paths)
        if reason:
            return JSONResponse({"error": reason}, status_code=409)
        process, log_path = start_prepare(int(feed_id), force_slice=True)
        jobs.begin(feed_id, process, log_path)
        return library_response()

    @app.get("/api/archive")
    def get_archive():
        try:
            library = load_library(shows_path)
        except LibraryError as error:
            return JSONResponse({"error": str(error)}, status_code=422)
        names = [show["name"] for show in library["shows"]]
        prune_local_archive(EpisodeLedger(ledger_path), archive_root)
        return {"episodes": _archive_episodes(archive_root, names)}

    @app.get("/api/archive/audio")
    async def archive_audio(path: str):
        try:
            target = _archive_file(archive_root, path)
        except ValueError as error:
            return JSONResponse({"error": str(error)}, status_code=400)
        if not target.is_file():
            return JSONResponse({"error": "That episode is no longer on this Mac"}, status_code=404)
        return FileResponse(target, media_type="audio/mpeg")

    if web_root is not None and web_root.is_dir():
        app.mount("/", StaticFiles(directory=str(web_root), html=True), name="web")
    return app


def _start_prepare_process(feed_id, force_slice=False):
    log_path = Path(f"/tmp/podcast-prepare-{feed_id}.log")
    command = [sys.executable, str(REPO / "podcastsToShokz.py"), "--prepare-show", str(feed_id)]
    if force_slice:
        command.append("--cut-ads")
    with log_path.open("w") as handle:
        process = subprocess.Popen(
            command,
            cwd=str(REPO),
            stdout=handle,
            stderr=subprocess.STDOUT,
        )
    return process, log_path


def _prepare_error(text, code):
    lines = [line.strip() for line in text.splitlines() if "ERROR:" in line]
    if code != 0:
        if lines:
            return " ".join(lines)
        cleaned = text.strip()
        if cleaned:
            return cleaned[-400:]
        return f"Prepare exited with status {code}"
    if lines:
        return " ".join(lines)
    return None


def _prepare_block_reason(jobs, marker_paths):
    marker = _live_marker_reason(marker_paths)
    if marker:
        return marker
    running = jobs.running_feed()
    if running is not None:
        return "Another prepare is already running"
    return None


def _marker_live(path):
    path = Path(path)
    try:
        exists = path.exists()
    except OSError:
        return False
    if not exists:
        return False
    return not running_marker_is_stale(str(path))


def _marker_pid(path):
    try:
        for line in Path(path).read_text().splitlines():
            if line.startswith("pid="):
                return int(line.split("=", 1)[1])
    except (OSError, ValueError):
        return None
    return None


def _marker_command(path):
    pid = _marker_pid(path)
    if pid is None:
        return ""
    try:
        return subprocess.check_output(["ps", "-p", str(pid), "-o", "command="], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def _marker_job(path, drive_roots):
    path = Path(path)
    drive_markers = {str(Path(root) / RUNNING_MARKER_NAME) for root in drive_roots}
    if str(path) in drive_markers:
        return "copying"
    if path == Path(TMP_RUNNING_MARKER):
        command = _marker_command(path)
        if "--prepare" in command:
            return "downloading"
        if "podcastsToShokz.py" in command:
            return "copying"
        return "running"
    return "downloading"


def _job_flags(jobs, prepare_markers, drive_roots):
    downloading = jobs.running_feed() is not None
    copying = False
    ambiguous = False
    seen = set()
    live = []
    markers = [Path(path) for path in prepare_markers]
    markers.extend(Path(root) / RUNNING_MARKER_NAME for root in drive_roots)
    drive_markers = {str(Path(root) / RUNNING_MARKER_NAME) for root in drive_roots}
    for path in markers:
        key = str(path)
        if key in seen or not _marker_live(path):
            continue
        seen.add(key)
        live.append(path)
    copy_pids = {
        _marker_pid(path)
        for path in live
        if str(path) in drive_markers
    }
    copy_pids.discard(None)
    for path in live:
        if str(path) in drive_markers:
            copying = True
            continue
        if path == Path(TMP_RUNNING_MARKER):
            kind = _marker_job(path, drive_roots)
            if kind == "downloading":
                downloading = True
            elif kind == "copying":
                copying = True
            else:
                ambiguous = True
            continue
        pid = _marker_pid(path)
        if pid is not None and pid in copy_pids:
            continue
        downloading = True
    if downloading and copying:
        return "both"
    if downloading:
        return "downloading"
    if copying:
        return "copying"
    if ambiguous:
        return "running"
    return None


def _live_marker_reason(marker_paths):
    for path in marker_paths:
        if path.exists() and not running_marker_is_stale(str(path)):
            return "Nightly prepare is already running"
    return None


def _ledger_episodes(path):
    try:
        return EpisodeLedger(path).episodes
    except (OSError, json.JSONDecodeError):
        return {}


def _ledger_status(episodes, feed_id):
    matches = []
    prefix = f"{int(feed_id)}:"
    for key, entry in episodes.items():
        same_key = str(key).startswith(prefix)
        try:
            same_feed = int(entry.get("feed_id")) == int(feed_id)
        except (TypeError, ValueError):
            same_feed = False
        if same_key or same_feed:
            matches.append(entry)
    if not matches:
        return "not_prepared"
    latest = max(matches, key=_published)
    if latest.get("sliced"):
        return "sliced"
    if latest.get("trimmed_path"):
        return "trimmed"
    return "not_prepared"


def _published(entry):
    try:
        return float(entry.get("published") or 0)
    except (TypeError, ValueError):
        return 0


def _one_feed(result):
    feed = (result or {}).get("feed")
    if isinstance(feed, list):
        feed = feed[0] if feed else None
    if not isinstance(feed, dict) or feed.get("id") is None:
        return None
    return {
        "name": feed.get("title") or "",
        "feed_id": int(feed["id"]),
        "author": feed.get("author") or "",
        "url": feed.get("url") or "",
    }


def _library_shows(shows_path):
    try:
        library = load_library(shows_path)
    except LibraryError:
        return []
    return library["shows"]


def _volume_listing(root, show_names, shows_by_name, ledger_path, load_episodes, folder_order):
    folders = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.name.startswith("._"):
            continue
        if path.suffix.lower() not in AUDIO_SUFFIXES:
            continue
        relative = path.relative_to(root).as_posix()
        folder_name = path.parent.relative_to(root).as_posix()
        if folder_name == ".":
            folder_name = ""
        title, show, safe_title = _readable_drive_file(path.name, show_names)
        stat = path.stat()
        folders.setdefault(folder_name, []).append({
            "name": title,
            "show": show,
            "filename": path.name,
            "path": relative,
            "mtime": int(stat.st_mtime),
            "posted": None,
            "_duration_path": path,
            "_duration_stat": stat,
            "_safe_title": safe_title,
        })
    _attach_durations(folders)
    _attach_posted(folders, shows_by_name, ledger_path, load_episodes)
    grouped = []
    for name in _folders_in_library_order(folders, folder_order):
        files = []
        for item in folders[name]:
            item.pop("_duration_path", None)
            item.pop("_duration_stat", None)
            item.pop("_safe_title", None)
            files.append(item)
        files.sort(key=_newest_episode, reverse=True)
        grouped.append({"name": name or "Headphones", "files": files})
    return {"name": root.name, "folders": grouped}


def _newest_episode(item):
    mtime = int(item.get("mtime") or 0)
    posted = item.get("posted")
    if isinstance(posted, (int, float)) and posted > 0:
        return (1, int(posted), mtime)
    return (0, 0, mtime)


def _folders_in_library_order(folders, folder_order):
    order = list(folder_order or [])

    def sort_key(name):
        label = name or "Headphones"
        try:
            return (0, order.index(label))
        except ValueError:
            return (1, label.casefold())

    return sorted(folders, key=sort_key)


def _attach_durations(folders):
    pending = []
    for items in folders.values():
        for item in items:
            path = item["_duration_path"]
            stat = item["_duration_stat"]
            key = (str(path), stat.st_mtime_ns, stat.st_size)
            with _DURATION_LOCK:
                cached = _DURATION_CACHE[key] if key in _DURATION_CACHE else None
                present = key in _DURATION_CACHE
            if present:
                item["duration"] = cached
            else:
                pending.append((item, path, key))
    if not pending:
        return
    with ThreadPoolExecutor(max_workers=4) as pool:
        probed = pool.map(lambda job: _probe_duration(job[1]), pending)
    for (item, _path, key), seconds in zip(pending, probed):
        item["duration"] = seconds
        if seconds is not None:
            with _DURATION_LOCK:
                _DURATION_CACHE[key] = seconds
                if len(_DURATION_CACHE) > 400:
                    _DURATION_CACHE.clear()
                    _DURATION_CACHE[key] = seconds


def _probe_duration(path):
    program = shutil.which("ffprobe")
    if program is None:
        for candidate in ("/opt/homebrew/bin/ffprobe", "/usr/local/bin/ffprobe"):
            if Path(candidate).is_file():
                program = candidate
                break
    if program is None:
        return None
    try:
        result = subprocess.run(
            [program, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    try:
        duration = float(json.loads(result.stdout)["format"]["duration"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None
    if duration <= 0:
        return None
    return int(round(duration))


def _attach_posted(folders, shows_by_name, ledger_path, load_episodes):
    by_filename, by_title = _ledger_posted(ledger_path)
    pending = []
    for items in folders.values():
        for item in items:
            filename = item["filename"]
            show = item["show"]
            safe_title = item.get("_safe_title")
            posted = by_filename.get(filename)
            if posted is None and show and safe_title:
                posted = by_title.get((show, safe_title))
            if posted is not None:
                item["posted"] = posted
                continue
            feed_id = shows_by_name.get(show) if show else None
            if feed_id and safe_title:
                pending.append((item, feed_id, safe_title))
    for item, feed_id, safe_title in pending:
        episodes = _episodes_for_feed(feed_id, load_episodes)
        item["posted"] = episodes.get(safe_title)


def _ledger_posted(ledger_path):
    by_filename = {}
    by_title = {}
    for entry in _ledger_episodes(ledger_path).values():
        published = _posted_stamp(entry.get("published"))
        if published is None:
            continue
        show = entry.get("podcast_name") or ""
        title = entry.get("title") or ""
        if show and title:
            _keep_latest(by_title, (show, safe_episode_title(title)), published)
        for key in ("sliced_path", "trimmed_path"):
            path = entry.get(key)
            if path:
                _keep_latest(by_filename, Path(path).name, published)
    return by_filename, by_title


def _keep_latest(mapping, key, published):
    current = mapping.get(key)
    if current is None or published > current:
        mapping[key] = published


def _posted_stamp(value):
    try:
        stamp = int(float(value))
    except (TypeError, ValueError):
        return None
    if stamp <= 0:
        return None
    return stamp


def _warm_episode_cache(feed_ids, load_episodes):
    missing = []
    now = time.monotonic()
    with _EPISODE_LOCK:
        for feed_id in dict.fromkeys(int(feed_id) for feed_id in feed_ids):
            cached = _EPISODE_CACHE.get((id(load_episodes), feed_id))
            if not cached or now - cached[0] >= 600:
                missing.append(feed_id)
    if not missing:
        return
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda feed_id: _episodes_for_feed(feed_id, load_episodes), missing))


def _active_headphone_root(drive_roots):
    mounted = {}
    for path in drive_roots:
        try:
            present = path.is_dir()
        except OSError:
            present = False
        if present:
            mounted[path.name] = path
    for name in ("OpenSwim", "SWIMBUDS"):
        if name in mounted:
            return mounted[name]
    return None


def _headphone_filenames(root):
    if root is None:
        return set()
    key = str(root)
    now = time.monotonic()
    cached = _DRIVE_FILES_CACHE.get(key)
    if cached and now - cached[0] < _DRIVE_FILES_TTL:
        return cached[1]
    names = _read_headphone_filenames(root)
    _DRIVE_FILES_CACHE[key] = (now, names)
    return names


def _read_headphone_filenames(root):
    names = set()
    try:
        entries = list(root.iterdir())
    except OSError:
        return names
    for entry in entries:
        if entry.name.startswith(".") or entry.name == "System Volume Information":
            continue
        try:
            if entry.is_file():
                _keep_audio_name(names, entry)
                continue
            if not entry.is_dir():
                continue
            children = list(entry.iterdir())
        except OSError:
            continue
        for child in children:
            _keep_audio_name(names, child)
    return names


def _keep_audio_name(names, path):
    try:
        is_file = path.is_file()
    except OSError:
        return
    if not is_file or path.name.startswith("._"):
        return
    if path.suffix.lower() not in AUDIO_SUFFIXES:
        return
    names.add(path.name)


def _latest_episode_filename(show_name, feed_id, ledger_episodes, load_episodes):
    best_stamp = None
    best_title = None

    def consider(stamp, title):
        nonlocal best_stamp, best_title
        if stamp is None or not title:
            return
        if best_stamp is None or stamp > best_stamp:
            best_stamp = stamp
            best_title = title

    for title, stamp in _episodes_for_feed(feed_id, load_episodes).items():
        consider(_posted_stamp(stamp), title)
    prefix = f"{int(feed_id)}:"
    for key, entry in ledger_episodes.items():
        same_key = str(key).startswith(prefix)
        try:
            same_feed = int(entry.get("feed_id")) == int(feed_id)
        except (TypeError, ValueError):
            same_feed = False
        if not same_key and not same_feed:
            continue
        consider(_posted_stamp(entry.get("published")), entry.get("title"))
    if best_title is None:
        return None
    return episode_filename(show_name, {"title": best_title})


def _latest_posted(feed_id, ledger_episodes, load_episodes):
    stamps = []
    prefix = f"{int(feed_id)}:"
    for key, entry in ledger_episodes.items():
        same_key = str(key).startswith(prefix)
        try:
            same_feed = int(entry.get("feed_id")) == int(feed_id)
        except (TypeError, ValueError):
            same_feed = False
        if not same_key and not same_feed:
            continue
        stamp = _posted_stamp(entry.get("published"))
        if stamp is not None:
            stamps.append(stamp)
    for stamp in _episodes_for_feed(feed_id, load_episodes).values():
        parsed = _posted_stamp(stamp)
        if parsed is not None:
            stamps.append(parsed)
    return max(stamps) if stamps else None


def _episodes_for_feed(feed_id, load_episodes):
    feed_id = int(feed_id)
    now = time.monotonic()
    cache_key = (id(load_episodes), feed_id)
    with _EPISODE_LOCK:
        cached = _EPISODE_CACHE.get(cache_key)
    if cached and now - cached[0] < 600:
        return cached[1]
    try:
        loaded = load_episodes(feed_id) or {}
    except Exception:
        loaded = {}
    if not isinstance(loaded, dict):
        loaded = {}
    with _EPISODE_LOCK:
        _EPISODE_CACHE[cache_key] = (now, loaded)
    return loaded


def _load_show_meta(feed_id):
    result = index.podcastByFeedId(int(feed_id))
    feed = (result or {}).get("feed")
    if isinstance(feed, list):
        feed = feed[0] if feed else None
    if not isinstance(feed, dict):
        return {}
    return {
        "image": feed.get("artwork") or feed.get("image") or "",
        "url": feed.get("link") or "",
    }


def _show_metas(feed_ids, load_feed, cache, lock):
    unique = list(dict.fromkeys(int(feed_id) for feed_id in feed_ids))
    found = {}
    pending = []
    now = time.monotonic()
    with lock:
        for feed_id in unique:
            cached = cache.get(feed_id)
            if cached and now < cached[0]:
                found[feed_id] = cached[1]
            else:
                pending.append(feed_id)
    if pending:
        with ThreadPoolExecutor(max_workers=4) as pool:
            loaded = pool.map(lambda feed_id: _fetch_show_meta(feed_id, load_feed, cache, lock), pending)
        for feed_id, meta in loaded:
            found[feed_id] = meta
    return found


def _fetch_show_meta(feed_id, load_feed, cache, lock):
    try:
        raw = load_feed(feed_id) or {}
        failed = False
    except Exception:
        raw = {}
        failed = True
    if not isinstance(raw, dict):
        raw = {}
        failed = True
    meta = {"image": _http_url(raw.get("image")), "url": _http_url(raw.get("url"))}
    expiry = time.monotonic() + (60 if failed else 3600)
    with lock:
        cached = cache.get(feed_id)
        if failed and cached and time.monotonic() < cached[0]:
            return feed_id, cached[1]
        cache[feed_id] = (expiry, meta)
    return feed_id, meta


def _http_url(value):
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if cleaned.startswith("https://") or cleaned.startswith("http://"):
        return cleaned
    return None


def _load_feed_episodes(feed_id):
    result = index.episodesByFeedId(int(feed_id), max_results=100)
    posted = {}
    for episode in (result or {}).get("items") or []:
        title = episode.get("title") or ""
        stamp = _posted_stamp(episode.get("datePublished"))
        if title and stamp is not None:
            _keep_latest(posted, safe_episode_title(title), stamp)
    return posted


def _readable_drive_file(filename, show_names):
    stem = _audio_stem(filename)
    matched = None
    remainder = stem
    for name in sorted({str(name) for name in show_names if name}, key=len, reverse=True):
        prefix = f"{name} - "
        if stem.startswith(prefix):
            matched = name
            remainder = stem[len(prefix):]
            break
    title = _humanize_title(remainder if matched else stem)
    return title or stem, matched, remainder if matched else None


def _audio_stem(filename):
    lower = filename.lower()
    for suffix in AUDIO_SUFFIXES:
        if lower.endswith(suffix):
            return filename[: -len(suffix)]
    return filename


def _humanize_title(text):
    text = text.replace("_", " ")
    text = re.sub(r" {2,}", " ", text)
    return text.strip(" .")


def _volume_root(drive_roots, volume):
    for root in drive_roots:
        if root.name == volume or str(root) == volume:
            return root
    return None


def _archive_episodes(root, show_names):
    episodes = []
    root = Path(root)
    for kind in ("sliced", "trimmed"):
        base = root / kind
        if not base.is_dir():
            continue
        try:
            folders = list(base.iterdir())
        except OSError:
            continue
        for folder in folders:
            if not folder.is_dir() or folder.name.startswith("."):
                continue
            try:
                files = list(folder.iterdir())
            except OSError:
                continue
            for path in files:
                if not path.is_file() or path.name.startswith("._") or path.suffix.lower() != ".mp3":
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                title, show, _remainder = _readable_drive_file(path.name, show_names)
                episodes.append({
                    "folder": folder.name,
                    "name": title,
                    "show": show,
                    "filename": path.name,
                    "path": f"{kind}/{folder.name}/{path.name}",
                    "mtime": stat.st_mtime,
                    "kind": kind,
                    "_duration_path": path,
                    "_duration_stat": stat,
                })
    episodes.sort(key=lambda item: item["mtime"], reverse=True)
    _attach_durations({"": episodes})
    for episode in episodes:
        episode.pop("_duration_path", None)
        episode.pop("_duration_stat", None)
    return episodes


def _archive_file(root, relative):
    if not relative or Path(relative).is_absolute():
        raise ValueError("Choose an episode on this Mac")
    parts = Path(relative).parts
    if len(parts) != 3 or parts[0] not in {"sliced", "trimmed"}:
        raise ValueError("That episode is outside the local archive")
    if any(part in {"", ".", ".."} or part.startswith(".") for part in parts):
        raise ValueError("That episode is outside the local archive")
    root = Path(root).resolve()
    target = (root / relative).resolve()
    if not target.is_relative_to(root) or target.suffix.lower() != ".mp3" or _on_headphones(target):
        raise ValueError("That episode is outside the local archive")
    return target


def _on_headphones(path):
    text = Path(path).as_posix()
    return text == "/Volumes/SWIMBUDS" or text.startswith("/Volumes/SWIMBUDS/") or text == "/Volumes/OpenSwim" or text.startswith("/Volumes/OpenSwim/")


def _commit_unlink(path):
    parent = path.parent
    path.unlink()
    sidecar = parent / f"._{path.name}"
    try:
        if sidecar.is_file():
            sidecar.unlink()
    except OSError:
        pass
    try:
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        directory = os.open(parent, flags)
    except OSError:
        return
    try:
        if hasattr(fcntl, "F_FULLFSYNC"):
            fcntl.fcntl(directory, fcntl.F_FULLFSYNC)
        else:
            os.fsync(directory)
    except OSError:
        pass
    finally:
        os.close(directory)


def _file_on_volume(root, relative):
    if not relative or Path(relative).is_absolute():
        raise ValueError("Choose a file on the headphones")
    root = root.resolve()
    target = (root / relative).resolve()
    if not target.is_relative_to(root) or target == root:
        raise ValueError("That file is outside the headphone drive")
    if target.name.startswith("._") or target.name == RUNNING_MARKER_NAME:
        raise ValueError("That file stays on the drive")
    return target


app = create_app()
