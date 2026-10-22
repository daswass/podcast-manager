import argparse
import json
import requests
import os
import unicodedata
from pathlib import Path
from pydub import AudioSegment
import time
from datetime import datetime
import shutil
import glob
import re
import pytz

from ad_detect import chapter_spans, choose_cut_spans, classify_transcript, tighten_spans
from ad_model import complete_ad_prompt
from ad_slices import cut_ads, find_ad_spans
from episode_ledger import EpisodeLedger, refresh_needs_copy, support_dir
from episode_pipeline import run_prepare, run_sync
from whisper_transcribe import transcribe_episode

import ssl
try:
    _create_unverified_https_context = ssl._create_unverified_context
except AttributeError:
    # Legacy Python that doesn't verify HTTPS certificates by default
    pass
else:
    # Handle target environment that doesn't support HTTPS verification
    ssl._create_default_https_context = _create_unverified_https_context

import podcastindex
from dotenv import load_dotenv
load_dotenv()
config = {
    "api_key": os.getenv('API_KEY'),
    "api_secret": os.getenv('API_SECRET')
}
index = podcastindex.init(config)

def configure_ffmpeg():
    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        for candidate in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                ffmpeg_path = candidate
                break

    if not ffmpeg_path:
        print("Warning: ffmpeg not found; playback speed changes will be skipped.")
        return

    AudioSegment.converter = ffmpeg_path
    ffprobe_path = shutil.which("ffprobe") or ffmpeg_path.replace("ffmpeg", "ffprobe")
    if os.path.isfile(ffprobe_path):
        AudioSegment.ffprobe = ffprobe_path

configure_ffmpeg()

LOCAL_TIMEZONE = pytz.timezone('America/New_York')

def print_mp3_duration(audio, num_tabs=0):
    try:
        duration_ms = len(audio)
        duration_sec = duration_ms / 1000
        minutes = int(duration_sec / 60)
        seconds = int(duration_sec % 60)
        indentation = "\t" * num_tabs
        
        print(f"{indentation}Audio Duration: {minutes} minutes and {seconds} seconds")
    except Exception as e:
        print(f"Error: {e}")
        return None

def get_last_run_timestamp(usb_drive_base_dir):
    timestamp_file_1 = os.path.join(usb_drive_base_dir, "podcast_downloader_last_run.txt")
    timestamp_file_2 = "/tmp/podcast_downloader_last_run.txt"

    if os.path.exists(timestamp_file_1):
        with open(timestamp_file_1, "r") as file1:
            return int(file1.read())
    elif os.path.exists(timestamp_file_2):
        with open(timestamp_file_2, "r") as file2:
            return int(file2.read())
    
    return 0

def update_last_run_timestamp(usb_drive_base_dir):
    last_run = get_last_run_timestamp(usb_drive_base_dir)
    
    timestamp_file_1 = os.path.join(usb_drive_base_dir, "podcast_downloader_last_run.txt")
    timestamp_file_2 = "/tmp/podcast_downloader_last_run.txt"

    # Try to write to USB drive first, but fall back to temp if it fails (permission error, etc.)
    if os.path.exists(usb_drive_base_dir):
        try:
            with open(timestamp_file_1, "w") as file1:
                file1.write(str(int(time.time())))
        except (PermissionError, OSError):
            # USB drive might not be writable, fall back to temp location
            with open(timestamp_file_2, "w") as file2:
                file2.write(str(int(time.time())))
    else:
        with open(timestamp_file_2, "w") as file2:
            file2.write(str(int(time.time())))
    
    return last_run

RUNNING_MARKER_NAME = "podcast_downloader_running.txt"
TMP_RUNNING_MARKER = "/tmp/podcast_downloader_running.txt"

def running_marker_path(usb_drive_base_dir):
    return os.path.join(usb_drive_base_dir, RUNNING_MARKER_NAME)

def _running_marker_pid(path):
    try:
        with open(path) as file:
            for line in file:
                line = line.strip()
                if line.startswith("pid="):
                    return int(line.split("=", 1)[1])
    except (OSError, ValueError):
        return None
    return None

