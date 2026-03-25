# podcast-manager

To install required libraries to use this script, run:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Then add Podcast Index credentials in a local `.env` file:

```env
API_KEY=your_key_here
API_SECRET=your_secret_here
```

Notes:
- `.env` is ignored by git.
- On Python 3.13+, `audioop-lts` is installed automatically because `audioop` was removed from the stdlib and `pydub` still needs it.
- Default output behavior is unchanged: if you run the script normally, it targets OpenSwim (or SWIMBUDS fallback).
- You can override the destination without changing defaults:

```bash
python podcastsToShokz.py --output-dir /Volumes/WassClawShare/Podcasts
# or
python podcastsToShokz.py --daswassclaw
```
