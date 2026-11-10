import time
from datetime import datetime, timezone
from pathlib import Path


def sync_plan(entry):
    if entry and entry.get("sliced") and entry.get("sliced_path"):
        return "copy_sliced"
    if entry and entry.get("downloaded") and entry.get("original_path"):
        return "trim_and_copy"
    return "download_trim_copy"


def prepare_plan(entry, info=None):
    # An explicit ad cut reprocesses the latest episode even when a sliced
    # file already exists. That file may have been written while the ad model
    # was unavailable.
    if info is not None and info.get("force_slice_latest"):
        if entry and entry.get("downloaded") and entry.get("original_path"):
            return "slice"
        return "download_and_slice"
    # cut_ads false keeps the fixed trim and speed and skips transcription.
    if info is not None and info.get("slice") is False:
        if entry and entry.get("downloaded") and entry.get("original_path"):
            return "skip"
        return "download"
    if entry and entry.get("sliced") and entry.get("sliced_path"):
        return "skip"
    if entry and entry.get("downloaded") and entry.get("original_path"):
        return "slice"
    return "download_and_slice"


def run_prepare(feeds, ledger, deps):
    for podcast_name, info in feeds:
        try:
            episodes = list(deps["fetch_episodes"](info) or [])
        except Exception as error:
            print(f"ERROR: Failed to fetch '{podcast_name}': {error}")
            continue
        count = info.get("force_latest") or info.get("num_episodes", 1)
        episodes = episodes[:count]
        keep_ids = []
        for index, episode in enumerate(episodes):
            episode_id = episode.get("id")
            if episode_id is None:
                print(f"\tSkipping an episode of {podcast_name} with no id")
                continue
            keep_ids.append(episode_id)
            episode_info = info
            if info.get("force_slice_latest"):
                episode_info = dict(info)
                if index == 0:
                    # The newest episode is re-sliced even when the show
                    # normally keeps ads.
                    episode_info.pop("slice", None)
                else:
                    episode_info.pop("force_slice_latest", None)
            try:
                _prepare_one(podcast_name, episode_info, episode, ledger, deps)
            except Exception as error:
                print(f"ERROR: Failed to prepare '{podcast_name}' episode {episode_id}: {error}")
        if episodes:
            ledger.prune_feed(info["id"], keep_ids)


def _published_after(episode, since):
    published = episode.get("datePublished")
    if published is None:
        return True
    try:
        return float(published) > float(since or 0)
    except (TypeError, ValueError):
        return True


def _drive_relative(drive_root, drive_path):
    path = Path(drive_path)
    if not drive_root:
        return path.as_posix()
    try:
        return path.resolve().relative_to(Path(drive_root).resolve()).as_posix()
    except (ValueError, OSError):
        return path.as_posix()


def _leave_on_headphones(episode, info, since, drive_path, replace, relative="", removed=()):
    """Skip a copy the headphones already settled.

    ``podcast_downloader_last_run.txt`` remembers the last finished run, so an
    episode from before that stays off the drive. ``podcast_downloader_removed.txt``
    remembers a file the listener deleted. A file that is already there is left alone.
    """
    if replace:
        return None
    if relative and relative in removed:
        return "removed from headphones"
    if Path(drive_path).is_file():
        return "already on headphones"
    if info.get("force_latest"):
        return None
    if not _published_after(episode, since):
        return "published before the last headphone run"
    return None


def _remember_copy(ledger, info, episode_id):
    entry = ledger.get(info["id"], episode_id)
    if entry and entry.get("sliced") and not entry.get("copied_sliced"):
        ledger.update(info["id"], episode_id, copied_sliced=True)


def run_sync(drive_root, feeds, ledger, deps):
    since = deps.get("last_run", 0) or 0
    checked = True
    for podcast_name, info in feeds:
        try:
            episodes = list(deps["fetch_episodes"](info) or [])
        except Exception as error:
            print(f"ERROR: Failed to fetch '{podcast_name}': {error}")
            checked = False
            continue
        count = info.get("force_latest") or info.get("num_episodes", 1)
        episodes = episodes[:count]
        for index, episode in enumerate(episodes):
            episode_id = episode.get("id")
            if episode_id is None:
                print(f"\tSkipping an episode of {podcast_name} with no id")
                continue
            replace = index == 0 and bool(info.get("force_slice_latest"))
            try:
                _sync_one(podcast_name, info, episode, ledger, deps, since=since, replace=replace)
            except Exception as error:
                print(f"ERROR: Failed to sync '{podcast_name}' episode {episode_id}: {error}")
        if episodes and deps.get("prune"):
            category_dir = str(Path(drive_root) / info["category"])
            deps["prune"](category_dir, podcast_name, count)
    return checked


