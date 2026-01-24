import os
import re
import math
import asyncio
import shutil
import time
import logging
import subprocess
import requests
import json
from pathlib import Path
from urllib.parse import unquote, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

from pyrogram import Client, filters, errors
from pyrogram.types import Message

# --- Config ---
API_ID = int(os.getenv("API_ID", "12345")) # Replace with yours if not using env
API_HASH = os.getenv("API_HASH", "your_hash")
SESSION_STRING = os.getenv("SESSION_STRING", "your_session")

DOWNLOAD_DIR = Path("output")
MAX_CHUNK_SIZE = 1500 * 1024 * 1024 

# Standard Browser Header
GENERIC_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
    "Referer": "https://google.com/",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"
}

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("BOT")

app = Client(
    "gofile-userbot",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING
)

# ---------------- GOFILE MULTI-THREADED DOWNLOADER ----------------

class GoFileDownloader:
    def __init__(self, token, progress_callback=None):
        self.token = token
        self.progress_lock = Lock()
        self.downloaded_bytes = 0
        self.total_size = 0
        self.progress_callback = progress_callback  # Function to update Telegram

    def _get_total_size(self, link):
        headers = {
            "Cookie": f"accountToken={self.token}",
            "User-Agent": GENERIC_HEADERS["User-Agent"]
        }
        r = requests.head(link, headers=headers)
        r.raise_for_status()
        return int(r.headers["Content-Length"]), r.headers.get("Accept-Ranges", "none") == "bytes"

    def _download_range(self, link, start, end, temp_file, i):
        existing_size = os.path.getsize(temp_file) if os.path.exists(temp_file) else 0
        range_start = start + existing_size
        if range_start > end:
            return i
            
        headers = {
            "Cookie": f"accountToken={self.token}",
            "Range": f"bytes={range_start}-{end}",
            "User-Agent": GENERIC_HEADERS["User-Agent"]
        }
        
        with requests.get(link, headers=headers, stream=True) as r:
            r.raise_for_status()
            with open(temp_file, "ab") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        with self.progress_lock:
                            self.downloaded_bytes += len(chunk)
                            # We don't await here because this is sync code running in a thread
                            # The callback will handle the throttling
                            if self.progress_callback:
                                self.progress_callback(self.downloaded_bytes, self.total_size)
        return i

    def _merge_temp_files(self, temp_dir, dest, num_threads):
        with open(dest, "wb") as outfile:
            for i in range(num_threads):
                temp_file = os.path.join(temp_dir, f"part_{i}")
                if os.path.exists(temp_file):
                    with open(temp_file, "rb") as f:
                        shutil.copyfileobj(f, outfile)
                    os.remove(temp_file)
        shutil.rmtree(temp_dir, ignore_errors=True)

    def download(self, link, dest, num_threads=4):
        temp_dir = dest + "_parts"
        self.total_size, is_support_range = self._get_total_size(link)
        
        # Reset tracker
        self.downloaded_bytes = 0

        # If file exists and size matches, skip
        if os.path.exists(dest):
            if os.path.getsize(dest) == self.total_size:
                return

        if num_threads == 1 or not is_support_range:
            # Single thread fallback
            headers = {"Cookie": f"accountToken={self.token}", "User-Agent": GENERIC_HEADERS["User-Agent"]}
            with requests.get(link, headers=headers, stream=True) as r:
                r.raise_for_status()
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                            self.downloaded_bytes += len(chunk)
                            if self.progress_callback:
                                self.progress_callback(self.downloaded_bytes, self.total_size)
        else:
            # Multi-thread logic
            os.makedirs(temp_dir, exist_ok=True)
            part_size = math.ceil(self.total_size / num_threads)
            
            # Calculate already downloaded bytes (for resume)
            for i in range(num_threads):
                t_file = os.path.join(temp_dir, f"part_{i}")
                if os.path.exists(t_file):
                    self.downloaded_bytes += os.path.getsize(t_file)

            with ThreadPoolExecutor(max_workers=num_threads) as executor:
                futures = []
                for i in range(num_threads):
                    start = i * part_size
                    end = min(start + part_size - 1, self.total_size - 1)
                    temp_file = os.path.join(temp_dir, f"part_{i}")
                    futures.append(
                        executor.submit(self._download_range, link, start, end, temp_file, i)
                    )
                
                for future in as_completed(futures):
                    future.result()  # Raise exceptions if any

            self._merge_temp_files(temp_dir, dest, num_threads)

