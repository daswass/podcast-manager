import requests
import os
from pydub import AudioSegment
import time
from datetime import datetime
import shutil
import glob
import re
import pytz

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

TMP_DIR = "/tmp/OpenSwim/"
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

def delete_old_episodes(category_dir, podcast_name, num_episodes):
    # Get a list of episode files in the category directory
    episode_files = glob.glob(os.path.join(category_dir, f"{podcast_name} - *"))

    # Sort the episode files by modification time (oldest first)
    episode_files.sort(key=os.path.getmtime)

    # If there are more episode files than the allowed number, delete the older ones
    while len(episode_files) > num_episodes:
        oldest_episode = episode_files.pop(0)
        os.remove(oldest_episode)

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
        except:
            print("Failed to squeeze audio")

def process_audio(info, episode_file_path):
    audio = AudioSegment.from_mp3(episode_file_path)
    print_mp3_duration(audio)
    
    skip_first = info.get("skip_first", 0)  # Default to 0 if not specified
    skip_last = info.get("skip_last", 0)  # Default to 0 if not specified
    playback_speed = info.get("playback_speed", 1.0)  # Default to normal speed (1.0)

    trim_audio(episode_file_path, skip_first, skip_last)
    squeeze_audio(episode_file_path, playback_speed)

def validate_timestamp_logic(last_run_timestamp, current_timestamp):
    """Validate and debug timestamp logic to help identify issues"""
    last_run_date = datetime.fromtimestamp(last_run_timestamp, tz=pytz.UTC).astimezone(LOCAL_TIMEZONE)
    current_date = datetime.fromtimestamp(current_timestamp, tz=pytz.UTC).astimezone(LOCAL_TIMEZONE)
    
    time_diff = current_timestamp - last_run_timestamp
    hours_diff = time_diff / 3600
    
    print(f"Timestamp Validation:")
    print(f"  Last run: {last_run_timestamp} ({last_run_date.strftime('%Y-%m-%d %H:%M:%S')})")
    print(f"  Current:  {current_timestamp} ({current_date.strftime('%Y-%m-%d %H:%M:%S')})")
    print(f"  Difference: {time_diff} seconds ({hours_diff:.2f} hours)")
    
    if time_diff < 0:
        print(f"  WARNING: Last run timestamp is in the future! This could cause issues.")
    elif time_diff == 0:
        print(f"  WARNING: Timestamps are identical. This could cause episodes to be missed.")
    elif time_diff < 60:
        print(f"  WARNING: Very short time difference ({time_diff}s). Episodes might be missed.")
    
    return time_diff > 0

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

def podcast_biohack(id, trim, num_episodes=1, speed=1.5):
    obj = podcast("BioHack", id, trim, num_episodes, speed)
    return obj

def podcast_heroic(id, trim, num_episodes=1, speed=1.0):
    obj = podcast("Heroic", id, trim, num_episodes, speed)
    return obj

def podcast_music(id, trim, num_episodes):
    obj = podcast("Music", id, trim, num_episodes, 1.0)
    return obj

