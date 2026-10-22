import json
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