def running_marker_is_stale(path):
    if not os.path.exists(path):
        return False

    pid = _running_marker_pid(path)
    if pid is None:
        return True

    try:
        os.kill(pid, 0)
        return False
    except ProcessLookupError:
        return True
    except PermissionError:
        return False

def create_running_marker(usb_drive_base_dir):
    started = datetime.now(LOCAL_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
    epoch = int(time.time())
    pid = os.getpid()
    content = f"started={started}\npid={pid}\nepoch={epoch}\n"
    path = running_marker_path(usb_drive_base_dir)

    try:
        with open(path, "w") as file:
            file.write(content)
        print(f"Created running marker: {path}")
        return
    except (PermissionError, OSError) as e:
        print(f"Warning: Could not create running marker on drive ({e}); using {TMP_RUNNING_MARKER}")

    with open(TMP_RUNNING_MARKER, "w") as file:
        file.write(content)

def remove_running_marker(usb_drive_base_dir):
    for path in (running_marker_path(usb_drive_base_dir), TMP_RUNNING_MARKER):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError as e:
            print(f"Warning: Could not remove running marker {path}: {e}")

def acquire_running_marker(usb_drive_base_dir):
    path = running_marker_path(usb_drive_base_dir)
    if os.path.exists(path):
        if running_marker_is_stale(path):
            print("Removing stale podcast_downloader_running.txt")
            remove_running_marker(usb_drive_base_dir)
        else:
            print("Podcast updater already running (podcast_downloader_running.txt on drive).")
            return False

    if os.path.exists(TMP_RUNNING_MARKER):
        if running_marker_is_stale(TMP_RUNNING_MARKER):
            remove_running_marker(usb_drive_base_dir)
        else:
            print("Podcast updater already running (podcast_downloader_running.txt in /tmp).")
            return False

    create_running_marker(usb_drive_base_dir)
    return True

def safe_episode_title(title):
    """Normalize and strip characters that break exFAT/FAT32 or confuse macOS MSC volumes."""
    title = unicodedata.normalize("NFC", title)
    title = re.sub(r"[ /]", "_", title)
    title = re.sub(r'[<>:"\\|?*]', "_", title)
    title = re.sub(r"[\x00-\x1f]", "_", title)
    title = title.rstrip(" .")
    return title or "episode"


def _episode_mtime_or_sentinel(path):
    try:
        return os.path.getmtime(path)
    except OSError as e:
        print(f"\tWarning: Could not read mtime for {path} ({e}); pruning may be less accurate.")
        return float("-inf")


def delete_old_episodes(category_dir, podcast_name, num_episodes):
    episode_files = glob.glob(os.path.join(category_dir, f"{podcast_name} - *"))
    episode_files = [p for p in episode_files if os.path.isfile(p)]
    episode_files.sort(key=_episode_mtime_or_sentinel)

    while len(episode_files) > num_episodes:
        oldest_episode = episode_files.pop(0)
        try:
            os.remove(oldest_episode)
        except OSError as e:
            print(f"\tWarning: Could not remove old episode {oldest_episode}: {e}")

def trim_audio(episode_file_path, skip_first, skip_last):
    if skip_first > 0 or skip_last > 0:
        audio = AudioSegment.from_mp3(episode_file_path)
        os.remove(episode_file_path)

        print(f"\tTrimming: [start = {skip_first}s, end = {skip_last}s] ...")
        
        if skip_last == 0:
            trimmed_audio = audio[skip_first * 1000 :]
        else:
            trimmed_audio = audio[skip_first * 1000 : -skip_last * 1000]

        trimmed_audio.export(episode_file_path, format="mp3")
        print_mp3_duration(trimmed_audio, num_tabs=2)

def squeeze_audio(episode_file_path, playback_speed):
    if playback_speed != 1.0:
        try:
            audio = AudioSegment.from_mp3(episode_file_path)
            os.remove(episode_file_path)

            print(f"\tChanging speed: [{playback_speed}x] ...")

            audio.export(episode_file_path, format="mp3", parameters=["-filter:a", f"atempo={playback_speed}"])
            
            squeezed_audio = AudioSegment.from_mp3(episode_file_path)
            print_mp3_duration(squeezed_audio, num_tabs=2)
        except Exception as e:
            print(f"\tFailed to squeeze audio: {e}")

def process_audio(info, episode_file_path, apply_trim=True):
    audio = AudioSegment.from_mp3(episode_file_path)
    print_mp3_duration(audio)
    
    skip_first = info.get("skip_first", 0)  # Default to 0 if not specified
    skip_last = info.get("skip_last", 0)  # Default to 0 if not specified
    playback_speed = info.get("playback_speed", 1.0)  # Default to normal speed (1.0)

    if apply_trim:
        trim_audio(episode_file_path, skip_first, skip_last)
    squeeze_audio(episode_file_path, playback_speed)

def retry_api_call(func, *args, max_retries=3, initial_delay=1, **kwargs):
    """Retry an API call with exponential backoff"""
    delay = initial_delay
    last_exception = None
    
    for attempt in range(max_retries):
        try:
            return func(*args, **kwargs)
        except requests.exceptions.HTTPError as e:
            last_exception = e
            # Check if it's a 500 error and we have retries left
            if hasattr(e, 'response') and e.response is not None and e.response.status_code == 500:
                if attempt < max_retries - 1:
                    print(f"\tAPI call failed with 500 error (attempt {attempt + 1}/{max_retries}). Retrying in {delay}s...")
                    time.sleep(delay)
                    delay *= 2  # Exponential backoff
                else:
                    print(f"\tAPI call failed after {max_retries} attempts. Last error: {e}")
                    raise
            else:
                # For non-500 errors, don't retry
                raise
        except Exception as e:
            # For other exceptions, don't retry
            raise
    
    if last_exception:
        raise last_exception

def podcast(category, id, trim, num_episodes, speed, force_latest=0):
    result = retry_api_call(index.podcastByFeedId, id)

    feed = result.get("feed")
    if isinstance(feed, list):
        if len(feed) == 0:
            raise ValueError(f"No feed found for id {id}")
        podcast_url = feed[0]["url"]
    elif isinstance(feed, dict):
        podcast_url = feed["url"]
    else:
        raise ValueError(f"Unexpected feed type for id {id}: {type(feed)}")
    
    return {
        "id": id,
        "url": podcast_url,
        "category": category,
        "num_episodes": num_episodes,
        "skip_first": trim[0],
        "skip_last": trim[1],
        "playback_speed": speed,
        "force_latest": force_latest
    }

def podcast_finance(id, trim, num_episodes=1, speed=1.4):
    obj = podcast("Finance", id, trim, num_episodes, speed)
    return obj

def podcast_news(id, trim, num_episodes=1, speed=1.4):
    obj = podcast("News", id, trim, num_episodes, speed)
    return obj

def podcast_biohack(id, trim, num_episodes=1, speed=1.5):
    obj = podcast("BioHack", id, trim, num_episodes, speed)
    return obj

def podcast_heroic(id, trim, num_episodes=1, speed=1.0):
    obj = podcast("Heroic", id, trim, num_episodes, speed)
    return obj

def podcast_music(id, trim, num_episodes):
    obj = podcast("Music", id, trim, num_episodes, 1.0)
    obj["slice"] = False
    return obj

# Spotify's prefix hosts embed the real media URL in the path, then refuse
# the connection. Strip them before download. Leave working trackers in place.
_BROKEN_ENCLOSURE_MARKERS = (
    "prfx.byspotify.com/e/",
    "pscrb.fm/rss/p/",
)


def unwrap_enclosure_url(url):
    if not url:
        return url

    changed = True
    while changed:
        changed = False
        for marker in _BROKEN_ENCLOSURE_MARKERS:
            index = url.find(marker)
            if index != -1:
                url = url[index + len(marker):]
                changed = True
                break

    if url.startswith("http://") or url.startswith("https://"):
        return url
    return "https://" + url.lstrip("/")


def episode_filename(podcast_name, episode):
    return f"{podcast_name} - {safe_episode_title(episode.get('title') or 'episode')}.mp3"


def original_episode_path(support, info, episode):
    return support / "originals" / str(info["id"]) / f"{episode['id']}.mp3"


def sliced_episode_path(support, podcast_name, info, episode):
    return support / "sliced" / info["category"] / episode_filename(podcast_name, episode)


def trimmed_episode_path(support, podcast_name, info, episode):
    return support / "trimmed" / info["category"] / episode_filename(podcast_name, episode)


def drive_episode_path(drive_root, podcast_name, info, episode):
    return os.path.join(drive_root, info["category"], episode_filename(podcast_name, episode))


# Acast answers the requests library's default client with 403 and an HTML
# block page. Any other client name is sent through to the mp3.
_DOWNLOAD_HEADERS = {"User-Agent": "podcast-manager"}


def download_episode_file(url, dest_path):
    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    unwrapped = unwrap_enclosure_url(url)
    with requests.get(unwrapped, timeout=120, stream=True, headers=_DOWNLOAD_HEADERS) as response:
        response.raise_for_status()
        with dest.open("wb") as handle:
            for chunk in response.iter_content(1024 * 256):
                if chunk:
                    handle.write(chunk)
    if dest.stat().st_size < 1024:
        dest.unlink(missing_ok=True)
        raise ValueError(f"Downloaded file is too small: {unwrapped}")


def load_episode_chapters(episode):
    url = episode.get("chaptersUrl")
    if not url:
        return []
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    payload = response.json()
    if isinstance(payload, list):
        return payload
    return payload.get("chapters") or []


def render_sliced_episode(info, source_path, dest_path, segments):
    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    audio = AudioSegment.from_mp3(source_path)
    audio_seconds = len(audio) / 1000
    publisher = chapter_spans(info.get("chapters") or [], audio_seconds)
    phrase_spans = find_ad_spans(segments)
    model_spans = None
    if segments:
        try:
            model_spans = classify_transcript(segments, audio_seconds, complete_ad_prompt)
        except Exception as error:
            print(f"\tAd model unavailable: {error}")
    if model_spans is None:
        print("\tPhrase matches are the fallback")
    else:
        print(f"\tAd model marked {len(model_spans)} span(s)")
    spans = tighten_spans(audio, choose_cut_spans(phrase_spans, model_spans, publisher))
    if spans:
        # The fixed skip_first/skip_last values target the same pre-roll and
        # post-roll. Applying them after a detected cut would remove the show.
        print(f"\tRemoving {len(spans)} ad span(s)")
        cut_ads(audio, spans).export(str(dest), format="mp3")
        process_audio(info, str(dest), apply_trim=False)
    else:
        print("\tNo ad spans found; using the fixed trim and speed")
        shutil.copy(source_path, dest)
        process_audio(info, str(dest), apply_trim=True)
    return audio_seconds


def render_trimmed_episode(info, source_path, dest_path):
    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(source_path, dest)
    process_audio(info, str(dest), apply_trim=True)


def copy_to_drive(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.exists(dst):
        src_stat = os.stat(src)
        dst_stat = os.stat(dst)
        if dst_stat.st_size == src_stat.st_size and dst_stat.st_mtime >= src_stat.st_mtime:
            return
    shutil.copy2(src, dst)


def append_timing(row):
    path = support_dir() / "timing.jsonl"
    with path.open("a") as handle:
        handle.write(json.dumps(row) + "\n")
    print(
        f"\tTiming: audio {row['audio_seconds']:.0f}s, "
        f"transcribe {row['transcribe_seconds']:.1f}s, "
        f"slice {row['slice_seconds']:.1f}s"
    )


def fetch_latest_episodes(info):
    count = info.get("force_latest") or info.get("num_episodes", 1)
    feed = retry_api_call(index.episodesByFeedId, info["id"], max_results=max(count, 1))
    items = (feed or {}).get("items") or []
    return items[:count]


def _sync_dependencies(support, drive_root):
    return {
        "fetch_episodes": fetch_latest_episodes,
        "download": download_episode_file,
        "render_trimmed": render_trimmed_episode,
        "copy_file": copy_to_drive,
        "prune": delete_old_episodes,
        "original_path": lambda info, episode: original_episode_path(support, info, episode),
        "trimmed_path": lambda info, episode: trimmed_episode_path(support, info["podcast_name"], info, episode),
        "sliced_path": lambda info, episode: sliced_episode_path(support, info["podcast_name"], info, episode),
        "drive_path": lambda info, episode: drive_episode_path(drive_root, info["podcast_name"], info, episode),
        "safe_title": safe_episode_title,
    }


def _prepare_dependencies(support):
    return {
        "fetch_episodes": fetch_latest_episodes,
        "download": download_episode_file,
        "transcribe": transcribe_episode,
        "load_chapters": load_episode_chapters,
        "render_sliced": render_sliced_episode,
        "render_trimmed": render_trimmed_episode,
        "original_path": lambda info, episode: original_episode_path(support, info, episode),
        "sliced_path": lambda info, episode: sliced_episode_path(support, info["podcast_name"], info, episode),
        "trimmed_path": lambda info, episode: trimmed_episode_path(support, info["podcast_name"], info, episode),
        "record_timing": append_timing,
        "safe_title": safe_episode_title,
    }


def main(usb_drive_base_dir):
    support = str(support_dir())
    if not acquire_running_marker(support):
        return
    if not acquire_running_marker(usb_drive_base_dir):
        remove_running_marker(support)
        return

    try:
        _run_podcast_update(usb_drive_base_dir)
    finally:
        remove_running_marker(usb_drive_base_dir)
        remove_running_marker(support)

def load_podcast_feeds():
    # Dictionary mapping podcast names to their RSS feed URLs, categories, number of episodes to download, skip_first, skip_last, playback_speed
    # See: https://podcastindex.org/
    podcast_definitions = [
        ("Ben Greenfield Life",                  lambda: podcast_biohack(310787,  [315 ,30])),
        ("The Human Upgrade with Dave Asprey",   lambda: podcast_biohack(1031629, [150, 0])),
        ("Huberman Lab",                         lambda: podcast_biohack(1365758, [120, 20])),
        ("FT News Briefing",                     lambda: podcast_finance(73509,  [30, 90])),
        ("Goldman Sachs The Markets",            lambda: podcast_finance(6321730,[10, 30])),
        ("Unhedged",                             lambda: podcast_finance(6416251,[32, 45])),
        ("The Daily",                            lambda: podcast_finance(743229, [32, 45])),
        ("WSJ Whats News",                       lambda: podcast_finance(491326, [20, 10])),
        ("Speaking of Alternatives",             lambda: podcast_finance(6879441,[20, 10])),
        ("Alt Goes Mainstream",                  lambda: podcast_finance(1384568,[20, 10])),
        ("Hard Fork",                            lambda: podcast_news(1026613, [0, 0])),
        ("The Intelligence",                     lambda: podcast_news(743611,  [0, 0])),
        ("Freakonomics Radio",                   lambda: podcast_news(311840,  [0, 0])),
        ("Up First",                             lambda: podcast_news(946759,  [0, 0])),
        ("Paper Trail",                          lambda: podcast_news(7800276, [0, 0])),
        ("Search Engine",                        lambda: podcast_news(5220235, [0, 0])),
        ("Machine Gods",                         lambda: podcast_news(8037119, [0, 0])),
        #("Hot Money: Agent of Chaos",            lambda: podcast("Hot Money", 5464375, [20, 10], 1, 1.4, 1)),
        ("Heroic with Brian Johnson",            lambda: podcast_heroic(842919, [10, 0])),
        ("The Daily Dad",                        lambda: podcast_heroic(743919, [100, 30])),
        ("The Daily Stoic",                      lambda: podcast_heroic(404905, [65, 40])),
        ("Deep House Moscow",                    lambda: podcast_music(1105989, [0, 0], 2)),
        ("Heldeep Radio",                        lambda: podcast_music(838482,  [45, 0], 1)),
        ("Clublife Radio",                       lambda: podcast_music(41388,   [45, 0], 1)),
    ]
    
    feeds = []
    for podcast_name, podcast_func in podcast_definitions:
        try:
            feeds.append((podcast_name, podcast_func()))
        except Exception as e:
            print(f"ERROR: Failed to initialize podcast '{podcast_name}': {e}")
            print(f"       Skipping this podcast and continuing with others...")
            continue
    return feeds

def _run_podcast_update(usb_drive_base_dir):
    if not os.path.exists(usb_drive_base_dir):
        print("Error: USB drive not mounted. Please connect the USB drive and try again.")
        return

    feeds = load_podcast_feeds()
    support = support_dir()
    ledger = EpisodeLedger(support / "ledger.json")
    print("Copying ready episodes and downloading anything not prepared yet...")
    try:
        run_sync(usb_drive_base_dir, feeds, ledger, _sync_dependencies(support, usb_drive_base_dir))
    finally:
        refresh_needs_copy(ledger, support / "needs_copy")
    update_last_run_timestamp(usb_drive_base_dir)
    print("Podcast episodes organized and downloaded. Big Day!")

def prepare_main():
    support = support_dir()
    if not acquire_running_marker(str(support)):
        return

    try:
        feeds = load_podcast_feeds()
        ledger = EpisodeLedger(support / "ledger.json")
        print("Preparing episodes. This transcribes and slices; it does not need the headphones.")
        try:
            run_prepare(feeds, ledger, _prepare_dependencies(support))
        finally:
            refresh_needs_copy(ledger, support / "needs_copy")
        print("Nightly prepare finished.")
    finally:
        remove_running_marker(str(support))

def wait_for_usb_drive(usb_base_drive_dir):
    while not os.path.exists("/Volumes/OpenSwim/") and not os.path.exists("/Volumes/SWIMBUDS/"):
        print("\rWaiting for USB drive{} ".format("." * ((int(time.time()) % 3) + 1)), end="", flush=True)
        time.sleep(1)
        print("\r" + " " * 50, end="", flush=True)  # Clear the line
    
    # Determine which drive is available
    if os.path.exists("/Volumes/OpenSwim/"):
        usb_base_drive_dir = "/Volumes/OpenSwim/"
    elif os.path.exists("/Volumes/SWIMBUDS/"):
        usb_base_drive_dir = "/Volumes/SWIMBUDS/"
    
    print("\rUSB drive is available at {}".format(usb_base_drive_dir))
    main(usb_base_drive_dir)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Download podcasts for SWIMBUDS")
    parser.add_argument("--prepare", action="store_true", help="Download, transcribe, and slice without the headphones")
    args = parser.parse_args()
    if args.prepare:
        prepare_main()
        raise SystemExit(0)

    # Check which USB drive is mounted (OpenSwim takes priority)
    if os.path.exists("/Volumes/OpenSwim/"):
        usb_drive_base_dir = "/Volumes/OpenSwim/"
    elif os.path.exists("/Volumes/SWIMBUDS/"):
        usb_drive_base_dir = "/Volumes/SWIMBUDS/"
    else:
        # Default to OpenSwim when waiting
        usb_drive_base_dir = "/Volumes/OpenSwim/"

    if os.getuid() == 0:
        six_hours_ago = int(time.time()) - (3 * 60 * 60)
        last_run_timestamp = get_last_run_timestamp(usb_drive_base_dir)

        if last_run_timestamp > six_hours_ago:
            last_run_date_string = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last_run_timestamp))
            print(f"Skip run. Updated within the last 3 hours. Last run: {last_run_date_string}")
        else:
            main(usb_drive_base_dir)
    else:
        wait_for_usb_drive(usb_drive_base_dir)
