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
try:
    from run import GoFile, Downloader, File
except ImportError:
    print("CRITICAL: run.py not found! Make sure run.py is in the same folder.")
    exit(1)

# --- Configuration ---
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")

DOWNLOAD_DIR = Path("output")
MIN_FREE_SPACE_MB = 400

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("BOT")

# --- Startup Binary Check ---
FFMPEG_PATH = shutil.which("ffmpeg")
ARIA2_PATH = shutil.which("aria2c")

if not FFMPEG_PATH:
    log.error("FFmpeg was not found in the system PATH. Bot cannot process videos.")
    # You can force a path if you know where it is, e.g., FFMPEG_PATH = "/usr/bin/ffmpeg"
if not ARIA2_PATH:
    log.warning("aria2c was not found. Some downloads might be slower.")

app = Client("gofile-userbot", api_id=API_ID, api_hash=API_HASH, session_string=SESSION_STRING)

# ---------------- UTILS ----------------

def get_free_space():
    return shutil.disk_usage(os.getcwd()).free

def format_bytes(size):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024: return f"{size:.2f} {unit}"
        size /= 1024

async def progress_bar(current, total, status_msg, action_name):
    try:
        now = time.time()
        if hasattr(status_msg, "last_update") and (now - status_msg.last_update) < 3: return
        status_msg.last_update = now
        perc = (current * 100 / total) if total > 0 else 0
        filled = int(20 * perc // 100)
        bar = "█" * filled + "░" * (20 - filled)
        await status_msg.edit(f"**{action_name}**\n`[{bar}]` {perc:.1f}%\n{format_bytes(current)} / {format_bytes(total)}")
    except: pass

def process_video(src):
    """FFMPEG Faststart + 10s Thumbnail. Checks for FFMPEG_PATH to avoid Errno 2."""
    if not FFMPEG_PATH:
        log.error("FFmpeg path is missing. Skipping processing.")
        return src, None

    fixed = src + ".fixed.mp4"
    # ss 10 is the fix for the blue/black screen
    subprocess.run([FFMPEG_PATH, "-y", "-i", src, "-c", "copy", "-movflags", "+faststart", fixed], 
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    thumb = src + ".jpg"
    subprocess.run([FFMPEG_PATH, "-y", "-ss", "00:00:10", "-i", fixed if os.path.exists(fixed) else src, 
                    "-vframes", "1", "-vf", "scale=320:-1", thumb], 
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    return (fixed if os.path.exists(fixed) else src), (thumb if os.path.exists(thumb) else None)

# ---------------- 1. GOFILE HANDLER (EXACT LOGIC) ----------------

async def handle_gofile(client, status, content_id):
    go = GoFile()
    files = go.get_files(dir=str(DOWNLOAD_DIR), content_id=content_id)
    if not files: return await status.edit("GoFile: Folder empty or error.")

    for idx, file in enumerate(files, 1):
        file_name = os.path.basename(file.dest)
        upload_queue = asyncio.Queue()
        download_complete = asyncio.Event()
        loop = asyncio.get_running_loop()

        def on_part_ready(path, part_num, total_parts, size):
            asyncio.run_coroutine_threadsafe(upload_queue.put((path, part_num, total_parts)), loop)

        async def download_task():
            try:
                await asyncio.to_thread(Downloader(token=go.token).download, file, 1, on_part_ready)
            finally: download_complete.set()

        async def upload_task():
            while True:
                get_task = asyncio.create_task(upload_queue.get())
                wait_task = asyncio.create_task(download_complete.wait())
                done, pending = await asyncio.wait([get_task, wait_task], return_when=asyncio.FIRST_COMPLETED)

                if get_task in done:
                    path, part_num, total_parts = await get_task
                    if wait_task in pending: wait_task.cancel()
                    
                    # Video Fix + 10s Thumbnail
                    video, thumb = process_video(str(path))
                    caption = file_name if total_parts == 1 else f"{file_name} [Part {part_num}/{total_parts}]"
                    
                    await client.send_video("me", video=video, caption=caption, thumb=thumb, supports_streaming=True,
                                         progress=progress_bar, progress_args=(status, f"GoFile {idx}/{len(files)} Uploading P{part_num}"))
                    
                    # Cleanup immediately to save 4GB Disk space
                    for f in [path, video, thumb]:
                        if f and os.path.exists(str(f)): os.remove(str(f))
                else:
                    if get_task in pending: get_task.cancel()
                    if upload_queue.empty(): break

        await asyncio.gather(download_task(), upload_task())

# ---------------- 2. YT-DLP HANDLER (PIXELDRAIN/OTHERS) ----------------

async def handle_ytdlp(client, status, url):
    await status.edit("YT-DLP: Downloading file(s)...")
    out_tpl = str(DOWNLOAD_DIR / "%(title)s.%(ext)s")
    
    # Using yt-dlp to handle downloading
    cmd = ["yt-dlp", "-o", out_tpl, "--merge-output-format", "mp4", url]
    if ARIA2_PATH:
        cmd.extend(["--downloader", "aria2c", "--downloader-args", "aria2c:-x 8 -s 8"])

    await asyncio.to_thread(subprocess.run, cmd)
    
    files = list(DOWNLOAD_DIR.glob("*"))
    for idx, f in enumerate(files, 1):
        if f.suffix.lower() in [".mp4", ".mkv", ".mov", ".webm"]:
            video, thumb = process_video(str(f))
            await client.send_video("me", video=video, caption=f.name, thumb=thumb, supports_streaming=True,
                                 progress=progress_bar, progress_args=(status, f"YT-DLP {idx}/{len(files)} Uploading"))
            # Cleanup
            for x in [f, video, thumb]:
                if x and os.path.exists(str(x)): os.remove(str(x))

# ---------------- 3. DIRECT LINK HANDLER ----------------

async def handle_direct(client, status, url):
    await status.edit("Direct: Starting download...")
    # Clean filename from URL
    file_name = url.split("/")[-1].split("?")[0] or "video.mp4"
    dest = str(DOWNLOAD_DIR / file_name)
    
    # We use your run.py Downloader to handle direct links.
    # This automatically splits them into 1.5GB parts based on your run.py config.
    file_obj = File(url, dest)
    upload_queue = asyncio.Queue()
    download_complete = asyncio.Event()
    loop = asyncio.get_running_loop()

    def on_part_ready(path, part_num, total_parts, size):
        asyncio.run_coroutine_threadsafe(upload_queue.put((path, part_num, total_parts)), loop)

    async def dl_task():
        try:
            await asyncio.to_thread(Downloader(token="").download, file_obj, 1, on_part_ready)
        finally: download_complete.set()

    async def up_task():
        while True:
            get_t = asyncio.create_task(upload_queue.get())
            wait_t = asyncio.create_task(download_complete.wait())
            done, _ = await asyncio.wait([get_t, wait_t], return_when=asyncio.FIRST_COMPLETED)
            if get_t in done:
                path, p_num, t_parts = await get_t
                video, thumb = process_video(str(path))
                caption = file_name if t_parts == 1 else f"{file_name} [Part {p_num}/{t_parts}]"
                await client.send_video("me", video=video, caption=caption, thumb=thumb,
                                     supports_streaming=True, progress=progress_bar, progress_args=(status, "Direct Uploading"))
                # Cleanup
                for f in [path, video, thumb]:
                    if f and os.path.exists(str(f)): os.remove(str(f))
            else: break

    await asyncio.gather(dl_task(), up_task())

# ---------------- MASTER HANDLER ----------------

@app.on_message(filters.text & (filters.outgoing | filters.private))
async def master_handler(client, message: Message):
    url = message.text.strip()
    if not url.startswith("http"): return

    # Space Check
    if get_free_space() < MIN_FREE_SPACE_MB * 1024 * 1024:
        return await message.reply("Disk Full. Please clear space.")

    status = await message.reply("Analyzing Link...")
    shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    try:
        # Link routing
        if "gofile.io/d/" in url:
            content_id = url.split("/")[-1]
            await handle_gofile(client, status, content_id)
        
        elif any(x in url.lower() for x in ["youtube.com", "youtu.be", "pixeldrain.com", "instagram.com", "x.com", "twitter.com"]):
            await handle_ytdlp(client, status, url)
            
        else:
            # All other links treated as direct files
            await handle_direct(client, status, url)

        await status.edit("✅ All finished. Disk cleared.")
    except Exception as e:
        log.exception(e)
        await status.edit(f"❌ Error: {str(e)[:150]}")
    finally:
        shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)

if __name__ == "__main__":
    if not FFMPEG_PATH:
        print("ERROR: FFmpeg not found! Install it using 'sudo apt install ffmpeg'.")
    else:
        app.run()
