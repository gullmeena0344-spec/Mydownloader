import os
import re
import asyncio
import shutil
import time
import logging
import subprocess
import math
from pathlib import Path
from pyrogram import Client, filters
from pyrogram.types import Message

# Config
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")

DOWNLOAD_DIR = Path("downloads")
TG_SPLIT_SIZE = 1500 * 1024 * 1024  # 1.5 GB
MIN_FREE_SPACE = 300 * 1024 * 1024  # 300 MB

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("BOT")

app = Client("gofile-userbot", api_id=API_ID, api_hash=API_HASH, session_string=SESSION_STRING)

# ---------------- PROGRESS HELPERS ----------------

def format_bytes(size):
    for u in ["B", "KB", "MB", "GB"]:
        if size < 1024: return f"{size:.2f} {u}"
        size /= 1024

async def update_progress(current, total, status, title, last_time):
    now = time.time()
    if now - last_time[0] < 3: return last_time[0]
    last_time[0] = now
    
    p = (current * 100 / total) if total > 0 else 0
    bar = f"[{'█'*int(p//10)}{'░'*(10-int(p//10))}]"
    try:
        await status.edit(f"**{title}**\n`{bar}` {p:.1f}%\n{format_bytes(current)} / {format_bytes(total)}")
    except: pass
    return now

def ytdlp_hook(d, status_msg, loop, last_time):
    if d['status'] == 'downloading':
        total = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
        current = d.get('downloaded_bytes', 0)
        if total > 0:
            asyncio.run_coroutine_threadsafe(
                update_progress(current, total, status_msg, "Downloading Link...", last_time), 
                loop
            )

# ---------------- VIDEO PROCESSING ----------------

def get_duration(file):
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", file]
    return float(subprocess.check_output(cmd))

def make_thumbnail(video):
    thumb = video + ".jpg"
    # Seek to 10s, scale 320px, high quality to avoid blue/black screen
    cmd = ["ffmpeg", "-y", "-ss", "00:00:10", "-i", video, "-vframes", "1", "-q:v", "2", "-vf", "scale=320:-1", thumb]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return thumb if os.path.exists(thumb) else None

# ---------------- DOWNLOAD & UPLOAD ----------------

async def process_and_upload(file_path, status):
    file_size = os.path.getsize(file_path)
    file_name = os.path.basename(file_path)
    last_time = [0]

    # IF SMALL: Faststart & Upload
    if file_size <= TG_SPLIT_SIZE:
        fixed = file_path + ".stream.mp4"
        subprocess.run(["ffmpeg", "-y", "-i", file_path, "-c", "copy", "-movflags", "+faststart", fixed], stdout=subprocess.DEVNULL)
        thumb = make_thumbnail(fixed)
        await app.send_video("me", fixed, caption=file_name, thumb=thumb, supports_streaming=True, 
                             progress=update_progress, progress_args=(status, "Uploading", last_time))
        for f in [file_path, fixed, thumb]: 
            if f and os.path.exists(f): os.remove(f)
        return

    # IF LARGE: Split & Sequential Upload
    duration = get_duration(file_path)
    num_parts = math.ceil(file_size / TG_SPLIT_SIZE)
    part_dur = duration / num_parts

    for i in range(num_parts):
        part_name = f"{os.path.splitext(file_name)[0]}_part_{i+1}.mp4"
        part_path = str(DOWNLOAD_DIR / part_name)
        
        await status.edit(f"Processing Part {i+1}/{num_parts}...")
        subprocess.run([
            "ffmpeg", "-y", "-ss", str(i * part_dur), "-t", str(part_dur),
            "-i", file_path, "-c", "copy", "-movflags", "+faststart", part_path
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        thumb = make_thumbnail(part_path)
        await app.send_video("me", part_path, caption=f"{file_name} (Part {i+1})", thumb=thumb, 
                             supports_streaming=True, progress=update_progress, progress_args=(status, f"Uploading {i+1}/{num_parts}", last_time))
        
        # Cleanup part immediately to save disk for next part
        os.remove(part_path)
        if thumb and os.path.exists(thumb): os.remove(thumb)

    os.remove(file_path)

@app.on_message(filters.text & (filters.outgoing | filters.private))
async def main_handler(client, message: Message):
    url = message.text.strip()
    if not url.startswith("http"): return

    status = await message.reply("Analysing...")
    shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    try:
        # 1. Download Stage
        last_time = [0]
        loop = asyncio.get_event_loop()
        
        # Unified yt-dlp options (Works for GoFile, Pixeldrain, and others)
        ydl_opts = {
            'outtmpl': str(DOWNLOAD_DIR / '%(title)s.%(ext)s'),
            'progress_hooks': [lambda d: ytdlp_hook(d, status, loop, last_time)],
            'merge_output_format': 'mp4',
            'noplaylist': True,
        }
        
        # Special handling for Gofile (ensure we get direct mp4 if possible)
        if "gofile.io" in url:
            ydl_opts['format'] = 'bestvideo+bestaudio/best'

        import yt_dlp
        await status.edit("Connecting to server...")
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            await asyncio.to_thread(ydl.download, [url])

        # 2. Process Stage
        files = list(DOWNLOAD_DIR.glob("*"))
        if not files:
            return await status.edit("No files found.")

        for f in files:
            if f.is_file():
                await process_and_upload(str(f), status)

        await status.edit("✅ All tasks complete.")

    except Exception as e:
        log.exception(e)
        await status.edit(f"❌ Error: {str(e)[:200]}")
    finally:
        shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)

app.run()
