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

# Assuming run.py is in the same directory
from run import GoFile

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")

DOWNLOAD_DIR = Path("output")
MAX_TG_SIZE = 2000 * 1024 * 1024  # 2000MB limit
THUMB_TIME = 20
MIN_FREE_SPACE_MB = 500

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("BOT")

app = Client(
    "gofile-userbot",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING,
)

# --- Helpers ---

def get_free_space():
    return shutil.disk_usage(os.getcwd()).free

def format_bytes(size):
    power = 2**10
    n = 0
    power_labels = {0 : '', 1: 'K', 2: 'M', 3: 'G', 4: 'T'}
    while size > power:
        size /= power
        n += 1
    return f"{size:.2f} {power_labels[n]}B"

# --- Progress Bar for Upload ---
async def progress_bar(current, total, status_msg, action_name):
    try:
        now = time.time()
        # Update only every 5 seconds to avoid flooding API
        if hasattr(status_msg, "last_update") and (now - status_msg.last_update) < 5:
            return
        
        status_msg.last_update = now
        percentage = current * 100 / total
        await status_msg.edit(
            f"{action_name}...\n"
            f"Progress: {percentage:.1f}%\n"
            f"{format_bytes(current)} / {format_bytes(total)}"
        )
    except Exception:
        pass

# --- FFmpeg Wrappers (Blocking, to be run in threads) ---
def cmd(cmd_list):
    subprocess.run(cmd_list, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def get_duration(path):
    try:
        result = subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", path]
        )
        return float(result.decode().strip())
    except Exception:
        return 0

def faststart_sync(src, dst):
    cmd(["ffmpeg", "-y", "-i", src, "-map", "0", "-c", "copy", "-movflags", "+faststart", dst])

def remux_sync(src, dst):
    cmd(["ffmpeg", "-y", "-err_detect", "ignore_err", "-i", src, "-map", "0", "-c", "copy", dst])

def thumb_sync(video, out):
    cmd(["ffmpeg", "-y", "-ss", str(THUMB_TIME), "-i", video, "-frames:v", "1", out])

def split_sync(path):
    file_size = os.path.getsize(path)
    if file_size <= MAX_TG_SIZE:
        return [path]

    base = Path(path)
    pattern = base.with_name(f"{base.stem}_part%03d.mp4")
    
    duration = get_duration(path)
    if duration > 0:
        part_count = math.ceil(file_size / MAX_TG_SIZE)
        segment_time = int(duration / part_count)
    else:
        segment_time = 1800 # Fallback 30 mins

    cmd([
        "ffmpeg", "-i", path, "-map", "0", "-c", "copy", 
        "-f", "segment", 
        "-segment_time", str(segment_time), 
        "-reset_timestamps", "1", 
        str(pattern)
    ])
    
    if os.path.exists(path):
        os.remove(path)
        
    return sorted(str(p) for p in base.parent.glob(f"{base.stem}_part*.mp4"))

# --- Async Wrappers ---
async def monitor_download(folder, status_msg):
    """Monitors folder size while downloading to give visual feedback"""
    while True:
        await asyncio.sleep(5)
        total_size = sum(f.stat().st_size for f in Path(folder).rglob('*') if f.is_file())
        if total_size > 0:
            try:
                await status_msg.edit(f"⬇️ Downloading...\nDownloaded: {format_bytes(total_size)}")
            except:
                pass

# --- Main Handler ---

GOFILE_RE = re.compile(r"https?://gofile\.io/d/\w+", re.I)

@app.on_message(filters.text)
async def handler(client, message: Message):
    if not message.text:
        return
    m = GOFILE_RE.search(message.text)
    if not m:
        return

    url = m.group(0)
    
    # 1. Disk Check
    if get_free_space() < MIN_FREE_SPACE_MB * 1024 * 1024:
        return await message.reply("❌ Server disk full. Cleanup required.")

    status = await message.reply("⬇️ Initializing Download...")
    shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
    DOWNLOAD_DIR.mkdir(exist_ok=True)

    # 2. Download with Monitor
    download_task = asyncio.to_thread(GoFile().execute, dir=str(DOWNLOAD_DIR), url=url, num_threads=1)
    monitor_task = asyncio.create_task(monitor_download(DOWNLOAD_DIR, status))
    
    try:
        await download_task
    except Exception as e:
        monitor_task.cancel()
        return await status.edit(f"❌ Download Failed: {e}")
    
    monitor_task.cancel() # Stop monitoring

    files = [p for p in DOWNLOAD_DIR.rglob("*") if p.is_file()]
    if not files:
        return await status.edit("❌ Download finished but no files found.")

    await status.edit(f"⚙️ Processing {len(files)} file(s)...")

    for f in files:
        file_path = str(f)
        file_size = os.path.getsize(file_path)
        current_free = get_free_space()

        # 3. Space Check for Processing
        if (file_size * 1.5) > current_free:
             await status.edit(f"❌ OOM Limit Reached.\nFile: {format_bytes(file_size)}\nFree: {format_bytes(current_free)}")
             shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
             return

        try:
            fixed = f.with_suffix(".fixed.mp4")
            thumb_path = f.with_suffix(".jpg")
            
            # Run Heavy Tasks in Threads to prevent Bot Freeze
            await status.edit("⚙️ Fixing video metadata...")
            await asyncio.to_thread(remux_sync, file_path, str(fixed))
            await asyncio.to_thread(faststart_sync, str(fixed), file_path)
            if fixed.exists(): os.remove(fixed)

            await status.edit("📸 Generating thumbnail...")
            await asyncio.to_thread(thumb_sync, file_path, str(thumb_path))

            await status.edit("✂️ Splitting video (if needed)...")
            parts = await asyncio.to_thread(split_sync, file_path)
            
            if not parts:
                await status.edit("❌ Error: Splitting produced no files.")
                continue

            for i, p in enumerate(parts):
                await status.edit(f"⬆️ Uploading Part {i+1}/{len(parts)}")
                
                await client.send_video(
                    "me",
                    video=p,
                    thumb=str(thumb_path) if thumb_path.exists() else None,
                    supports_streaming=True,
                    caption=f"Part {i+1} of {len(parts)}",
                    progress=progress_bar,
                    progress_args=(status, f"⬆️ Uploading Part {i+1}")
                )
                if os.path.exists(p):
                    os.remove(p)

            if thumb_path.exists():
                os.remove(thumb_path)

        except Exception as e:
            log.error(f"Error processing {f}: {e}")
            await status.edit(f"❌ Error: {e}")
            shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
            return

    shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
    await status.edit("✅ All Done!")

app.run()