def process_feed_api(last_run_timestamp, podcast_name, info):
    feed_id = info["id"]
    category = info["category"]
    category_dir = os.path.join(usb_drive_base_dir, category)
    tmp_category_dir = os.path.join(TMP_DIR, category)
    num_episodes = info.get("num_episodes", 1)
    force_latest = info.get("force_latest", 0)

    # If force_latest is set, get episodes regardless of timestamp
    if force_latest > 0:
        feed = retry_api_call(index.episodesByFeedId, feed_id, since=0, max_results=max(force_latest, 50))  # Get more episodes than needed
        num_episodes = force_latest  # Override num_episodes with force_latest
    else:
        # Increase max_results to ensure we don't miss episodes due to API limits
        feed = retry_api_call(index.episodesByFeedId, feed_id, since=last_run_timestamp, max_results=max(num_episodes * 2, 50))

    # Check if feed response is valid
    if not feed or "items" not in feed or not feed["items"]:
        print(f"\tNo episodes found in feed response")
        return

    print(f"\tFound {len(feed['items'])} episodes in feed")

    for i in range(num_episodes):
        if i >= len(feed["items"]):
            print(f"\tNo more episodes available (requested {num_episodes}, got {len(feed['items'])})")
            break  # If there are no more episodes in the feed, exit the loop

        episode = feed["items"][i]
        episode_title = re.sub(r'[ /]', '_', episode["title"])
        episode_url = episode["enclosureUrl"]
        
        publication_timestamp = episode["datePublished"]
        publication_local_date_string = datetime.fromtimestamp(publication_timestamp, tz=pytz.UTC).astimezone(LOCAL_TIMEZONE).strftime('%Y-%m-%d %H:%M:%S')

        print(f"[{i+1}] Episode: {episode_title}")
        print(f"[{i+1}] Published: {publication_local_date_string}")

        # Skip timestamp check if force_latest is enabled
        if force_latest == 0 and publication_timestamp <= last_run_timestamp:
            print(f"\tSkipping episode published before last run ({publication_timestamp} <= {last_run_timestamp})")
            continue

        episode_file_path = os.path.join(category_dir, f"{podcast_name} - {episode_title}.mp3")
        tmp_episode_file_path = os.path.join(tmp_category_dir, f"{podcast_name} - {episode_title}.mp3")

        if os.path.exists(episode_file_path) or os.path.exists(tmp_episode_file_path):
            print(f"\tSkip. Already downloaded.")
        else:
            response = requests.get(episode_url)
            if response.status_code == 200:
                with open(tmp_episode_file_path, "wb") as f:
                    f.write(response.content)
                    
                    file_size = os.path.getsize(tmp_episode_file_path)
                    if file_size < 1024:
                        print(f"\tFile size [{file_size} bytes] too small: Skipping")
                        continue

                process_audio(info, tmp_episode_file_path)
            else:
                print(f"\tFailed to download episode: HTTP {response.status_code}")

def validate_timestamp_logic(last_run_timestamp, current_timestamp):
    """Validate and debug timestamp logic to help identify issues"""
    last_run_date = datetime.fromtimestamp(last_run_timestamp, tz=pytz.UTC).astimezone(LOCAL_TIMEZONE)
    current_date = datetime.fromtimestamp(current_timestamp, tz=pytz.UTC).astimezone(LOCAL_TIMEZONE)
    
    time_diff = current_timestamp - last_run_timestamp
    hours_diff = time_diff / 3600
    
    print(f"Timestamp Validation:")
    print(f"  Last run: {last_run_timestamp} ({last_run_date.strftime('%Y-%m-%d %H:%M:%S')})")
    print(f"  Current:  {current_timestamp} ({current_date.strftime('%Y-%m-%d %H:%M:%S')})")
    print(f"  Difference: {time_diff} seconds ({hours_diff:.2f} hours)")
    
    if time_diff < 0:
        print(f"  WARNING: Last run timestamp is in the future! This could cause issues.")
    elif time_diff == 0:
        print(f"  WARNING: Timestamps are identical. This could cause episodes to be missed.")
    elif time_diff < 60:
        print(f"  WARNING: Very short time difference ({time_diff}s). Episodes might be missed.")
    
    return time_diff > 0

