"""Local browser UI for the podcast-manager episode archive."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote

from flask import Flask, abort, jsonify, render_template_string, send_from_directory


ARCHIVE_DIR = Path(os.environ.get("PODCAST_ARCHIVE_DIR", "/tmp/podcast-manager")).resolve()
app = Flask(__name__)


def _episode_catalog() -> list[dict[str, str]]:
    """Return archive files, newest first, without exposing arbitrary paths."""
    if not ARCHIVE_DIR.is_dir():
        return []

    episodes = []
    for path in ARCHIVE_DIR.glob("*/*.mp3"):
        if not path.is_file():
            continue
        stat = path.stat()
        episodes.append(
            {
                "category": path.parent.name,
                "name": path.stem,
                "path": str(path.relative_to(ARCHIVE_DIR)),
                "url": f"/audio/{quote(path.parent.name)}/{quote(path.name)}",
                "modified": stat.st_mtime,
            }
        )
    return sorted(episodes, key=lambda episode: episode["modified"], reverse=True)


@app.get("/api/episodes")
def episodes():
    return jsonify(_episode_catalog())


@app.get("/audio/<category>/<filename>")
def audio(category: str, filename: str):
    # send_from_directory provides safe-join protection; constrain the extension too.
    if Path(category).name != category or not filename.lower().endswith(".mp3"):
        abort(404)
    directory = ARCHIVE_DIR / category
    if not directory.is_dir():
        abort(404)
    return send_from_directory(directory, filename, mimetype="audio/mpeg", conditional=True)


@app.get("/")
def index():
    return render_template_string(PAGE)


PAGE = """<!doctype html>
<html lang="en"><head>
  <meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Podcast Manager</title>
  <script src="https://unpkg.com/wavesurfer.js@7"></script>
  <style>
    :root { color-scheme: dark; font-family: Inter, system-ui, sans-serif; background:#101217; color:#f3f4f6; }
    body { margin:0 auto; max-width:900px; padding:32px 20px; } h1 { margin:0 0 6px; }
    p { color:#aeb6c6; } #player { display:none; background:#1a1f29; border-radius:12px; padding:20px; margin:24px 0; }
    #now-playing { font-weight:650; margin-bottom:14px; } #waveform { min-height:72px; }
    button { margin-top:14px; padding:8px 14px; border:0; border-radius:7px; background:#42d392; color:#07160f; font-weight:700; cursor:pointer; }
    ul { padding:0; list-style:none; } li { padding:14px; border-bottom:1px solid #292e3a; cursor:pointer; } li:hover { background:#1a1f29; }
    .category { color:#42d392; font-size:.8rem; text-transform:uppercase; margin-right:10px; } .empty { color:#aeb6c6; }
  </style>
</head><body>
  <h1>Podcast Manager</h1><p>Local processed-episode archive</p>
  <section id="player"><div id="now-playing"></div><div id="waveform"></div><button id="play">Play</button></section>
  <ul id="episodes"><li class="empty">Loading episodes…</li></ul>
  <script>
    const list = document.querySelector('#episodes'), player = document.querySelector('#player');
    const wavesurfer = WaveSurfer.create({ container:'#waveform', waveColor:'#64748b', progressColor:'#42d392', cursorColor:'#fff', height:76, barWidth:2, barGap:2 });
    const play = document.querySelector('#play');
    play.onclick = () => wavesurfer.playPause();
    wavesurfer.on('play', () => play.textContent = 'Pause'); wavesurfer.on('pause', () => play.textContent = 'Play');
    fetch('/api/episodes').then(r => r.json()).then(episodes => {
      if (!episodes.length) { list.innerHTML = '<li class="empty">No local episodes yet. Run the downloader first.</li>'; return; }
      list.innerHTML = '';
      episodes.forEach(episode => { const item = document.createElement('li'); const category = document.createElement('span'); category.className = 'category'; category.textContent = episode.category; item.append(category, document.createTextNode(episode.name));
        item.onclick = () => { player.style.display='block'; document.querySelector('#now-playing').textContent = episode.name; wavesurfer.load(episode.url); window.scrollTo({top:0, behavior:'smooth'}); }; list.append(item); });
    }).catch(() => list.innerHTML = '<li class="empty">Could not load the archive.</li>');
  </script>
</body></html>"""


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5050")), debug=False)
