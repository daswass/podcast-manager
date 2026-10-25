# podcast-manager

Downloads, trims, and speed-adjusts podcast episodes for the OpenSwim/SwimBuds drive.
The processed files are retained locally in `/tmp/podcast-manager` and pruned using
the same per-podcast episode count that is copied to the headphones.

## Install

```bash
pip install -r requirements.txt
```

## Local player

Run the downloader at least once, then install the dedicated local web service:

```bash
./scripts/install-web-player.sh
```

This creates/refreshes its isolated `.venv`, installs the canonical
`launchagents/com.wassclaw.podcast-manager-web.plist` as a per-user LaunchAgent,
and starts the player at <http://127.0.0.1:5050>. It is separate from the
External Brain review service on port 8765.

For one-off foreground development, run `python web_ui.py`. The player uses
WaveSurfer.js, with waveform scrubbing and standard play/pause controls. Set
`PODCAST_ARCHIVE_DIR` to serve a different archive location.
