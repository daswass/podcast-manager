import os
import time
from datetime import datetime
import pytz
import podcastindex
from dotenv import load_dotenv

load_dotenv()
config = {
    "api_key": os.getenv('API_KEY'),
    "api_secret": os.getenv('API_SECRET')
}
index = podcastindex.init(config)

LOCAL_TIMEZONE = pytz.timezone('America/New_York')

def debug_timeline():
    feed_id = 5464375  # Hot Money: Agent of Chaos
    
    # Get the last run timestamp
    usb_drive_base_dir = "/Volumes/OpenSwim/"
    timestamp_file = os.path.join(usb_drive_base_dir, "podcast_downloader_last_run.txt")
    
    if os.path.exists(timestamp_file):
        with open(timestamp_file, "r") as file:
            last_run_timestamp = int(file.read().strip())
    else:
        last_run_timestamp = 0
    
    last_run_date = datetime.fromtimestamp(last_run_timestamp, tz=pytz.UTC).astimezone(LOCAL_TIMEZONE)
    current_time = datetime.now(LOCAL_TIMEZONE)
    
    print(f"Current time: {current_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Last run time: {last_run_date.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Time since last run: {current_time - last_run_date}")
    print("=" * 60)
    
    # Get podcast episodes
    try:
        feed = index.episodesByFeedId(feed_id, since=0)  # Get all episodes
        print(f"Total episodes in feed: {len(feed['items'])}")
        print("\nRecent episodes:")
        
        for i, episode in enumerate(feed['items'][:5]):  # Show last 5 episodes
            pub_timestamp = episode["datePublished"]
            pub_date = datetime.fromtimestamp(pub_timestamp, tz=pytz.UTC).astimezone(LOCAL_TIMEZONE)
            is_new = pub_timestamp > last_run_timestamp
            
            status = "NEW" if is_new else "OLD"
            print(f"  {i+1}. {episode['title']}")
            print(f"     Published: {pub_date.strftime('%Y-%m-%d %H:%M:%S')} ({status})")
            print(f"     Timestamp: {pub_timestamp}")
            
            if is_new:
                print(f"     ✓ This episode would be downloaded")
            else:
                print(f"     ✗ This episode was published before last run")
            print()
            
    except Exception as e:
        print(f"Error getting episodes: {e}")

def test_force_latest():
    """Test the force_latest functionality"""
    feed_id = 5464375  # Hot Money: Agent of Chaos
    
    print("\n" + "=" * 60)
    print("Testing force_latest=2 functionality:")
    print("=" * 60)
    
    try:
        # Get all episodes (simulating force_latest behavior)
        feed = index.episodesByFeedId(feed_id, since=0)
        print(f"Total episodes available: {len(feed['items'])}")
        
        # Show what would be downloaded with force_latest=2
        print("\nWith force_latest=2, these episodes would be downloaded:")
        for i, episode in enumerate(feed['items'][:2]):  # First 2 episodes
            pub_timestamp = episode["datePublished"]
            pub_date = datetime.fromtimestamp(pub_timestamp, tz=pytz.UTC).astimezone(LOCAL_TIMEZONE)
            print(f"  {i+1}. {episode['title']}")
            print(f"     Published: {pub_date.strftime('%Y-%m-%d %H:%M:%S')}")
            print(f"     Duration: {episode.get('duration', 'Unknown')} seconds")
            print()
            
    except Exception as e:
        print(f"Error testing force_latest: {e}")

if __name__ == "__main__":
    debug_timeline()
    test_force_latest() 