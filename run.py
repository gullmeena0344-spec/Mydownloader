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

try:
    from run import GoFile, Downloader, File
except ImportError:
    GoFile = None
    Downloader = None
    print("Warning: run.py not found.")

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")

DOWNLOAD_DIR = Path("output")
MAX_CHUNK_SIZE = 1900 * 1024 * 1024
MIN_FREE_SPACE_MB = 500

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("BOT")

app = Client(
    "gofile-userbot",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING
)

# ---------------- UTILS ----------------

def format_bytes(size):
    for u in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024:
            return f"{size:.2f}{u}"
        size /= 1024

def get_progress_bar(percent, total=15):
    filled = int(total * percent // 100)
    return f"▰{'▰'*filled}{'▱'*(total-filled)}"

async def progress_bar(current, total, status, title):
    now = time.time()
    if not hasattr(status, "start"):
        status.start = now
        status.last = 0
    if now - status.last < 3:
        return
    status.last = now

    percent = current * 100 / total if total else 0
    elapsed = now - status.start
    speed = current / elapsed if elapsed else 0
    eta = (total - current) / speed if speed else 0

    await status.edit(
        f"<b>{title}</b>\n"
        f"<code>{get_progress_bar(percent)} {percent:.1f}%</code>\n"
        f"<b>Size:</b> {format_bytes(current)} / {format_bytes(total)}\n"
        f"<b>ETA:</b> {int(eta)}s"
    )

# ---------------- THUMBNAIL (FIXED) ----------------

def get_existing_or_generate_thumb(video_path):
    jpg = f"{video_path}.jpg"
    if os.path.exists(jpg) and os.path.getsize(jpg) > 1024:
        return jpg

    if not os.path.exists(video_path):
        return None

    thumb = f"{video_path}.thumb.jpg"
    try:
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", str(video_path),
                "-ss", "00:00:03",
                "-vframes", "1",
                "-q:v", "2",
                thumb
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        if os.path.exists(thumb) and os.path.getsize(thumb) > 1024:
            return thumb
    except:
        pass
    return None

# ---------------- GOFILE ----------------

async def handle_gofile_logic(client, status, url):
    go = GoFile()
    cid = re.search(r"gofile\.io/d/([\w\-]+)", url)
    if not cid:
        await status.edit("❌ Invalid GoFile link")
        return

    files = go.get_files(str(DOWNLOAD_DIR), content_id=cid.group(1))
    await status.edit(f"Found {len(files)} file(s)")

    upload_queue = asyncio.Queue()
    done = asyncio.Event()
    loop = asyncio.get_running_loop()

    def on_part_ready(path, part, total, size):
        asyncio.run_coroutine_threadsafe(upload_queue.put((path, part, total)), loop)

    async def download():
        await asyncio.to_thread(
            Downloader(token=go.token).download,
            files[0], 1, on_part_ready
        )
        done.set()

    async def upload():
        while True:
            if upload_queue.empty() and done.is_set():
                break
            path, part, total = await upload_queue.get()
            thumb = await asyncio.to_thread(get_existing_or_generate_thumb, path)

            await client.send_video(
                "me",
                path,
                caption=os.path.basename(path),
                thumb=thumb,
                supports_streaming=True,
                progress=progress_bar,
                progress_args=(status, f"UP {part}/{total}")
            )

            if thumb and os.path.exists(thumb):
                os.remove(thumb)
            if os.path.exists(path):
                os.remove(path)

    await asyncio.gather(download(), upload())
    await status.edit("✅ GoFile done")

# ---------------- GENERIC ----------------

async def download_direct(url, out, status):
    cmd = ["yt-dlp", "-f", "bv*+ba/b", "-o", str(out), url]
    proc = await asyncio.create_subprocess_exec(*cmd)
    await proc.wait()
    return out.exists()

async def handle_generic_logic(client, status, url):
    name = "video.mp4"
    path = DOWNLOAD_DIR / name

    ok = await download_direct(url, path, status)
    if not ok:
        await status.edit("❌ Download failed")
        return

    size = path.stat().st_size
    thumb = await asyncio.to_thread(get_existing_or_generate_thumb, path)

    await client.send_video(
        "me",
        str(path),
        caption=name,
        thumb=thumb,
        supports_streaming=True,
        progress=progress_bar,
        progress_args=(status, "Uploading")
    )

    if thumb and os.path.exists(thumb):
        os.remove(thumb)
    os.remove(path)

# ---------------- MAIN ----------------

@app.on_message(filters.text & (filters.private | filters.outgoing))
async def handler(client, message: Message):
    text = message.text.strip()
    if not text.startswith("http"):
        return

    status = await message.reply("Processing...")
    shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
    DOWNLOAD_DIR.mkdir(exist_ok=True)

    try:
        if "gofile.io" in text:
            await handle_gofile_logic(client, status, text)
        else:
            await handle_generic_logic(client, status, text)
    finally:
        shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)

app.run()
