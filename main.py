import os
import asyncio
import shutil
import time
import logging
import subprocess
import re
import aiohttp
from urllib.parse import unquote
from pathlib import Path
from pyrogram import Client, filters
from pyrogram.types import Message
# Assuming run.py is in the same folder and works correctly
from run import GoFile, Downloader

# --- Config ---
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
SESSION_STRING = os.getenv("SESSION_STRING", "")

BASE_OUTPUT_DIR = Path("output")
MIN_FREE_SPACE_MB = 400

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
log = logging.getLogger("BOT")

app = Client("gofile-userbot", api_id=API_ID, api_hash=API_HASH, session_string=SESSION_STRING)

# ---------------- UTILS ----------------

def get_free_space():
    return shutil.disk_usage(os.getcwd()).free

def format_bytes(size):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024: return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} TB"

def sanitize_filename(name):
    """Removes illegal characters from filenames."""
    # Remove null bytes, illegal chars for Windows/Linux
    name = re.sub(r'[<>:"/\\|?*]', '', name)
    name = name.strip()
    return name if name else "video.mp4"

def get_progress_bar(percent, total=20):
    filled = int(total * percent // 100)
    bar = "█" * filled + "░" * (total - filled)
    return f"[{bar}] {percent:.1f}%"

async def progress_bar(current, total, status_msg, action_name, is_percentage=False):
    try:
        now = time.time()
        # Initialize last_update
        if not hasattr(status_msg, "last_update"):
            status_msg.last_update = 0
        
        # 2.5s delay to prevent FloodWait
        if (now - status_msg.last_update) < 2.5: 
            return

        status_msg.last_update = now
        
        if is_percentage:
            perc = current
            bar = get_progress_bar(perc)
            await status_msg.edit(f"{action_name}\n{bar}")
        else:
            perc = (current * 100 / total) if total > 0 else 0
            bar = get_progress_bar(perc)
            await status_msg.edit(f"{action_name}\n{bar}\n{format_bytes(current)} / {format_bytes(total)}")
    except Exception: 
        pass

# ---------------- MEDIA PROCESSING (Threaded) ----------------

def get_video_duration(src):
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", 
             "-of", "default=noprint_wrappers=1:nokey=1", src],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        return float(result.stdout.strip())
    except: return 0.0

def _make_thumb_sync(src):
    thumb_path = src + ".jpg"
    duration = get_video_duration(src)
    
    # Logic: 
    # > 5s: take at 20%
    # 0-5s: take at 1s
    # 0s (Metadata fail): take at 0s
    if duration > 5:
        seek_time = str(int(duration * 0.20))
    elif duration > 0:
        seek_time = "00:00:01"
    else:
        seek_time = "00:00:00"

    try:
        # scale=320:-1 is CRITICAL for Telegram thumbnails
        cmd = ["ffmpeg", "-y", "-ss", seek_time, "-i", src, "-vframes", "1", 
               "-vf", "scale=320:-1", "-q:v", "5", thumb_path]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
            return thumb_path
    except: pass
    return None

def _faststart_sync(src):
    dst = src + ".fast.mp4"
    try:
        # -movflags +faststart optimizes MP4 for streaming
        subprocess.run(["ffmpeg", "-y", "-i", src, "-c", "copy", "-movflags", "+faststart", dst],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        if os.path.exists(dst) and os.path.getsize(dst) > 0:
            if os.path.exists(src): os.remove(src)
            return dst
    except: pass
    return src

async def make_thumb(src):
    return await asyncio.to_thread(_make_thumb_sync, src)

async def faststart_mp4(src):
    return await asyncio.to_thread(_faststart_sync, src)

# ---------------- DOWNLOAD HELPERS ----------------

# 1. AIOHTTP Downloader
async def download_file(url, dest_path, status_msg):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }
    timeout = aiohttp.ClientTimeout(total=None)
    connector = aiohttp.TCPConnector(ssl=False) 

    try:
        async with aiohttp.ClientSession(connector=connector, timeout=timeout, headers=headers) as session:
            async with session.get(url) as response:
                if response.status != 200:
                    log.error(f"HTTP Error {response.status}")
                    return False
                
                total_size = int(response.headers.get("Content-Length", 0))
                downloaded = 0
                
                with open(dest_path, "wb") as f:
                    async for chunk in response.content.iter_chunked(1024 * 1024):
                        if not chunk: break
                        f.write(chunk)
                        downloaded += len(chunk)
                        await progress_bar(downloaded, total_size, status_msg, "⬇️ Downloading...")
                return True
    except Exception as e:
        log.error(f"Download Exception: {e}")
        return False

# 2. YT-DLP Downloader
async def ytdlp_download(url, dest_folder, status_msg):
    out_tpl = str(dest_folder / "%(title)s.%(ext)s")
    
    # Add User-Agent to prevent blocks
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    
    process = await asyncio.create_subprocess_exec(
        "yt-dlp", "-o", out_tpl, "--merge-output-format", "mp4", "--newline", 
        "--user-agent", ua, url,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )

    while True:
        line = await process.stdout.readline()
        if not line: break
        
        line_decoded = line.decode().strip()
        match = re.search(r"(\d+\.\d+)%", line_decoded)
        if match:
            try:
                percent = float(match.group(1))
                await progress_bar(percent, 100, status_msg, "⬇️ YT-DLP Downloading", is_percentage=True)
            except: pass

    await process.wait()
    return process.returncode == 0

