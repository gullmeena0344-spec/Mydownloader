import os
import re
import math
import asyncio
import shutil
import time
import logging
import subprocess
from pathlib import Path

from pyrogram import Client, filters
from pyrogram.types import Message
from run import GoFile

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")

DOWNLOAD_DIR = Path("output")
MAX_TG_SIZE = 2000 * 1024 * 1024
MIN_FREE_SPACE_MB = 300 # Lowered slightly to give you more room

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("BOT")

app = Client("gofile-userbot", api_id=API_ID, api_hash=API_HASH, session_string=SESSION_STRING)

# --- Enhanced Helpers ---

def get_free_space():
    return shutil.disk_usage(os.getcwd()).free

def format_bytes(size):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024: return f"{size:.2f} {unit}"
        size /= 1024

async def progress_bar(current, total, status_msg, action_name):
    try:
        now = time.time()
        if hasattr(status_msg, "last_update") and (now - status_msg.last_update) < 4:
            return
        status_msg.last_update = now
        perc = current * 100 / total
        await status_msg.edit(f"{action_name}...\nProgress: {perc:.1f}%\n{format_bytes(current)} / {format_bytes(total)}")
    except: pass

# --- Resilient FFmpeg Functions ---

def cmd(cmd_list):
    """Runs command and captures errors for debugging"""
    try:
        subprocess.run(cmd_list, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        log.error(f"FFmpeg Error: {e.stderr}")
        raise Exception(f"FFmpeg failed: {e.stderr[:100]}")

def get_duration(path):
    try:
        result = subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", path]
        )
        return float(result.decode().strip())
    except: return 0

def process_video_sync(src, dst_fixed):
    # Combined remux and faststart to save disk operations
    cmd(["ffmpeg", "-y", "-i", src, "-map", "0", "-c", "copy", "-movflags", "+faststart", dst_fixed])

def thumb_sync(video, out):
    dur = get_duration(video)
    seek = "00:00:01" if dur < 20 else "00:00:20"
    cmd(["ffmpeg", "-y", "-ss", seek, "-i", video, "-frames:v", "1", out])

def split_sync(path):
    file_size = os.path.getsize(path)
    if file_size <= MAX_TG_SIZE: return [path]
    
    base = Path(path)
    pattern = base.with_name(f"part_%03d.mp4")
    duration = get_duration(path)
    
    part_count = math.ceil(file_size / MAX_TG_SIZE)
    segment_time = int(duration / part_count) if duration > 0 else 1200

    cmd(["ffmpeg", "-i", path, "-map", "0", "-c", "copy", "-f", "segment", "-segment_time", str(segment_time), "-reset_timestamps", "1", str(pattern)])
    os.remove(path)
    return sorted(str(p) for p in base.parent.glob("part_*.mp4"))

# --- Main Logic ---

async def monitor_download(folder, status_msg):
    while True:
        await asyncio.sleep(4)
        total = sum(f.stat().st_size for f in Path(folder).rglob('*') if f.is_file())
        if total > 0:
            try: await status_msg.edit(f"⬇️ Downloading...\nSize: {format_bytes(total)}")
            except: pass

@app.on_message(filters.text & filters.outgoing)
async def handler(client, message: Message):
    m = re.search(r"https?://gofile\.io/d/\w+", message.text)
    if not m: return

    if get_free_space() < MIN_FREE_SPACE_MB * 1024 * 1024:
        return await message.reply("❌ Disk Full.")

    status = await message.reply("⬇️ Starting Download...")
    shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
    DOWNLOAD_DIR.mkdir(exist_ok=True)

    # Download
    dl_task = asyncio.to_thread(GoFile().execute, dir=str(DOWNLOAD_DIR), url=m.group(0), num_threads=1)
    mon_task = asyncio.create_task(monitor_download(DOWNLOAD_DIR, status))
    try: await dl_task
    finally: mon_task.cancel()

    files = [p for p in DOWNLOAD_DIR.rglob("*") if p.is_file() and not p.name.endswith(('.jpg', '.txt'))]
    if not files: return await status.edit("❌ No files.")

    for f in files:
        # Step 1: Sanitize Name (Remove spaces/special chars)
        clean_name = re.sub(r'[^a-zA-Z0-9.]', '_', f.name)
        new_path = f.parent / clean_name
        os.rename(f, new_path)
        
        try:
            # Step 2: Process Metadata
            await status.edit(f"⚙️ Optimizing: {clean_name}")
            fixed_path = new_path.with_suffix(".fixed.mp4")
            await asyncio.to_thread(process_video_sync, str(new_path), str(fixed_path))
            os.replace(fixed_path, new_path) # Replace original with optimized

            # Step 3: Thumbnail
            thumb_path = new_path.with_suffix(".jpg")
            await asyncio.to_thread(thumb_sync, str(new_path), str(thumb_path))

            # Step 4: Split and Upload
            parts = await asyncio.to_thread(split_sync, str(new_path))
            for i, p in enumerate(parts):
                await client.send_video(
                    "me", video=p,
                    thumb=str(thumb_path) if thumb_path.exists() else None,
                    caption=f"Part {i+1}/{len(parts)}",
                    progress=progress_bar,
                    progress_args=(status, f"⬆️ Uploading Part {i+1}")
                )
                os.remove(p)

            if thumb_path.exists(): os.remove(thumb_path)
        except Exception as e:
            await message.reply(f"❌ Failed {clean_name}: {str(e)}")

    shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
    await status.edit("✅ Success")

app.run()