def main(usb_drive_base_dir):
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
        #("Hot Money: Agent of Chaos",            lambda: podcast("Hot Money", 5464375, [20, 10], 1, 1.4, 1)),
        ("Heroic with Brian Johnson",            lambda: podcast_heroic(842919, [10, 0])),
        ("The Daily Dad",                        lambda: podcast_heroic(743919, [100, 30])),
        ("The Daily Stoic",                      lambda: podcast_heroic(404905, [65, 40])),
        ("Deep House Moscow",                    lambda: podcast_music(1105989, [0, 0], 2)),
        ("Heldeep Radio",                        lambda: podcast_music(838482,  [45, 0], 1)),
        ("Clublife Radio",                       lambda: podcast_music(41388,   [45, 0], 1)),
    ]
    
    # Build podcast_feeds dictionary with error handling
    podcast_feeds = {}
    for podcast_name, podcast_func in podcast_definitions:
        try:
            podcast_feeds[podcast_name] = podcast_func()
        except Exception as e:
            print(f"ERROR: Failed to initialize podcast '{podcast_name}': {e}")
            print(f"       Skipping this podcast and continuing with others...")
            continue

    last_run_timestamp = get_last_run_timestamp(usb_drive_base_dir)
    current_timestamp = int(time.time())
    
    # Validate timestamp logic before processing
    if not validate_timestamp_logic(last_run_timestamp, current_timestamp):
        print("WARNING: Timestamp validation failed. Consider resetting last_run_timestamp.")
    
    # Update timestamp BEFORE processing to ensure consistency
    # This prevents race conditions where episodes published during processing could be missed
    update_last_run_timestamp(usb_drive_base_dir)
    
    print(f"Processing podcasts since timestamp: {last_run_timestamp} ({datetime.fromtimestamp(last_run_timestamp, tz=pytz.UTC).astimezone(LOCAL_TIMEZONE).strftime('%Y-%m-%d %H:%M:%S')})")
    print(f"Current timestamp: {current_timestamp} ({datetime.fromtimestamp(current_timestamp, tz=pytz.UTC).astimezone(LOCAL_TIMEZONE).strftime('%Y-%m-%d %H:%M:%S')})")

    for podcast_name, info in podcast_feeds.items():
        try:
            feed_id = info["id"]
            feed_url = info["url"]
            category = info["category"]
            tmp_category_dir = os.path.join(TMP_DIR, category)
            
            print(f"\nPodcast: [{feed_id}] {podcast_name}")
            print(f"URL: {feed_url}")

            # Create the category directory if it doesn't exist
            os.makedirs(tmp_category_dir, exist_ok=True)

            #process_feed_direct(last_run_timestamp, podcast_name, info)
            process_feed_api(last_run_timestamp, podcast_name, info)
        except Exception as e:
            print(f"\nERROR: Failed to process podcast '{podcast_name}': {e}")
            print(f"       Continuing with other podcasts...")
            continue

    # Move all downloaded and processed episodes to the USB drive
    if os.path.exists(usb_drive_base_dir):
        print(f"\nProcessing Complete. Copying latest Podcasts to Headphones ...")
        for podcast_name, info in podcast_feeds.items():
            category = info["category"]
            num_episodes = info.get("num_episodes", 1)
            force_latest = info.get("force_latest", 0)
            
            # Use force_latest if set, otherwise use num_episodes
            episodes_to_keep = force_latest if force_latest > 0 else num_episodes
            
            tmp_category_dir = os.path.join(TMP_DIR, category)
            category_dir = os.path.join(usb_drive_base_dir, category)

            try:
                os.makedirs(category_dir, exist_ok=True)
            except (PermissionError, OSError) as e:
                print(f"\tWarning: Could not create directory {category_dir}: {e}")
                continue  # Skip this category if we can't create the directory

            try:
                for filename in os.listdir(tmp_category_dir):
                    src = os.path.join(tmp_category_dir, filename)
                    dst = os.path.join(category_dir, filename)
                    try:
                        shutil.move(src, dst)
                    except (PermissionError, OSError, FileNotFoundError) as e:
                        print(f"\tWarning: Could not move {filename} to USB drive: {e}")
                        # Continue with other files even if one fails

                delete_old_episodes(category_dir, podcast_name, episodes_to_keep)
            except (PermissionError, OSError) as e:
                print(f"\tWarning: Could not access files in {tmp_category_dir}: {e}")
        
        shutil.rmtree(TMP_DIR)

        print("Podcast episodes organized and downloaded. Big Day!")
    else:
        print("Error: USB drive not mounted. Please connect the USB drive and try again.")

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