# ---------------- HANDLERS ----------------

async def handle_ytdlp(client, status, url, download_path):
    success = await ytdlp_download(url, download_path, status)
    if not success:
        return await status.edit("❌ YT-DLP Download Failed.")
    
    files_found = list(download_path.glob("*"))
    if not files_found: return await status.edit("❌ No files found.")

    for f in files_found:
        if f.suffix.lower() in [".mp4", ".mkv", ".mov", ".webm"]:
            fixed = await faststart_mp4(str(f))
            thumb = await make_thumb(fixed)
            try:
                await client.send_video("me", video=fixed, caption=f.name, thumb=thumb, supports_streaming=True,
                                     progress=progress_bar, progress_args=(status, "⬆️ Uploading Video"))
            except Exception as e: log.error(f"Upload fail: {e}")
            
            for x in [fixed, thumb]:
                if x and os.path.exists(str(x)): os.remove(str(x))

async def handle_direct(client, status, url, download_path, custom_filename=None):
    if custom_filename:
        file_name = sanitize_filename(custom_filename)
    else:
        raw_name = url.split("/")[-1].split("?")[0]
        file_name = sanitize_filename(unquote(raw_name) if raw_name else "video.mp4")
    
    dest_path = download_path / file_name
    
    success = await download_file(url, dest_path, status)
    if not success:
        return await status.edit(f"❌ Failed to download.")

    fixed = await faststart_mp4(str(dest_path))
    thumb = await make_thumb(fixed)
    
    try:
        await client.send_video("me", video=fixed, caption=file_name, thumb=thumb, supports_streaming=True,
                                progress=progress_bar, progress_args=(status, "⬆️ Uploading File"))
    except Exception as e:
        log.error(f"Upload Error: {e}")
        await status.edit(f"❌ Upload Error: {e}")

    for f in [fixed, thumb]:
        if f and os.path.exists(str(f)): os.remove(str(f))

async def handle_pixeldrain(client, status, url, download_path):
    await status.edit("Pixeldrain: Processing...")
    match = re.search(r"pixeldrain\.com/u/([a-zA-Z0-9]+)", url)
    if not match: return await status.edit("❌ Invalid Pixeldrain URL.")
    
    file_id = match.group(1)
    direct_url = f"https://pixeldrain.com/api/file/{file_id}"
    await handle_direct(client, status, direct_url, download_path, custom_filename=f"{file_id}.mp4")

async def handle_gofile(client, status, content_id, download_path):
    go = GoFile()
    files = go.get_files(dir=str(download_path), content_id=content_id)
    if not files: return await status.edit("GoFile: No files found.")

    for idx, file in enumerate(files, 1):
        file_name = os.path.basename(file.dest)
        await status.edit(f"⬇️ GoFile: Downloading {file_name}...")
        
        await asyncio.to_thread(Downloader(token=go.token).download, file, 1, lambda *a: None)
        
        fixed = await faststart_mp4(file.dest)
        thumb = await make_thumb(fixed)
        
        try:
            await client.send_video("me", video=fixed, caption=file_name, thumb=thumb, supports_streaming=True,
                                    progress=progress_bar, progress_args=(status, "⬆️ Uploading Video"))
        except Exception as e:
            log.error(f"GoFile Upload Error: {e}")
            
        if os.path.exists(fixed): os.remove(fixed)
        if thumb and os.path.exists(thumb): os.remove(thumb)

# ---------------- MAIN ROUTER ----------------

@app.on_message(filters.text & (filters.outgoing | filters.private))
async def main_handler(client, message: Message):
    text = message.text.strip()
    if not text.startswith("http"): return
    
    if get_free_space() < MIN_FREE_SPACE_MB * 1024 * 1024:
        return await message.reply("Disk Full.")

    status = await message.reply("⏳ Connecting...")
    
    unique_dir = BASE_OUTPUT_DIR / str(message.id)
    shutil.rmtree(unique_dir, ignore_errors=True)
    unique_dir.mkdir(parents=True, exist_ok=True)

    try:
        if "gofile.io/d/" in text:
            content_id = text.split("/")[-1]
            await handle_gofile(client, status, content_id, unique_dir)
        elif "pixeldrain.com" in text:
            await handle_pixeldrain(client, status, text, unique_dir)
        elif any(x in text for x in ["youtube", "twitter", "instagram", "youtu.be", "tiktok"]):
            await handle_ytdlp(client, status, text, unique_dir)
        else:
            await handle_direct(client, status, text, unique_dir)
            
        await status.edit("✅ Job Finished.")
    except Exception as e:
        log.exception(e)
        await status.edit(f"❌ Error: {str(e)[:150]}")
    finally:
        try:
            shutil.rmtree(unique_dir, ignore_errors=True)
        except: pass

if __name__ == "__main__":
    if not API_ID:
        print("❌ Error: API_ID missing.")
    else:
        app.run()
