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
        for episode in episodes:
            episode_id = episode.get("id")
            if episode_id is None:
                print(f"\tSkipping an episode of {podcast_name} with no id")
                continue
            keep_ids.append(episode_id)
            try:
                _prepare_one(podcast_name, info, episode, ledger, deps)
            except Exception as error:
                print(f"ERROR: Failed to prepare '{podcast_name}' episode {episode_id}: {error}")
        if episodes:
            ledger.prune_feed(info["id"], keep_ids)


def run_sync(drive_root, feeds, ledger, deps):
    for podcast_name, info in feeds:
        try:
            episodes = list(deps["fetch_episodes"](info) or [])
        except Exception as error:
            print(f"ERROR: Failed to fetch '{podcast_name}': {error}")
            continue
        count = info.get("force_latest") or info.get("num_episodes", 1)
        episodes = episodes[:count]
        for episode in episodes:
            episode_id = episode.get("id")
            if episode_id is None:
                print(f"\tSkipping an episode of {podcast_name} with no id")
                continue
            try:
                _sync_one(podcast_name, info, episode, ledger, deps)
            except Exception as error:
                print(f"ERROR: Failed to sync '{podcast_name}' episode {episode_id}: {error}")
        if episodes and deps.get("prune"):
            category_dir = str(Path(drive_root) / info["category"])
            deps["prune"](category_dir, podcast_name, count)


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


def _sync_one(podcast_name, info, episode, ledger, deps):
    episode_id = episode["id"]
    info = dict(info)
    info["podcast_name"] = podcast_name
    entry = _without_missing_files(ledger.get(info["id"], episode_id))
    plan = sync_plan(entry)
    title = episode.get("title") or "episode"
    print(f"\nSync: {podcast_name} — {title} [{plan}]")
    drive_path = deps["drive_path"](info, episode)

    if plan == "copy_sliced":
        deps["copy_file"](entry["sliced_path"], drive_path)
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
    deps["copy_file"](str(trimmed), drive_path)


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
