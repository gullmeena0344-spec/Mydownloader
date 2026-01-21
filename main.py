import os
import re
import math
import asyncio
import shutil
import time
import logging
import subprocess
import requests
from pathlib import Path

from pyrogram import Client, filters
from pyrogram.types import Message
from run import GoFile, Downloader, File 

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")

DOWNLOAD_DIR = Path("output")
MAX_TG_SIZE = 1500 * 1024 * 1024 # 1.5GB
MIN_FREE_SPACE_MB = 400

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("BOT")

app = Client(
    "gofile-userbot",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING
)

# ---------------- Utils ----------------

def get_free_space():
    return shutil.disk_usage(os.getcwd()).free

def format_bytes(size):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024:
            return f"{size:.2f} {unit}"
        size /= 1024

async def progress_bar(current, total, status_msg, action_name):
    try:
        now = time.time()
        if hasattr(status_msg, "last_update") and (now - status_msg.last_update) < 4:
            return
        status_msg.last_update = now
        perc = (current * 100 / total) if total > 0 else 0
        filled = int(20 * perc // 100)
        bar = "█" * filled + "░" * (20 - filled)
        await status_msg.edit(f"{action_name}\n[{bar}] {perc:.1f}%\n{format_bytes(current)} / {format_bytes(total)}")
    except: pass

def make_thumb(video_path):
    """Generates a reliable thumbnail at 10 seconds."""
    if not os.path.exists(video_path):
        return None
    thumb_path = video_path + ".jpg"
    # ss 10 skips the blue/black screen. -vf scale handles the size.
    cmd = [
        "ffmpeg", "-y", "-ss", "00:00:10", "-i", video_path,
        "-vframes", "1", "-vf", "scale=320:-1", thumb_path
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return thumb_path if os.path.exists(thumb_path) else None

# ---------------- Smart Download ----------------

async def smart_download(url, status):
    # Pixeldrain List logic
    if "pixeldrain.com/l/" in url:
        list_id = url.split("/")[-1]
        r = requests.get(f"https://pixeldrain.com/api/list/{list_id}").json()
        files = []
        for f in r.get("files", []):
            out = DOWNLOAD_DIR / f['name']
            await status.edit(f"Downloading {f['name']}...")
            subprocess.run(["aria2c", "-x", "8", "-s", "8", "-o", str(out), f"https://pixeldrain.com/api/file/{f['id']}"], check=True)
            files.append(out)
        return files

    # Direct MP4 or Generic
    await status.edit("Downloading link...")
    out = DOWNLOAD_DIR / "%(title)s.%(ext)s"
    subprocess.run(["yt-dlp", "-o", str(out), "--merge-output-format", "mp4", url], check=True)
    return list(DOWNLOAD_DIR.glob("*"))

# ---------------- Handler ----------------

@app.on_message(filters.text & (filters.outgoing | filters.private))
async def handler(client, message: Message):
    text = message.text.strip()
    if not text.startswith("http"): return
    if get_free_space() < MIN_FREE_SPACE_MB * 1024 * 1024:
        return await message.reply("Disk Full.")

    status = await message.reply("Analyzing...")
    shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    try:
        # 1. GOFILE LOGIC
        if "gofile.io/d/" in text:
            go = GoFile()
            m = re.search(r"gofile\.io/d/([\w\-]+)", text)
            files = go.get_files(dir=str(DOWNLOAD_DIR), content_id=m.group(1))
            
            for idx, file in enumerate(files, 1):
                file_name = os.path.basename(file.dest)
                upload_queue = asyncio.Queue()
                download_complete = asyncio.Event()
                loop = asyncio.get_running_loop()

                def on_part_ready(path, part_num, total_parts, size):
                    asyncio.run_coroutine_threadsafe(upload_queue.put((path, part_num, total_parts)), loop)

                async def download_task():
                    try:
                        # Downloader in run.py already does faststart!
                        await asyncio.to_thread(Downloader(token=go.token).download, file, 1, on_part_ready)
                    finally:
                        download_complete.set()

                async def upload_task():
                    while True:
                        get_task = asyncio.create_task(upload_queue.get())
                        wait_task = asyncio.create_task(download_complete.wait())
                        done, pending = await asyncio.wait([get_task, wait_task], return_when=asyncio.FIRST_COMPLETED)

                        if get_task in done:
                            path, part_num, total_parts = await get_task
                            if wait_task in pending: wait_task.cancel()
                            
                            await status.edit(f"[{idx}/{len(files)}] Uploading Part {part_num}...")
                            
                            # GENERATE THUMB FROM THE READY PART
                            thumb = make_thumb(str(path))
                            caption = file_name if total_parts == 1 else f"{file_name} (Part {part_num}/{total_parts})"
                            
                            await client.send_video(
                                "me", video=str(path), caption=caption, 
                                thumb=thumb, supports_streaming=True,
                                progress=progress_bar, 
                                progress_args=(status, f"[{idx}/{len(files)}] Uploading Part {part_num}")
                            )
                            
                            # CLEANUP
                            if os.path.exists(path): os.remove(path)
                            if thumb and os.path.exists(thumb): os.remove(thumb)
                        else:
                            if get_task in pending: get_task.cancel()
                            if upload_queue.empty(): break

                await asyncio.gather(download_task(), upload_task())

        # 2. GENERIC LOGIC (Direct MP4, Pixeldrain, etc)
        else:
            downloaded = await smart_download(text, status)
            for idx, f in enumerate(downloaded, 1):
                f_path = str(f)
                await status.edit(f"Processing video {idx}...")
                
                # Apply faststart for direct downloads
                fixed = f_path + ".fast.mp4"
                subprocess.run(["ffmpeg", "-y", "-i", f_path, "-c", "copy", "-movflags", "+faststart", fixed], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                
                final_path = fixed if os.path.exists(fixed) else f_path
                thumb = make_thumb(final_path)

                await client.send_video(
                    "me", video=final_path, caption=os.path.basename(f_path), 
                    thumb=thumb, supports_streaming=True,
                    progress=progress_bar, progress_args=(status, f"Uploading video {idx}")
                )
                
                # CLEANUP
                for x in (f_path, fixed, thumb):
                    if x and os.path.exists(x): os.remove(x)

        await status.edit("✅ All tasks complete.")

    except Exception as e:
        log.exception(e)
        await status.edit(f"❌ Error: {str(e)[:150]}")
    finally:
        shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)

app.run()
