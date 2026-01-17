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
THUMB_DIR = "thumbs"
SPLIT_SIZE = 1900 * 1024 * 1024  # ~2GB

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(THUMB_DIR, exist_ok=True)

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

def cleanup(path):
    try:
        if os.path.isfile(path):
            os.remove(path)
        elif os.path.isdir(path):
            shutil.rmtree(path)
    except:
        pass

def disk_ok(min_mb=600):
    total, used, free = shutil.disk_usage("/")
    return free // (1024 * 1024) > min_mb

def sizeof_fmt(num):
    for u in ["B", "KB", "MB", "GB"]:
        if num < 1024:
            return f"{num:.2f}{u}"
        num /= 1024

async def progress(current, total, msg, start, label):
    now = time.time()
    diff = now - start
    if diff <= 0:
        return
    speed = current / diff
    percent = current * 100 / total
    bar = "█" * int(percent / 10) + "░" * (10 - int(percent / 10))
    try:
        await msg.edit(
            f"{label}\n"
            f"[{bar}] {percent:.1f}%\n"
            f"{sizeof_fmt(current)} / {sizeof_fmt(total)}\n"
            f"{sizeof_fmt(speed)}/s"
        )
    except:
        pass

# ================= VIDEO FIXES =================

def faststart_fix(inp):
    out = inp.replace(".mp4", "_fast.mp4")
    subprocess.run(
        ["ffmpeg", "-y", "-i", inp, "-map", "0", "-c", "copy", "-movflags", "+faststart", out],
        check=True
    )
    cleanup(inp)
    return out

def generate_thumb(video):
    thumb = os.path.join(THUMB_DIR, os.path.basename(video) + ".jpg")
    subprocess.run(
        ["ffmpeg", "-y", "-i", video, "-ss", "00:00:02", "-vframes", "1", thumb],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    return thumb if os.path.exists(thumb) else None

def split_video(path):
    size = os.path.getsize(path)
    if size <= SPLIT_SIZE:
        return [path]

    parts = math.ceil(size / SPLIT_SIZE)
    out_files = []

    for i in range(parts):
        out = path.replace(".mp4", f".part{i+1}.mp4")
        subprocess.run(
            [
                "ffmpeg", "-y", "-i", path,
                "-ss", str(i * 600),
                "-t", "600",
                "-c", "copy",
                out
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        if os.path.exists(out):
            out_files.append(out)

    cleanup(path)
    return out_files

# ================= DOWNLOADERS =================

def pixeldrain_download(url):
    fid = PIXELDRAIN_RE.search(url).group(1)
    out = os.path.join(DOWNLOAD_DIR, f"{fid}.mp4")
    r = requests.get(f"https://pixeldrain.com/api/file/{fid}", stream=True)
    with open(out, "wb") as f:
        for c in r.iter_content(1024 * 1024):
            f.write(c)
    return out

def yt_dlp_download(url):
    out = os.path.join(DOWNLOAD_DIR, "%(title)s.%(ext)s")
    try:
        subprocess.run(
            ["yt-dlp", "-f", "bv*+ba/b", "--merge-output-format", "mp4", "-o", out, url],
            check=True
        )
    except:
        return None

    for f in os.listdir(DOWNLOAD_DIR):
        if f.endswith(".mp4"):
            return os.path.join(DOWNLOAD_DIR, f)
    return None

def aria2_download(url):
    subprocess.run(
        ["aria2c", "-x", "8", "-s", "8", "-d", DOWNLOAD_DIR, url],
        check=True
    )
    for f in os.listdir(DOWNLOAD_DIR):
        if f.endswith(".mp4"):
            return os.path.join(DOWNLOAD_DIR, f)
    return None

# ================= PROCESS =================

async def process(msg: Message):
    if not disk_ok():
        await msg.reply("❌ Disk low, wait for cleanup")
        return

    status = await msg.reply("⬇️ Downloading...")
    start = time.time()

    try:
        url = msg.text.strip()

        if PIXELDRAIN_RE.search(url):
            video = pixeldrain_download(url)
        else:
            video = yt_dlp_download(url) or aria2_download(url)

        if not video:
            await status.edit("❌ Download failed")
            return

        video = faststart_fix(video)
        parts = split_video(video)

        for part in parts:
            thumb = generate_thumb(part)
            await msg.reply_video(
                part,
                thumb=thumb,
                supports_streaming=True,
                progress=progress,
                progress_args=(status, start, "Uploading")
            )
            cleanup(part)
            if thumb:
                cleanup(thumb)

        await status.delete()

    except Exception as e:
        await status.edit(f"❌ Error: {e}")
        cleanup(DOWNLOAD_DIR)
        cleanup(THUMB_DIR)

# ================= HANDLER =================

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
