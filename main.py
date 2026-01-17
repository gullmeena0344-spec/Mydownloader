import os
import re
import shutil
import asyncio
import subprocess
import time
import math
import requests

from pyrogram import Client, filters
from pyrogram.types import Message

# ================= CONFIG =================

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))

DOWNLOAD_DIR = "downloads"
SPLIT_SIZE = 1900 * 1024 * 1024  # 1.9GB
MIN_FREE_MB = 800

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

PIXELDRAIN_RE = re.compile(r"https?://pixeldrain\.com/u/([A-Za-z0-9]+)")

QUEUE = asyncio.Queue()
BUSY = False

# ================= BOT =================

app = Client(
    "video_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

# ================= UTILS =================

def disk_ok():
    total, used, free = shutil.disk_usage("/")
    return free // (1024 * 1024) > MIN_FREE_MB

def cleanup(path):
    try:
        if os.path.isfile(path):
            os.remove(path)
        elif os.path.isdir(path):
            shutil.rmtree(path)
    except:
        pass

def sizeof_fmt(num):
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if num < 1024:
            return f"{num:.2f}{unit}"
        num /= 1024

async def progress(current, total, msg, start, label):
    now = time.time()
    diff = now - start
    if diff < 1:
        return
    percent = current * 100 / total
    speed = current / diff
    bar = "█" * int(percent / 10) + "░" * (10 - int(percent / 10))
    try:
        await msg.edit(
            f"{label}\n"
            f"[{bar}] {percent:.1f}%\n"
            f"{sizeof_fmt(current)} / {sizeof_fmt(total)}\n"
            f"Speed: {sizeof_fmt(speed)}/s"
        )
    except:
        pass

# ================= MEDIA FIX =================

def faststart_fix(src, dst):
    subprocess.run(
        ["ffmpeg", "-y", "-i", src, "-map", "0", "-c", "copy", "-movflags", "+faststart", dst],
        check=True
    )

def generate_thumbnail(video, thumb):
    subprocess.run(
        ["ffmpeg", "-y", "-i", video, "-ss", "00:00:02", "-vframes", "1", thumb],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )

def split_video(path):
    size = os.path.getsize(path)
    if size <= SPLIT_SIZE:
        return [path]

    parts = []
    base = os.path.splitext(path)[0]

    subprocess.run(
        [
            "ffmpeg", "-y", "-i", path, "-c", "copy", "-map", "0",
            "-f", "segment", "-segment_time", "3600",
            f"{base}_part%03d.mp4"
        ],
        check=True
    )

    for f in sorted(os.listdir(DOWNLOAD_DIR)):
        if f.startswith(os.path.basename(base)) and f.endswith(".mp4"):
            parts.append(os.path.join(DOWNLOAD_DIR, f))

    cleanup(path)
    return parts

# ================= DOWNLOADERS =================

def pixeldrain_download(url):
    fid = PIXELDRAIN_RE.search(url).group(1)
    out = os.path.join(DOWNLOAD_DIR, f"{fid}.mp4")
    with requests.get(f"https://pixeldrain.com/api/file/{fid}", stream=True) as r:
        r.raise_for_status()
        with open(out, "wb") as f:
            for c in r.iter_content(1024 * 1024):
                if c:
                    f.write(c)
    return out

def yt_dlp_download(url):
    out = os.path.join(DOWNLOAD_DIR, "%(title)s.%(ext)s")
    cmd = [
        "yt-dlp",
        "-f", "bv*+ba/b",
        "--merge-output-format", "mp4",
        "--no-playlist",
        "-o", out,
        url
    ]
    subprocess.run(cmd, check=True)
    for f in os.listdir(DOWNLOAD_DIR):
        if f.lower().endswith(".mp4"):
            return os.path.join(DOWNLOAD_DIR, f)
    raise Exception("yt-dlp failed")

def aria2_download(url):
    subprocess.run(
        ["aria2c", "-x", "8", "-s", "8", "-d", DOWNLOAD_DIR, url],
        check=True
    )
    for f in os.listdir(DOWNLOAD_DIR):
        if f.lower().endswith(".mp4"):
            return os.path.join(DOWNLOAD_DIR, f)
    raise Exception("aria2 failed")

# ================= PROCESS =================

async def process(msg: Message):
    if not disk_ok():
        await msg.reply("❌ Disk almost full, wait for cleanup")
        return

    status = await msg.reply("⬇️ Downloading...")
    start = time.time()
    text = msg.text.strip()

    try:
        if PIXELDRAIN_RE.search(text):
            raw = pixeldrain_download(text)
        else:
            try:
                raw = yt_dlp_download(text)
            except:
                raw = aria2_download(text)

        fixed = raw.replace(".mp4", "_fixed.mp4")
        faststart_fix(raw, fixed)
        cleanup(raw)

        thumb = fixed.replace(".mp4", ".jpg")
        generate_thumbnail(fixed, thumb)

        parts = split_video(fixed)

        for i, part in enumerate(parts, 1):
            await status.edit(f"⬆️ Uploading part {i}/{len(parts)}")
            await msg.reply_video(
                part,
                thumb=thumb,
                supports_streaming=True,
                progress=progress,
                progress_args=(status, start, "Uploading")
            )
            cleanup(part)

        cleanup(thumb)
        await status.delete()

    except Exception as e:
        await status.edit(f"❌ Error: {e}")
        cleanup(DOWNLOAD_DIR)

# ================= QUEUE =================

@app.on_message(filters.private & filters.user(ADMIN_ID))
async def handler(_, msg: Message):
    global BUSY
    await QUEUE.put(msg)
    await msg.reply("📥 Added to queue")

    if BUSY:
        return

    BUSY = True
    while not QUEUE.empty():
        task = await QUEUE.get()
        await process(task)
        QUEUE.task_done()
    BUSY = False

# ================= START =================

app.run()