def _prepare_one(podcast_name, info, episode, ledger, deps):
    episode_id = episode["id"]
    info = dict(info)
    info["podcast_name"] = podcast_name
    entry = _without_missing_files(ledger.get(info["id"], episode_id))
    plan = prepare_plan(entry, info)
    title = episode.get("title") or "episode"
    print(f"\nPrepare: {podcast_name} — {title} [{plan}]")
    if plan == "skip":
        return

    original = Path(deps["original_path"](info, episode))
    if plan == "download" or plan == "download_and_slice" or not original.exists():
        original.parent.mkdir(parents=True, exist_ok=True)
        deps["download"](episode.get("enclosureUrl"), original)
        ledger.update(
            info["id"],
            episode_id,
            podcast_name=podcast_name,
            title=title,
            published=episode.get("datePublished"),
            downloaded=True,
            original_path=str(original),
            sliced=False,
            copied_sliced=False,
        )

    if plan == "download":
        trimmed = Path(deps["trimmed_path"](info, episode))
        if not trimmed.exists():
            trimmed.parent.mkdir(parents=True, exist_ok=True)
            deps["render_trimmed"](info, str(original), trimmed)
            ledger.update(info["id"], episode_id, trimmed_path=str(trimmed), sliced=False)
        return

    started = time.perf_counter()
    segments = deps["transcribe"](str(original))
    transcribe_seconds = time.perf_counter() - started

    sliced = Path(deps["sliced_path"](info, episode))
    chapters = []
    if deps.get("load_chapters"):
        try:
            chapters = deps["load_chapters"](episode) or []
        except Exception as error:
            print(f"\tChapters unavailable: {error}")
    render_info = dict(info)
    render_info["chapters"] = chapters
    started = time.perf_counter()
    audio_seconds = deps["render_sliced"](render_info, str(original), sliced, segments)
    slice_seconds = time.perf_counter() - started

    trimmed_path = entry.get("trimmed_path") if entry else None
    if trimmed_path:
        Path(trimmed_path).unlink(missing_ok=True)
    ledger.update(
        info["id"],
        episode_id,
        sliced=True,
        sliced_path=str(sliced),
        trimmed_path=None,
        copied_sliced=False,
    )
    deps["record_timing"]({
        "podcast": podcast_name,
        "episode": title,
        "episode_id": episode_id,
        "audio_seconds": audio_seconds,
        "transcribe_seconds": round(transcribe_seconds, 3),
        "slice_seconds": round(slice_seconds, 3),
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })


def _sync_one(podcast_name, info, episode, ledger, deps, since=0, replace=False):
    episode_id = episode["id"]
    info = dict(info)
    info["podcast_name"] = podcast_name
    entry = _without_missing_files(ledger.get(info["id"], episode_id))
    title = episode.get("title") or "episode"
    drive_path = deps["drive_path"](info, episode)
    relative = _drive_relative(deps.get("drive_root"), drive_path)
    removed = {str(item) for item in deps.get("removed") or ()}
    reason = _leave_on_headphones(
        episode, info, since, drive_path, replace, relative=relative, removed=removed,
    )
    if reason:
        print(f"\nSync: {podcast_name} — {title} [{reason}]")
        _remember_copy(ledger, info, episode_id)
        return
    plan = sync_plan(entry)
    print(f"\nSync: {podcast_name} — {title} [{plan}]")

    if plan == "copy_sliced":
        _copy_episode(deps, podcast_name, title, entry["sliced_path"], drive_path)
        ledger.update(info["id"], episode_id, copied_sliced=True)
        return

    original = Path(deps["original_path"](info, episode))
    if plan == "download_trim_copy" or not original.exists():
        original.parent.mkdir(parents=True, exist_ok=True)
        deps["download"](episode.get("enclosureUrl"), original)
        ledger.update(
            info["id"],
            episode_id,
            podcast_name=podcast_name,
            title=title,
            published=episode.get("datePublished"),
            downloaded=True,
            original_path=str(original),
            sliced=False,
            copied_sliced=False,
        )

    trimmed = Path(deps["trimmed_path"](info, episode))
    if not trimmed.exists():
        trimmed.parent.mkdir(parents=True, exist_ok=True)
        deps["render_trimmed"](info, str(original), trimmed)
        ledger.update(info["id"], episode_id, trimmed_path=str(trimmed))
    _copy_episode(deps, podcast_name, title, str(trimmed), drive_path)


def _copy_episode(deps, podcast_name, title, source, drive_path):
    note = deps.get("note_copy")
    if note:
        note(podcast_name, title)
    try:
        deps["copy_file"](source, drive_path)
    finally:
        if note:
            note(None, None)


def _without_missing_files(entry):
    if not entry:
        return None
    entry = dict(entry)
    if entry.get("sliced_path") and not Path(entry["sliced_path"]).exists():
        entry["sliced"] = False
        entry["sliced_path"] = None
    if entry.get("original_path") and not Path(entry["original_path"]).exists():
        entry["downloaded"] = False
        entry["original_path"] = None
    return entry
