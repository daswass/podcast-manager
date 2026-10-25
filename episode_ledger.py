import json
import time
from datetime import datetime
from pathlib import Path


def support_dir():
    path = Path.home() / "Library/Application Support/podcast-manager"
    path.mkdir(parents=True, exist_ok=True)
    return path


class EpisodeLedger:
    def __init__(self, path):
        self.path = Path(path)
        self.episodes = {}
        if self.path.exists():
            payload = json.loads(self.path.read_text())
            self.episodes = payload.get("episodes", {})

    def get(self, feed_id, episode_id):
        return self.episodes.get(self._key(feed_id, episode_id))

    def update(self, feed_id, episode_id, **fields):
        key = self._key(feed_id, episode_id)
        entry = dict(self.episodes.get(key, {"feed_id": feed_id, "episode_id": episode_id}))
        entry.update(fields)
        self.episodes[key] = entry
        self.save()
        return entry

    def prune_feed(self, feed_id, keep_ids):
        keep = {str(episode_id) for episode_id in keep_ids}
        prefix = f"{feed_id}:"
        for key in list(self.episodes):
            if not key.startswith(prefix):
                continue
            episode_id = key.split(":", 1)[1]
            if episode_id in keep:
                continue
            entry = self.episodes.pop(key)
            for path_key in ("original_path", "trimmed_path", "sliced_path"):
                path = entry.get(path_key)
                if path:
                    Path(path).unlink(missing_ok=True)
        self.save()

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"episodes": self.episodes}, indent=2))
        temporary.replace(self.path)

    def _key(self, feed_id, episode_id):
        return f"{feed_id}:{episode_id}"


RETENTION_SECONDS = 14 * 24 * 60 * 60
_EPISODE_FILES = ("original_path", "sliced_path", "trimmed_path")
_KEEP_FILES = {"ledger.json", "needs_copy", "timing.jsonl"}


def prune_local_archive(ledger, root, now=None, max_age=RETENTION_SECONDS):
    """Drop local episode files older than two weeks.

    The newest episode of each show stays, even when it was published earlier,
    so a show that posts less often still has a file to play and copy.
    """
    episodes = getattr(ledger, "episodes", None)
    if not isinstance(episodes, dict):
        return []
    now = time.time() if now is None else now
    cutoff = now - max_age
    root = Path(root)

    newest = {}
    for key, entry in episodes.items():
        stamp = _entry_stamp(entry)
        feed_id = entry.get("feed_id", key.split(":", 1)[0])
        current = newest.get(feed_id)
        if current is None or stamp >= current[0]:
            newest[feed_id] = (stamp, key)
    protected = {key for _stamp, key in newest.values()}

    removed = []
    for key in list(episodes):
        if key in protected:
            continue
        entry = episodes[key]
        if _entry_stamp(entry) >= cutoff:
            continue
        _unlink_entry_files(entry)
        episodes.pop(key)
        removed.append(key)
    if removed:
        ledger.save()

    referenced = set()
    for entry in episodes.values():
        for path_key in _EPISODE_FILES:
            path = entry.get(path_key)
            if path:
                referenced.add(str(Path(path).resolve()))
    if root.is_dir():
        for path in list(root.rglob("*")):
            if not path.is_file() or path.name in _KEEP_FILES:
                continue
            try:
                resolved = str(path.resolve())
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if resolved in referenced or mtime >= cutoff:
                continue
            path.unlink(missing_ok=True)
        for path in sorted((path for path in root.rglob("*") if path.is_dir()), key=lambda item: len(item.parts), reverse=True):
            try:
                path.rmdir()
            except OSError:
                pass
    _trim_timing(root / "timing.jsonl", cutoff)
    return removed


def _entry_stamp(entry):
    try:
        published = float(entry.get("published") or 0)
    except (TypeError, ValueError):
        published = 0
    if published > 0:
        return published
    mtimes = []
    for path_key in _EPISODE_FILES:
        path = entry.get(path_key)
        if not path:
            continue
        file = Path(path)
        try:
            if file.exists():
                mtimes.append(file.stat().st_mtime)
        except OSError:
            continue
    return max(mtimes) if mtimes else 0


def _unlink_entry_files(entry):
    for path_key in _EPISODE_FILES:
        path = entry.get(path_key)
        if path:
            Path(path).unlink(missing_ok=True)


def _trim_timing(path, cutoff):
    if not path.exists():
        return
    kept = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            stamp = datetime.fromisoformat(json.loads(line)["at"]).timestamp()
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            kept.append(line)
            continue
        if stamp >= cutoff:
            kept.append(line)
    path.write_text(("\n".join(kept) + "\n") if kept else "")


def refresh_needs_copy(ledger, flag_path):
    flag_path = Path(flag_path)
    waiting = any(
        entry.get("sliced") and not entry.get("copied_sliced")
        for entry in ledger.episodes.values()
    )
    if waiting:
        flag_path.parent.mkdir(parents=True, exist_ok=True)
        flag_path.write_text("1\n")
    elif flag_path.exists():
        flag_path.unlink()
