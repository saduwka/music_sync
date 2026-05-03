import os
import json
import logging
import subprocess
import shutil
import re
import requests
from datetime import datetime

# --- CONFIGURATION ---
PLAYLIST_URL = "https://music.youtube.com/playlist?list=PLsLnUuEiM597QsUDH-L-waW5NE5sM3bFk"
IPOD_IP = "192.168.10.34"
IPOD_USER = "root"
IPOD_MUSIC_PATH = "/var/mobile/Media/Music_Sync"
BASE_DIR = os.path.expanduser("~/music_sync")
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")
READY_DIR = os.path.join(BASE_DIR, "ipod_ready")
STATE_FILE = os.path.join(BASE_DIR, "state.json")
LOG_FILE = os.path.join(BASE_DIR, "logs/sync.log")
ENV_FILE = os.path.join(BASE_DIR, ".env")

# --- LOAD ENV ---
def get_env(key):
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, 'r') as f:
            for line in f:
                if line.startswith(f"{key}="):
                    return line.split('=', 1)[1].strip()
    return None

BOT_TOKEN = get_env("TELEGRAM_BOT_TOKEN")
ADMIN_ID = get_env("TELEGRAM_ADMIN_ID")

# --- LOGGING ---
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()]
)

def send_telegram_message(message):
    if not BOT_TOKEN or not ADMIN_ID:
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": ADMIN_ID, "text": message, "parse_mode": "HTML"}
    try:
        logging.info(f"Sending Telegram notification...")
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
    except Exception as e:
        logging.error(f"Failed to send Telegram message: {e}")

def sanitize_filename(filename):
    s = filename.encode('ascii', 'ignore').decode('ascii')
    s = re.sub(r'[^\w\s-]', '', s).strip()
    s = re.sub(r'[-\s]+', '_', s)
    return s if s else "unknown_track"

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                return json.load(f)
        except:
            pass
    return {"downloaded": [], "last_sync": ""}

def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)

def get_playlist_tracks(url):
    logging.info(f"Fetching playlist tracks...")
    cmd = ["yt-dlp", "--flat-playlist", "--print", "%(id)s|%(title)s", url]
    result = subprocess.run(cmd, capture_output=True, text=True)
    tracks = []
    if result.returncode == 0:
        for line in result.stdout.strip().split('\n'):
            if '|' in line:
                v_id, title = line.split('|', 1)
                tracks.append({"video_id": v_id, "title": title})
    return tracks

def download_track(video_id, title):
    safe_title = sanitize_filename(title)
    logging.info(f"Processing: {safe_title}")
    
    if os.path.exists(DOWNLOAD_DIR):
        shutil.rmtree(DOWNLOAD_DIR)
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    
    output_template = os.path.join(DOWNLOAD_DIR, f"{safe_title}.%(ext)s")
    cmd = [
        "yt-dlp", "-x", "--audio-format", "m4a", "--audio-quality", "256K",
        "--output", output_template, "--add-metadata", "--embed-thumbnail",
        f"https://www.youtube.com/watch?v={video_id}"
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        files = [os.path.join(DOWNLOAD_DIR, f) for f in os.listdir(DOWNLOAD_DIR) if f.endswith(".m4a")]
        if files:
            source = files[0]
            target = os.path.join(READY_DIR, os.path.basename(source))
            os.makedirs(READY_DIR, exist_ok=True)
            shutil.move(source, target)
            return target
    return None

def push_to_ipod(local_path):
    filename = os.path.basename(local_path)
    logging.info(f"Pushing: {filename}")
    ssh_opts = ["-O", "-o", "HostKeyAlgorithms=+ssh-rsa", "-o", "PubkeyAcceptedAlgorithms=+ssh-rsa"]
    scp_cmd = ["scp"] + ssh_opts + [local_path, f"{IPOD_USER}@{IPOD_IP}:'{IPOD_MUSIC_PATH}/{filename}'"]
    result = subprocess.run(scp_cmd, capture_output=True, text=True)
    
    if result.returncode == 0:
        # Trigger Gremlin import on iPod and then delete the source file to save space
        logging.info(f"Triggering library import and cleanup for: {filename}")
        import_ssh_cmd = [
            "ssh", "-o", "HostKeyAlgorithms=+ssh-rsa", "-o", "PubkeyAcceptedAlgorithms=+ssh-rsa",
            f"{IPOD_USER}@{IPOD_IP}", 
            f"/usr/bin/killall Music 2>/dev/null; /usr/local/bin/gimport '{IPOD_MUSIC_PATH}/{filename}'; rm '{IPOD_MUSIC_PATH}/{filename}'"
        ]
        subprocess.run(import_ssh_cmd, capture_output=True, text=True)
        return True
    return False

def main():
    start_time = datetime.now()
    send_telegram_message("🎵 <b>Синхронизация iPod началась</b>\nПроверяю новые треки...")
    
    try:
        state = load_state()
        downloaded_ids = {t["video_id"] for t in state["downloaded"]}
        tracks = get_playlist_tracks(PLAYLIST_URL)
        new_tracks = [t for t in tracks if t["video_id"] not in downloaded_ids]
        
        if not new_tracks:
            logging.info("Everything is up to date.")
            send_telegram_message("✅ <b>Синхронизация окончена</b>\nНовых треков нет, всё актуально.")
            return

        logging.info(f"Syncing {len(new_tracks)} tracks...")
        send_telegram_message(f"📥 <b>Найдено новых треков: {len(new_tracks)}</b>\nНачинаю скачивание и перенос...")
        
        synced_count = 0
        for track in new_tracks:
            file_path = download_track(track["video_id"], track["title"])
            if file_path:
                if push_to_ipod(file_path):
                    logging.info(f"Synced: {track['title']}")
                    state["downloaded"].append({
                        "video_id": track["video_id"],
                        "title": track["title"],
                        "downloaded_at": datetime.now().isoformat()
                    })
                    save_state(state)
                    synced_count += 1
                    if os.path.exists(file_path):
                        os.remove(file_path)
        
        state["last_sync"] = datetime.now().isoformat()
        save_state(state)
        
        duration = datetime.now() - start_time
        send_telegram_message(
            f"✅ <b>Синхронизация успешно завершена!</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📦 Перенесено треков: <b>{synced_count}</b>\n"
            f"⏱ Время выполнения: <b>{str(duration).split('.')[0]}</b>"
        )
        
    except Exception as e:
        logging.error(f"Critical error: {e}")
        send_telegram_message(f"⚠️ <b>Ошибка при синхронизации!</b>\n<code>{str(e)}</code>")

if __name__ == "__main__":
    main()
