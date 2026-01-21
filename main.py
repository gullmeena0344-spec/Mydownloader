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
from run import GoFile, Downloader, File

# --- Config ---
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")

DOWNLOAD_DIR = Path("output")
MAX_TG_SIZE = 1500 * 1024 * 1024  # 1.5GB for 4GB disk safety
MIN_FREE_SPACE_MB = 400

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("BOT")

app = Client("gofile-userbot", api_id=API_ID, api_hash=API_HASH, session_string=SESSION_STRING)

# ---------------- UTILS ----------------

def get_free_space():
    return shutil.disk_usage(os.getcwd()).free

def format_bytes(size):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024: return f"{size:.2f} {unit}"
        size /= 1024

def get_progress_bar(percent, total=20):
    filled = int(total * percent // 100)
    bar = "█" * filled + "░" * (total - filled)
    return f"[{bar}] {percent:.1f}%"

async def progress_bar(current, total, status_msg, action_name):
    try:
        now = time.time()
        if hasattr(status_msg, "last_update") and (now - status_msg.last_update) < 2: return
        status_msg.last_update = now
        perc = (current * 100 / total) if total > 0 else 0
        bar = get_progress_bar(perc)
        await status_msg.edit(f"{action_name}\n{bar}\n{format_bytes(current)} / {format_bytes(total)}")
    except: pass

def faststart_mp4(src):
    dst = src + ".fast.mp4"
    subprocess.run(["ffmpeg", "-y", "-i", src, "-c", "copy", "-movflags", "+faststart", dst],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if os.path.exists(dst):
        if os.path.exists(src): os.remove(src) # Immediate removal to save disk
        return dst
    return src

def make_thumb(src):
    # Small delay to ensure file isn't locked on small disks
    time.sleep(0.5)
    thumb = src + ".jpg"
    # Back to your original working 00:00:01
    subprocess.run(["ffmpeg", "-y", "-i", src, "-ss", "00:00:01", "-vframes", "1", thumb],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return thumb if os.path.exists(thumb) else None

# ---------------- 1. GOFILE HANDLER (EXACT ORIGINAL LOGIC) ----------------

async def handle_gofile(client, status, content_id):
    go = GoFile()
    files = go.get_files(dir=str(DOWNLOAD_DIR), content_id=content_id)
    if not files: return await status.edit("GoFile: No files found.")

    for idx, file in enumerate(files, 1):
        file_name = os.path.basename(file.dest)
        upload_queue = asyncio.Queue()
        download_complete = asyncio.Event()
        loop = asyncio.get_running_loop()

        def on_part_ready(path, p_num, t_parts, size):
            asyncio.run_coroutine_threadsafe(upload_queue.put((path, p_num, t_parts)), loop)

        async def download_task():
            try: await asyncio.to_thread(Downloader(token=go.token).download, file, 1, on_part_ready)
            finally: download_complete.set()

        async def upload_task():
            while True:
                get_task = asyncio.create_task(upload_queue.get())
                wait_task = asyncio.create_task(download_complete.wait())
                done, pending = await asyncio.wait([get_task, wait_task], return_when=asyncio.FIRST_COMPLETED)

                if get_task in done:
                    path, part_num, total_parts = await get_task
                    if wait_task in pending: wait_task.cancel()
                    
                    # Process exactly like your GoFile-only version
                    fixed = faststart_mp4(str(path))
                    thumb = make_thumb(fixed)
                    
                    caption = file_name if total_parts == 1 else f"{file_name} [Part {part_num}/{total_parts}]"
                    await client.send_video("me", video=fixed, caption=caption, thumb=thumb, supports_streaming=True,
                                         progress=progress_bar, progress_args=(status, f"Uploading Part {part_num}"))
                    
                    for f in [fixed, thumb]:
                        if f and os.path.exists(f): os.remove(f)
                else:
                    if get_task in pending: get_task.cancel()
                    if upload_queue.empty(): break

        await asyncio.gather(download_task(), upload_task())

# ---------------- 2. YT-DLP / PIXELDRAIN HANDLER ----------------

async def handle_ytdlp(client, status, url):
    await status.edit("YT-DLP: Downloading...")
    out_tpl = str(DOWNLOAD_DIR / "%(title)s.%(ext)s")
    
    # Stable Pixeldrain flags (Limited connections to prevent errors)
    cmd = ["yt-dlp", "-o", out_tpl, "--merge-output-format", "mp4", 
           "--downloader", "aria2c", "--downloader-args", "aria2c:--max-connection-per-server=2 --split=2", url]
    
    await asyncio.to_thread(subprocess.run, cmd)
    
    for f in DOWNLOAD_DIR.glob("*"):
        if f.suffix in [".mp4", ".mkv", ".mov"]:
            fixed = faststart_mp4(str(f))
            thumb = make_thumb(fixed)
            await client.send_video("me", video=fixed, caption=f.name, thumb=thumb, supports_streaming=True,
                                 progress=progress_bar, progress_args=(status, "Uploading Video"))
            for x in [fixed, thumb]:
                if x and os.path.exists(str(x)): os.remove(str(x))

# ---------------- 3. DIRECT LINK HANDLER ----------------

async def handle_direct(client, status, url):
    await status.edit("Direct Link: Downloading...")
    file_name = url.split("/")[-1].split("?")[0] or "video.mp4"
    file_obj = File(url, str(DOWNLOAD_DIR / file_name))
    
    upload_queue = asyncio.Queue()
    download_complete = asyncio.Event()
    loop = asyncio.get_running_loop()

    def on_part_ready(p, p_num, t_parts, sz):
        asyncio.run_coroutine_threadsafe(upload_queue.put((p, p_num, t_parts)), loop)

    async def dl_task():
        try: await asyncio.to_thread(Downloader(token="").download, file_obj, 1, on_part_ready)
        finally: download_complete.set()

    async def up_task():
        while True:
            get_t = asyncio.create_task(upload_queue.get())
            wait_t = asyncio.create_task(download_complete.wait())
            done, _ = await asyncio.wait([get_t, wait_t], return_when=asyncio.FIRST_COMPLETED)
            if get_t in done:
                path, p_num, t_parts = await get_t
                fixed = faststart_mp4(str(path))
                thumb = make_thumb(fixed)
                await client.send_video("me", video=fixed, caption=f"{file_name} P{p_num}", thumb=thumb,
                                     supports_streaming=True, progress=progress_bar, progress_args=(status, "Uploading Part"))
                for f in [fixed, thumb]:
                    if f and os.path.exists(str(f)): os.remove(str(f))
            else: break

    await asyncio.gather(dl_task(), up_task())

# ---------------- MAIN ROUTER ----------------

@app.on_message(filters.text & (filters.outgoing | filters.private))
async def main_handler(client, message: Message):
    text = message.text.strip()
    if not text.startswith("http"): return
    if get_free_space() < MIN_FREE_SPACE_MB * 1024 * 1024:
        return await message.reply("Disk Full.")

    status = await message.reply("Analyzing Link...")
    shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    try:
        # Strictly separated routing
        if "gofile.io/d/" in text:
            content_id = text.split("/")[-1]
            await handle_gofile(client, status, content_id)
        elif any(x in text for x in ["pixeldrain", "youtube", "twitter", "instagram"]):
            await handle_ytdlp(client, status, text)
        else:
            await handle_direct(client, status, text)
            
        await status.edit("✅ All finished.")
    except Exception as e:
        log.exception(e)
        await status.edit(f"❌ Error: {str(e)[:150]}")
    finally:
        shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)

app.run()
