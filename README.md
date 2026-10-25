# podcast-manager

Downloads the latest episode of each show, removes sponsor reads from spoken shows, and copies the result to Shokz headphones (SWIMBUDS or OpenSwim).

Two jobs do this:

- **4:00 AM prepare** does not need the headphones. It downloads shows that cut ads, transcribes them, and cuts sponsor reads. Shows with cut ads turned off are downloaded and trimmed only.
- **Copy** runs when the headphones mount, and again at 6:40 AM if they are still mounted. It copies files that prepare already finished. A show that published a newer episode after 4:00 AM is downloaded then and given its fixed trim.

OpenSwim is used when both drives are mounted.

## Setup

Apple silicon. The transcriber and the ad model run through MLX.

```bash
./scripts/install.sh
```

The installer asks whether to install two optional login jobs, then asks for a [Podcast Index](https://podcastindex.org/) key when `.env` is missing. Those keys are written to `.env`. It then creates the virtualenv, installs Python dependencies, and builds the page. The optional jobs are:

- the headphone copy watcher and the 4:00 AM prepare job
- the manager website at http://127.0.0.1:8765, started at login

`.env` and `local.env` are gitignored. `local.env` holds the LaunchAgent label prefix (`com.podcast-manager` by default). Copy `.env.example` and `local.env.example` if you want to fill them in by hand. The scripts find this checkout from their own path.

The mount watcher needs Full Disk Access for `/bin/bash` (System Settings → Privacy & Security → Full Disk Access). The 4:00 AM job runs the next time the Mac is awake at 4:00 AM. ffmpeg has to be installed (`brew install ffmpeg`); the installer offers to do that when Homebrew is available.

## Run it by hand

```bash
./scripts/run-manager.sh
./venv/bin/python podcastsToShokz.py --prepare
./scripts/run-on-swimbuds-mount.sh
```

`./scripts/run-manager.sh` serves the page and the API at http://127.0.0.1:8765 and opens the browser. A badge at the top shows when a download or a headphone copy is running. Library is the show list. When SWIMBUDS or OpenSwim is plugged in, each show says whether its latest episode is on that drive. OpenSwim is the drive that counts when both are plugged in. Headphones lists SWIMBUDS and OpenSwim when those drives are plugged in, and can delete a file from the drive without touching the local copy. Prepare on a show runs that show only. If SWIMBUDS or OpenSwim is plugged in, it also copies that show and keeps only its configured number of episodes. A rose Ads badge means the latest episode kept its ads. Click it to remove ads from that episode and copy the show to connected headphones. The show’s saved cut-ads setting stays as it is. OpenSwim wins when both are plugged in. The 4:00 AM job and the plug-in copy stay on their own schedule.

While changing the page, run `npm --prefix web run dev` and leave the API on port 8765. The dev server proxies `/api` to it.

Prepare logs to `/tmp/podcast-nightly-prepare.log`. Copy logs to `/tmp/podcast-swimbuds-updater.log`. Per-episode timings land in `~/Library/Application Support/podcast-manager/timing.jsonl`.

The copy job skips itself when the headphones were updated in the last 3 hours, unless prepare has left a `needs_copy` flag for a finished slice.

## Where files go

Local files live in `~/Library/Application Support/podcast-manager/`:

| Path | What it is |
|---|---|
| `ledger.json` | Which episode was downloaded, sliced, and copied |
| `originals/<feed id>/<episode id>.mp3` | The download |
| `sliced/<folder>/` | Spoken shows after sponsor cuts and speed change |
| `trimmed/<folder>/` | A show with cut ads off, or a spoken show that was not sliced |
| `needs_copy` | Present when a sliced file still needs to reach the headphones |

The headphones get one file per show (two for Deep House Moscow), under `BioHack`, `Finance`, `News`, `Second Takes`, `Heroic`, or `Music`.

## How sponsor reads are removed

Spoken shows use publisher chapters whose titles are ads, sponsors, or promos, plus a local model (`mlx-community/Qwen2.5-7B-Instruct-4bit`) over a Whisper transcript (`mlx-community/whisper-large-v3-turbo`). The model marks the lines that belong to each sponsor read. A short phrase list is used only when the model does not run.

An edge can snap a few seconds to a nearby silence. When any sponsor span is found, the fixed start/end trim below is not applied a second time. Playback speed still is. A show with cut ads off never takes this path.

Acast refuses Python’s default client. Downloads send `User-Agent: podcast-manager`.

## Shows

Trim is seconds cut from the start and the end when no sponsor spans were found. Speed is the playback rate. The feed id is the Podcast Index id. Add, remove, and edit shows on the page or in `shows.json`. A show with `cut_ads` null inherits its folder. Pinning a show to true or false leaves it alone when the folder changes. A new folder defaults cut ads to on. The folder name Music does not change behavior; Music is off because its `cut_ads` value is false.

| Show | Folder | Feed | Speed | Trim | Kept |
|---|---|---|---|---|---|
| Ben Greenfield Life | BioHack | 310787 | 1.5x | 315 / 30 | 1 |
| The Human Upgrade with Dave Asprey | BioHack | 1031629 | 1.5x | 150 / 0 | 1 |
| Huberman Lab | BioHack | 1365758 | 1.5x | 120 / 20 | 1 |
| FT News Briefing | Finance | 73509 | 1.4x | 30 / 90 | 1 |
| Goldman Sachs The Markets | Finance | 6321730 | 1.4x | 10 / 30 | 1 |
| Unhedged | Finance | 6416251 | 1.4x | 32 / 45 | 1 |
| The Daily | Finance | 743229 | 1.4x | 32 / 45 | 1 |
| WSJ Whats News | Finance | 491326 | 1.4x | 20 / 10 | 1 |
| Speaking of Alternatives | Finance | 6879441 | 1.4x | 20 / 10 | 1 |
| Alt Goes Mainstream | Finance | 1384568 | 1.4x | 20 / 10 | 1 |
| The Intelligence | News | 743611 | 1.4x | none | 1 |
| Up First | News | 946759 | 1.4x | none | 1 |
| Hard Fork | Second Takes | 1026613 | 1.4x | none | 1 |
| Freakonomics Radio | Second Takes | 311840 | 1.4x | none | 1 |
| Paper Trail | Second Takes | 7800276 | 1.4x | none | 1 |
| Search Engine | Second Takes | 5220235 | 1.4x | none | 1 |
| Machine Gods | Second Takes | 8037119 | 1.4x | none | 1 |
| Revisionist History | Second Takes | 51341 | 1.4x | none | 1 |
| Heroic with Brian Johnson | Heroic | 842919 | 1.0x | 10 / 0 | 1 |
| The Daily Dad | Heroic | 743919 | 1.0x | 100 / 30 | 1 |
| The Daily Stoic | Heroic | 404905 | 1.0x | 65 / 40 | 1 |
| Deep House Moscow | Music | 1105989 | 1.0x | none | 2 |
| Heldeep Radio | Music | 838482 | 1.0x | 45 / 0 | 1 |
| Clublife Radio | Music | 41388 | 1.0x | 45 / 0 | 1 |

Clublife’s Podcast Index feed is Tiësto’s show, currently published under the title PRISMATIC. The headphone filename stays `Clublife Radio`.

## Tests

```bash
./venv/bin/python -m unittest
```

## Local player

Processed episodes stay under `~/Library/Application Support/podcast-manager`. This serves that archive in the browser:

```bash
./scripts/install-web-player.sh
```

The script installs a per-user LaunchAgent for this checkout and starts the player at http://127.0.0.1:5050. For a one-off run, use `./venv/bin/python web_ui.py`. Set `PODCAST_ARCHIVE_DIR` to serve a different folder.
