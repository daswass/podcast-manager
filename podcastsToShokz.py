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

    if os.path.exists(usb_drive_base_dir):
        with open(timestamp_file_1, "w") as file1:
            file1.write(str(int(time.time())))
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

def podcast(id, trim, num_episodes, speed):
    result = index.podcastByFeedId(id)
    podcast_url = result["feed"]["url"]

    return {
        "id": id,
        "url": podcast_url,
        "category": "Finance",
        "num_episodes": num_episodes,
        "skip_first": trim[0],
        "skip_last": trim[1],
        "playback_speed": speed
    }

def podcast_finance(id, trim, num_episodes=1, speed=1.4):
    obj = podcast(id, trim, num_episodes, speed)
    obj["category"] = "Finance"
    return obj

def podcast_biohack(id, trim, num_episodes=1, speed=1.5):
    obj = podcast(id, trim, num_episodes, speed)
    obj["category"] = "BioHack"
    return obj

def podcast_heroic(id, trim, num_episodes=1, speed=1.0):
    obj = podcast(id, trim, num_episodes, speed)
    obj["category"] = "Heroic"
    return obj

def podcast_music(id, trim, num_episodes):
    obj = podcast(id, trim, num_episodes, 1.0)
    obj["category"] = "Music"
    return obj

def process_feed_api(last_run_timestamp, podcast_name, info):
    feed_id = info["id"]
    category = info["category"]
    category_dir = os.path.join(usb_drive_base_dir, category)
    tmp_category_dir = os.path.join(TMP_DIR, category)
    num_episodes = info.get("num_episodes", 1)

    feed = index.episodesByFeedId(feed_id, since=last_run_timestamp)

    for i in range(num_episodes):
        if i >= len(feed["items"]):
            print("\t No new episodes in feed")
            #print(feed)
            break  # If there are no more episodes in the feed, exit the loop

        episode = feed["items"][i]
        episode_title = re.sub(r'[ /]', '_', episode["title"])
        episode_url = episode["enclosureUrl"]
        
        publication_timestamp = episode["datePublished"]
        publication_local_date_string = datetime.fromtimestamp(publication_timestamp, tz=pytz.UTC).astimezone(LOCAL_TIMEZONE).strftime('%Y-%m-%d %H:%M:%S')

        print(f"[{i+1}] Episode: {episode_title}")
        print(f"[{i+1}] Published: {publication_local_date_string}")

        if publication_timestamp <= last_run_timestamp:
            print(f"\tSkipping episode published before last run")
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

def main(usb_drive_base_dir):
    # Dictionary mapping podcast names to their RSS feed URLs, categories, number of episodes to download, skip_first, skip_last, playback_speed
    # See: https://podcastindex.org/
    podcast_feeds = {
        "FT News Briefing":             podcast_finance(73509,  [30, 80]),
        "Goldman Sachs The Markets":    podcast_finance(6321730,[10, 25]),
        "Unhedged":                     podcast_finance(6416251,[32, 45]),
        "The Daily":                    podcast_finance(743229, [32, 30]),
        "WSJ Whats News":               podcast_finance(491326, [20, 10]),
        
        "Ben Greenfield Life":                  podcast_biohack(310787, [315 ,30]),
        "The Human Upgrade with Dave Asprey":   podcast_biohack(1031629, [150, 0]),
        "Huberman Lab":                         podcast_biohack(1365758, [120, 20]),

        "Heroic with Brian Johnson":    podcast_heroic(842919, [10, 0]),
        "The Daily Dad":                podcast_heroic(743919, [90, 30]),
        "The Daily Stoic":              podcast_heroic(404905, [34, 40]),

        "Deep House Moscow":            podcast_music(1105989, [0, 0], 2),
        "Heldeep Radio":                podcast_music(7021728, [45, 0], 1),
        "Clublife Radio":               podcast_music(41388, [45, 0], 1),
    }

    last_run_timestamp = get_last_run_timestamp(usb_drive_base_dir)
    #last_run_date_string = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last_run_timestamp))

    for podcast_name, info in podcast_feeds.items():
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

    # Move all downloaded and processed episodes to the USB drive
    if os.path.exists(usb_drive_base_dir):
        print(f"\nProcessing Complete. Copying latest Podcasts to OpenSwim ...")
        for podcast_name, info in podcast_feeds.items():
            category = info["category"]
            num_episodes = info.get("num_episodes", 1)
            tmp_category_dir = os.path.join(TMP_DIR, category)
            category_dir = os.path.join(usb_drive_base_dir, category)

            os.makedirs(category_dir, exist_ok=True)

            for filename in os.listdir(tmp_category_dir):
                src = os.path.join(tmp_category_dir, filename)
                dst = os.path.join(category_dir, filename)
                shutil.move(src, dst)

            delete_old_episodes(category_dir, podcast_name, num_episodes)
        
        shutil.rmtree(TMP_DIR)

        update_last_run_timestamp(usb_drive_base_dir)
        print("Podcast episodes organized and downloaded. Big Day!")
    else:
        print("Error: USB drive not mounted. Please connect the USB drive and try again.")

def wait_for_usb_drive(usb_base_drive_dir):
    while not os.path.exists(usb_base_drive_dir):
        print("\rWaiting for USB drive at {}{} ".format(usb_base_drive_dir, "." * ((int(time.time()) % 3) + 1)), end="", flush=True)
        time.sleep(1)
        print("\r" + " " * 50, end="", flush=True)  # Clear the line
    
    print("\rUSB drive is available at {}".format(usb_base_drive_dir))
    main(usb_base_drive_dir)

if __name__ == "__main__":
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
