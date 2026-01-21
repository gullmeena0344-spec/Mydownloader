import os
import re
import math
import asyncio
import shutil
import time
import logging
import subprocess
from pathlib import Path
from queue import Queue
from threading import Thread

from pyrogram import Client, filters
from pyrogram.types import Message
from run import GoFile, Downloader, File

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")

DOWNLOAD_DIR = Path("output")
MAX_TG_SIZE = 1990 * 1024 * 1024
MIN_FREE_SPACE_MB = 500
MAX_PART_SIZE = 2 * 1024 * 1024 * 1024  # 2GB

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("BOT")

app = Client(
    "gofile-userbot",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING
)

# ------------------- Helpers -------------------

def get_free_space():
    return shutil.disk_usage(os.getcwd()).free

def format_bytes(size):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024:
            return f"{size:.2f} {unit}"
        size /= 1024

def get_progress_bar(percent, total=20):
    filled = int(total * percent // 100)
    bar = "█" * filled + "░" * (total - filled)
    return f"[{bar}] {percent:.1f}%"

async def progress_bar(current, total, status_msg, action_name):
    try:
        now = time.time()
        if hasattr(status_msg, "last_update") and (now - status_msg.last_update) < 2:
            return
        status_msg.last_update = now
        perc = current * 100 / total
        bar = get_progress_bar(perc)
        await status_msg.edit(f"{action_name}\n{bar}\n{format_bytes(current)} / {format_bytes(total)}")
    except Exception as e:
        log.debug(f"Progress update error: {e}")

# ✅ Thumbnail + faststart
def faststart_mp4(src):
    dst = src + ".fast.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-i", src, "-c", "copy", "-movflags", "+faststart", dst],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    return dst

def make_thumb(src):
    thumb = src + ".jpg"
    subprocess.run(
        ["ffmpeg", "-y", "-i", src, "-ss", "00:00:01", "-vframes", "1", thumb],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    return thumb if os.path.exists(thumb) else None

def split_video(src):
    """Split video into 2GB parts"""
    size = os.path.getsize(src)
    if size <= MAX_PART_SIZE:
        return [src]
    base = str(src).replace(".mp4", "")
    subprocess.run([
        "ffmpeg", "-y", "-i", src,
        "-c", "copy",
        "-map", "0",
        "-f", "segment",
        "-segment_bytes", str(MAX_PART_SIZE),
        f"{base}_part_%03d.mp4"
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return sorted([str(p) for p in Path(".").glob(base + "_part_*.mp4")])

async def yt_dlp_download(url, output_dir):
    """Download YouTube/Pixeldrain/HLS/m3u8/direct links using yt-dlp"""
    os.makedirs(output_dir, exist_ok=True)
    cmd = [
        "yt-dlp",
        "-f", "bv*+ba/b",
        "--merge-output-format", "mp4",
        "--remux-video", "mp4",
        "--external-downloader", "aria2c",
        "--external-downloader-args", "-x16 -k1M",
        "-o", str(Path(output_dir)/"%(title)s.%(ext)s"),
        url
    ]
    proc = await asyncio.create_subprocess_exec(*cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL
    )
    await proc.communicate()
    return sorted(list(Path(output_dir).glob("*.mp4")))

# ------------------- Main Handler -------------------

@app.on_message(filters.text & (filters.outgoing | filters.private))
async def handler(client, message: Message):
    text = message.text.strip()

    if get_free_space() < MIN_FREE_SPACE_MB * 1024 * 1024:
        return await message.reply("Disk Full.")

    status = await message.reply("Starting Download...")
    shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    try:
        files = []

        # ---------------- GoFile ----------------
        m = re.search(r"gofile\.io/d/([\w\-]+)", text)
        if m:
            go = GoFile()
            files = go.get_files(dir=str(DOWNLOAD_DIR), content_id=m.group(1))

        # ---------------- Direct/yt-dlp/Pixeldrain/HLS ----------------
        elif any(re.search(p, text) for p in [
            r"\.mp4$", r"\.mov$", r"\.m3u8$",
            r"^https?:\/\/(www\.)?youtube\.com",
            r"^https?:\/\/(www\.)?youtu\.be",
            r"^https?:\/\/(www\.)?pixeldrain\.com",
            r"saint2\.cr/embed"
        ]):
            files = await yt_dlp_download(text, str(DOWNLOAD_DIR))
        else:
            await status.edit("No supported link found.")
            return

        if not files:
            await status.edit("No files found.")
            return

        await status.edit(f"Found {len(files)} file(s). Processing...")

        # ---------------- Upload ----------------
        for idx, file in enumerate(files, 1):
            file_name = os.path.basename(file)
            await status.edit(f"[{idx}/{len(files)}] Processing: {file_name[:30]}...")

            fixed = faststart_mp4(str(file))
            thumb = make_thumb(fixed)

            parts = split_video(fixed)
            for i, part in enumerate(parts, 1):
                caption = file_name if len(parts)==1 else f"{file_name} [Part {i}/{len(parts)}]"
                await client.send_video(
                    "me",
                    video=part,
                    caption=caption,
                    supports_streaming=True,
                    thumb=thumb,
                    progress=progress_bar,
                    progress_args=(status, f"[{idx}/{len(files)}] Uploading Part {i}/{len(parts)}")
                )
                try: os.remove(part)
                except: pass

            for f in (file, fixed, thumb):
                try:
                    if f and os.path.exists(f): os.remove(f)
                except: pass

        shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
        await status.edit("All done!")

    except Exception as e:
        log.error(f"Handler error: {e}")
        await status.edit(f"Error: {str(e)[:100]}")
        shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)

# ------------------- Run -------------------
app.run()