# ---------------- UTILS ----------------

def format_bytes(size):
    for u in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024:
            return f"{size:.2f} {u}"
        size /= 1024

def get_progress_bar(percent, total=15):
    filled = int(total * percent // 100)
    return f"▰{'▰'*filled}{'▱'*(total-filled-1)}▱"

class ProgressTracker:
    def __init__(self, status_msg, title):
        self.status = status_msg
        self.title = title
        self.last_update = 0
        self.start_time = time.time()
        self.loop = asyncio.get_event_loop()

    def update(self, current, total):
        now = time.time()
        if now - self.last_update < 3 and current != total:
            return
        
        self.last_update = now
        p = (current * 100 / total) if total > 0 else 0
        diff = now - self.start_time
        speed = current / diff if diff > 0 else 0
        eta = (total - current) / speed if speed > 0 else 0
        eta_str = time.strftime("%H:%M:%S", time.gmtime(eta))

        text = (
            f"<b>{self.title}</b>\n"
            f"<code>{get_progress_bar(p)} {p:.1f}%</code>\n"
            f"<b>Size:</b> {format_bytes(current)} / {format_bytes(total)}\n"
            f"<b>Speed:</b> {format_bytes(speed)}/s\n"
            f"<b>ETA:</b> {eta_str}"
        )
        
        # Fire and forget update to avoid blocking download thread
        asyncio.run_coroutine_threadsafe(self.status.edit(text), self.loop)

async def progress_bar(current, total, status, title):
    # Wrapper for Pyrogram upload progress
    tracker = ProgressTracker(status, title)
    tracker.update(current, total)

# ---------------- CORE LOGIC ----------------

def get_extension_from_url(url):
    path = urlparse(url).path
    ext = os.path.splitext(path)[1]
    return ext if ext else ".mp4"

def get_real_filename(url, default_name, headers=None):
    try:
        r = requests.head(url, allow_redirects=True, timeout=5, headers=headers)
        if "Content-Disposition" in r.headers:
            fname = re.findall("filename=(.+)", r.headers["Content-Disposition"])
            if fname:
                return unquote(fname[0].strip().replace('"', '').replace("'", ""))
    except: pass
    return default_name

def generate_thumbnail(video_path):
    thumb_path = f"{video_path}.jpg"
    timestamps = ["00:00:15", "00:00:02"]
    for ss in timestamps:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(video_path), "-ss", ss, "-vframes", "1", thumb_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        if os.path.exists(thumb_path):
            return thumb_path
    return None

async def upload_media_safe(client, chat_id, path, caption, thumb, status, progress_text):
    if not os.path.exists(path): return
    
    ext = os.path.splitext(path)[1].lower()
    is_video = ext in ['.mp4', '.mkv', '.avi', '.mov', '.webm', '.flv']
    
    # Progress wrapper for pyrogram
    async def upload_progress(current, total):
        await progress_bar(current, total, status, progress_text)

    try:
        if is_video:
            await client.send_video(
                chat_id,
                path,
                caption=caption,
                thumb=thumb,
                supports_streaming=True,
                progress=upload_progress
            )
        else:
            await client.send_document(
                chat_id,
                path,
                caption=caption,
                thumb=thumb,
                progress=upload_progress
            )
    except errors.FloodWait as e:
        log.warning(f"FloodWait: {e.value}s")
        await asyncio.sleep(e.value + 5)
        await upload_media_safe(client, chat_id, path, caption, thumb, status, progress_text)
    except Exception as e:
        log.error(f"Upload Error: {e}")

# ---------------- SCRAPERS ----------------

def get_gofile_token():
    try:
        r = requests.post("https://api.gofile.io/accounts", headers=GENERIC_HEADERS).json()
        if r.get("status") == "ok":
            return r["data"]["token"]
    except: pass
    return None

def extract_gofile_folder(content_id, token):
    files = []
    try:
        url = f"https://api.gofile.io/contents/{content_id}?wt={token}"
        r = requests.get(url, headers=GENERIC_HEADERS).json()
        
        if r.get("status") == "ok":
            contents = r["data"].get("children", {})
            for child_id, child_data in contents.items():
                if child_data["type"] == "folder":
                    files.extend(extract_gofile_folder(child_data["id"], token))
                else:
                    files.append({
                        "url": child_data["link"], # This is the download link
                        "name": child_data["name"],
                        "size": child_data["size"],
                        "token": token,
                        "type": "gofile"
                    })
    except Exception as e:
        log.error(f"GoFile Error: {e}")
    return files

async def resolve_url(url, token=None):
    items = []
    
    if "gofile.io/d/" in url:
        log.info("Detected GoFile")
        content_id = url.split("/d/")[-1]
        guest_token = get_gofile_token()
        if guest_token:
            found = extract_gofile_folder(content_id, guest_token)
            items.extend(found)
    
    # ... (Keep other resolvers like Bunkr/Cyberfile from original code if needed) ...
    # Simplified here to focus on GoFile fix
    
    if not items:
        # Fallback for generic direct links
        items.append({"url": url, "name": "download", "size": 0, "type": "generic"})

    return items

# ---------------- MAIN HANDLER ----------------

@app.on_message(filters.text & (filters.outgoing | filters.private))
async def handler(client, message: Message):
    text = message.text.strip()
    if not text.startswith("http"): return

    status = await message.reply("Analysing link...")
    shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    url_parts = text.split()
    main_url = url_parts[0]
    
    try:
        file_list = await resolve_url(main_url)
        
        if not file_list:
            await status.edit("❌ No files found.")
            return

        total_files = len(file_list)
        await status.edit(f"Found {total_files} file(s). Starting...")

        for index, item in enumerate(file_list, 1):
            url = item["url"]
            name = re.sub(r'[^\w\-. ]', '', item.get("name", "video"))
            size = item.get("size", 0)
            
            # Ensure name has extension
            if not os.path.splitext(name)[1]:
                name += ".mp4"
            
            out_path = DOWNLOAD_DIR / name

            await status.edit(f"<b>File {index}/{total_files}</b>\nDownloading: {name}...")

            # --- GOFILE DOWNLOADER LOGIC ---
            if item.get("type") == "gofile":
                # Initialize tracker
                tracker = ProgressTracker(status, f"DL: {name}")
                
                # Initialize the specific GoFile downloader
                downloader = GoFileDownloader(token=item["token"], progress_callback=tracker.update)
                
                # Run sync download in async thread
                try:
                    await asyncio.to_thread(downloader.download, url, str(out_path), num_threads=8)
                except Exception as e:
                    await status.edit(f"Download Failed: {e}")
                    continue

            # --- GENERIC DOWNLOADER (FALLBACK) ---
            else:
                cmd = ["curl", "-k", "-L", "-s", "-o", str(out_path), url]
                proc = await asyncio.create_subprocess_exec(*cmd)
                await proc.wait()

            # --- UPLOAD LOGIC ---
            if out_path.exists() and out_path.stat().st_size > 0:
                is_video = out_path.suffix.lower() in ['.mp4', '.mkv', '.avi', '.mov']
                thumb = await asyncio.to_thread(generate_thumbnail, out_path) if is_video else None
                
                await upload_media_safe(client, "me", str(out_path), name, thumb, status, "Uploading")
                
                os.remove(out_path)
                if thumb and os.path.exists(thumb): os.remove(thumb)
            else:
                await status.edit(f"Failed to download: {name}")

        await status.edit("✅ All tasks completed!")

    except Exception as e:
        log.exception(e)
        await status.edit(f"Error: {str(e)}")
    
    shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)

if __name__ == "__main__":
    app.run()
