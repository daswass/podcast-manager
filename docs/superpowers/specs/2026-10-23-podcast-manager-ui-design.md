# Local podcast manager

A browser page for the show library and the headphone drive. FastAPI serves a built React app. `shows.json` in the repo root is the only show list. The page and a hand edit both change that file. The 4:00 AM prepare job and the plug-in copy read it when they start.

## Config

`shows.json` has two lists, `folders` and `shows`.

A folder has:

- `name`, unique
- `cut_ads`, boolean

A show has:

- `name`
- `feed_id`, unique Podcast Index id
- `folder`, a folder name
- `skip_first` and `skip_last`, non-negative integers, seconds
- `playback_speed`, a positive number
- `num_episodes`, an integer of at least 1
- `cut_ads`, `true`, `false`, or `null`

`null` means inherit the folder. The effective value is the show’s own setting when it is `true` or `false`, otherwise the folder’s. The page shows the effective value and whether it is inherited. Changing a folder’s `cut_ads` updates every show that still inherits. Setting the switch on one show writes `true` or `false` for that show, and later folder changes leave it alone. Choosing inherit writes `null` again.

Speed and trim exist only on the show. The folder name “Music” does not change behavior. `cut_ads` false uses today’s music path: download, fixed trim, playback speed, no transcription and no sponsor cut. `cut_ads` true uses today’s spoken path.

The committed file seeds today’s library. BioHack, Finance, News, Second Takes, and Heroic have `cut_ads` true. Music has `cut_ads` false. Shows inherit that value, except Heroic with Brian Johnson, which is pinned off.

| Show | Folder | Feed | Speed | Trim | Kept |
|---|---|---|---|---|---|
| Ben Greenfield Life | BioHack | 310787 | 1.5 | 315 / 30 | 1 |
| The Human Upgrade with Dave Asprey | BioHack | 1031629 | 1.5 | 150 / 0 | 1 |
| Huberman Lab | BioHack | 1365758 | 1.5 | 120 / 20 | 1 |
| FT News Briefing | Finance | 73509 | 1.4 | 30 / 90 | 1 |
| Goldman Sachs The Markets | Finance | 6321730 | 1.4 | 10 / 30 | 1 |
| Unhedged | Finance | 6416251 | 1.4 | 32 / 45 | 1 |
| The Daily | Finance | 743229 | 1.4 | 32 / 45 | 1 |
| WSJ Whats News | Finance | 491326 | 1.4 | 20 / 10 | 1 |
| Speaking of Alternatives | Finance | 6879441 | 1.4 | 20 / 10 | 1 |
| Alt Goes Mainstream | Finance | 1384568 | 1.4 | 20 / 10 | 1 |
| The Intelligence | News | 743611 | 1.4 | 0 / 0 | 1 |
| Up First | News | 946759 | 1.4 | 0 / 0 | 1 |
| Hard Fork | Second Takes | 1026613 | 1.4 | 0 / 0 | 1 |
| Freakonomics Radio | Second Takes | 311840 | 1.4 | 0 / 0 | 1 |
| Paper Trail | Second Takes | 7800276 | 1.4 | 0 / 0 | 1 |
| Search Engine | Second Takes | 5220235 | 1.4 | 0 / 0 | 1 |
| Machine Gods | Second Takes | 8037119 | 1.4 | 0 / 0 | 1 |
| Revisionist History | Second Takes | 51341 | 1.4 | 0 / 0 | 1 |
| Heroic with Brian Johnson | Heroic | 842919 | 1.0 | 10 / 0 | 1 |
| The Daily Dad | Heroic | 743919 | 1.0 | 100 / 30 | 1 |
| The Daily Stoic | Heroic | 404905 | 1.0 | 65 / 40 | 1 |
| Deep House Moscow | Music | 1105989 | 1.0 | 0 / 0 | 2 |
| Heldeep Radio | Music | 838482 | 1.0 | 45 / 0 | 1 |
| Clublife Radio | Music | 41388 | 1.0 | 45 / 0 | 1 |

`load_podcast_feeds()` reads this file and looks up each feed URL from Podcast Index at run time. The hardcoded list and `force_latest` go away. `num_episodes` is the only count. The README tells you to edit the page or `shows.json`, not the Python list.

## Saving

The server is the only writer the page uses. It writes a temporary file beside `shows.json` and replaces the original. Each library response includes the file’s version. A save that does not carry the version the page loaded is refused, and the page reloads. A hand edit therefore wins over a stale page.

The page polls about once a second. A changed file appears without a restart. Number and text fields save about 300ms after the last change. Switches save at once. A focused field is not replaced by a poll. If the server refuses the save because the file changed, the page reloads and shows the file’s value.

If `shows.json` is unreadable or fails validation, the page shows the error and does not write. Prepare and copy exit with that error. They do not treat a bad file as an empty library, and they do not prune.

Validation refuses a duplicate `feed_id`, a duplicate folder name, a show whose folder does not exist, a non-positive speed, a `num_episodes` below 1, and a negative trim. Deleting a folder that still has shows is refused. Renaming a folder updates its shows. Removing a show deletes that object only. Audio files stay.

A new folder defaults `cut_ads` to true.

## The page

`./scripts/run-manager.sh` serves the built React app and the API at `http://127.0.0.1:8765` and opens the browser. UI development uses the Vite dev server, which proxies to that API. The everyday command does not require a second process.

The screen is dark and has two views, Library and Headphones.

Library lists folders. A folder row shows its name and its cut-ads switch. You can add a folder, rename it, and delete it when it is empty. Opening a show edits folder, start trim, end trim, speed, episodes to keep, and cut-ads (on, off, or inherit). There is no save button.

Add show has a search box and a paste field. Search queries Podcast Index and lists title, author, and feed id. Choosing a result fills the name and feed id. The paste field accepts a feed URL or a numeric id. Digits resolve through Podcast Index by feed id. Anything else resolves through Podcast Index by feed URL. The new show joins the folder selected in the dialog. That selection starts as the folder you had open, or the first folder when none is open. The show inherits that folder’s `cut_ads`.

Each show has Prepare. It runs in the background for that show only. The button stays disabled, with the reason, while the nightly marker is present or another prepare is running. The row shows preparing, then sliced, trimmed, or the error. The error stays until the next attempt. After a restart, the row shows the ledger (sliced, trimmed, or not prepared), not a leftover preparing state.

Headphones lists `/Volumes/SWIMBUDS` and `/Volumes/OpenSwim` when those directories exist. Files are grouped by folder. AppleDouble `._` files are hidden. Each listed file can be deleted from that volume. Deleting a file that is already gone refreshes the list. Nothing mounted shows an empty state. This delete does not remove the local copy. The copy job still prefers OpenSwim when both volumes are mounted.

## Tests

Unit tests cover effective `cut_ads`, an atomic replace, a stale save being refused, a duplicate feed being refused, a folder delete being blocked while it has shows, and an invalid file being left in place. A prepare request is refused when the running marker is present. The React side is covered by the API contract above. The existing unittest suite still passes.
