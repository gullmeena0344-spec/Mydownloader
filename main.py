import os
import re
import asyncio
import shutil
import time
import logging
import subprocess
import requests
from pathlib import Path

from pyrogram import Client, filters
from pyrogram.types import Message
from run import GoFile, Downloader # Assuming these are from your custom 'run' module

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")

DOWNLOAD_DIR = Path("output")
MAX_TG_SIZE = 1500 * 1024 * 1024 # 1.5 GB
MIN_FREE_SPACE_MB = 300 # Safety buffer for the OS

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("BOT")

app = Client("gofile-userbot", api_id=API_ID, api_hash=API_HASH, session_string=SESSION_STRING)

# ---------------- Utils ----------------

def get_free_space():
    return shutil.disk_usage(os.getcwd()).free

def format_bytes(size):
    for u in ["B", "KB", "MB", "GB"]:
        if size < 1024: return f"{size:.2f} {u}"
        size /= 1024

async def progress_bar(current, total, status, title):
    now = time.time()
    if hasattr(status, "last") and now - status.last < 2: return
    status.last = now
    p = (current * 100 / total) if total > 0 else 0
    try:
        await status.edit(f"**{title}**\n`[{'█'*int(p//5)}{'░'*(20-int(p//5))}]` {p:.1f}%\n{format_bytes(current)} / {format_bytes(total)}")
    except: pass

# ---------------- Disk-Safe Processing ----------------

def process_video_safely(src):
    """Handles faststart and thumbnails without overflowing a small disk."""
    file_size = os.path.getsize(src)
    free_space = get_free_space()
    
    # If file is larger than 80% of free space, skip faststart to avoid Disk Full error
    if file_size > (free_space * 0.8):
        log.warning(f"Disk too low ({format_bytes(free_space)}). Skipping faststart for {src}")
        return src

    dst = src + ".fast.mp4"
    try:
        subprocess.run([
            "ffmpeg", "-y", "-i", src, "-c", "copy", "-movflags", "+faststart", dst
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        if os.path.exists(dst):
            os.remove(src) # Delete original immediately
            return dst
    except Exception as e:
        log.error(f"Faststart failed: {e}")
    return src

def make_thumb(src):
    thumb = src + ".jpg"
    # Skip 5 seconds to avoid blue/black frames, scale for TG
    subprocess.run([
        "ffmpeg", "-y", "-ss", "00:00:05", "-i", src, 
        "-vframes", "1", "-vf", "scale=320:-1", thumb
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return thumb if os.path.exists(thumb) else None

def split_large_file(file_path):
    """Splits file and deletes original immediately to save disk."""
    if os.path.getsize(file_path) <= MAX_TG_SIZE:
        return [file_path]

    log.info(f"Splitting {file_path}...")
    base = os.path.splitext(file_path)[0]
    ext = os.path.splitext(file_path)[1]
    output_pattern = f"{base}_part%03d{ext}"
    
    # Split into 1.4GB segments
    subprocess.run([
        "ffmpeg", "-i", file_path, "-c", "copy", "-map", "0", 
        "-segment_time", "01:00:00", "-f", "segment", "-reset_timestamps", "1", 
        output_pattern
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    os.remove(file_path) # DELETE ORIGINAL LARGE FILE IMMEDIATELY
    return sorted(list(DOWNLOAD_DIR.glob(f"{os.path.basename(base)}_part*{ext}")))

# ---------------- Core Logic ----------------

async def handle_upload(client, file_path, status, current_idx, total_files):
    """Processes, splits, and uploads a single file."""
    # 1. Split if necessary
    parts = split_large_file(str(file_path))
    
    for p_idx, p in enumerate(parts, 1):
        p_name = os.path.basename(p)
        display_name = p_name if len(parts) == 1 else f"{p_name} ({p_idx}/{len(parts)})"
        
        # 2. Process (Faststart)
        processed_path = process_video_safely(p)
        thumb = make_thumb(processed_path)
        
        # 3. Upload
        await status.edit(f"Uploading file {current_idx}/{total_files}...")
        try:
            await client.send_video(
                "me",
                processed_path,
                caption=display_name,
                supports_streaming=True,
                thumb=thumb,
                progress=progress_bar,
                progress_args=(status, f"Uploading {current_idx}/{total_files}")
            )
        except Exception as e:
            log.error(f"Upload failed: {e}")
        
        # 4. Immediate Cleanup of parts
        if os.path.exists(processed_path): os.remove(processed_path)
        if thumb and os.path.exists(thumb): os.remove(thumb)

@app.on_message(filters.text & (filters.outgoing | filters.private))
async def handler(client, message: Message):
    url = message.text.strip()
    if not url.startswith("http"): return

    status = await message.reply("Checking disk and link...")
    shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    try:
        if "gofile.io/d/" in url:
            m = re.search(r"gofile\.io/d/([\w\-]+)", url)
            go = GoFile()
            files = go.get_files(dir=str(DOWNLOAD_DIR), content_id=m.group(1))
            
            # SEQUENTIAL: Download 1 -> Upload 1 -> Delete 1 -> Move to 2
            for idx, f in enumerate(files, 1):
                await status.edit(f"Downloading item {idx}/{len(files)}...")
                await asyncio.to_thread(Downloader(token=go.token).download, f)
                await handle_upload(client, Path(f.dest), status, idx, len(files))
        
        else:
            # Generic Links (Pixeldrain/yt-dlp)
            await status.edit("Downloading with yt-dlp...")
            cmd = [
                "yt-dlp", "--no-playlist", "--merge-output-format", "mp4",
                "-o", str(DOWNLOAD_DIR / "%(title)s.%(ext)s"), url
            ]
            await asyncio.to_thread(subprocess.run, cmd)
            
            dl_files = list(DOWNLOAD_DIR.glob("*"))
            for idx, f in enumerate(dl_files, 1):
                await handle_upload(client, f, status, idx, len(dl_files))

        await status.edit("✅ All tasks complete. Disk cleared.")

    except Exception as e:
        log.exception(e)
        await status.edit(f"❌ Error: {str(e)[:100]}")
    finally:
        shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)

app.run()
