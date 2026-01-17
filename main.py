import os
import re
import shutil
import asyncio
import subprocess
import time
import math
import requests
import mimetypes

from pyrogram import Client, filters
from pyrogram.types import Message

# ================= CONFIG =================

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))

DOWNLOAD_DIR = "downloads"
MAX_SIZE = 1900 * 1024 * 1024  # 1.9GB split limit

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

PIXELDRAIN_RE = re.compile(r"https?://pixeldrain\.com/u/([\w-]+)")
GOFILE_RE = re.compile(r"https?://gofile\.io/d/([\w-]+)")

QUEUE = asyncio.Queue()
BUSY = False

# ================= BOT =================

app = Client(
    "bot",
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

def sizeof_fmt(num):
    for unit in ["B","KB","MB","GB","TB"]:
        if num < 1024:
            return f"{num:.2f}{unit}"
        num /= 1024

async def progress(current, total, msg, start, label):
    if total == 0:
        return
    now = time.time()
    diff = now - start
    speed = current / diff if diff else 0
    percent = current * 100 / total
    bar = "█" * int(percent / 10) + "░" * (10 - int(percent / 10))
    text = (
        f"{label}\n"
        f"[{bar}] {percent:.1f}%\n"
        f"{sizeof_fmt(current)} / {sizeof_fmt(total)}\n"
        f"Speed: {sizeof_fmt(speed)}/s"
    )
    try:
        await msg.edit(text)
    except:
        pass

def is_video(path):
    mime, _ = mimetypes.guess_type(path)
    return mime and mime.startswith("video")

# ================= FIX VIDEO =================

def faststart_mp4(path):
    out = path + ".fast.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-i", path, "-map", "0", "-c", "copy", "-movflags", "+faststart", out],
        check=True
    )
    os.replace(out, path)

def mkv_to_mp4(path):
    if not path.lower().endswith(".mkv"):
        return path
    out = path.replace(".mkv", ".mp4")
    subprocess.run(
        ["ffmpeg", "-y", "-i", path, "-map", "0", "-c", "copy", out],
        check=True
    )
    cleanup(path)
    return out

def split_video(path):
    size = os.path.getsize(path)
    if size <= MAX_SIZE:
        return [path]

    parts = []
    total = math.ceil(size / MAX_SIZE)

    for i in range(total):
        out = f"{path}.part{i+1}.mp4"
        subprocess.run(
            [
                "ffmpeg", "-y", "-i", path,
                "-ss", str(i * 600),
                "-t", "600",
                "-c", "copy",
                out
            ],
            check=True
        )
        parts.append(out)

    cleanup(path)
    return parts

# ================= DOWNLOADERS =================

def pixeldrain_download(url):
    fid = PIXELDRAIN_RE.search(url).group(1)
    r = requests.get(f"https://pixeldrain.com/api/file/{fid}", stream=True)
    r.raise_for_status()

    cd = r.headers.get("Content-Disposition", "")
    name = cd.split("filename=")[-1].strip('"') if "filename=" in cd else f"{fid}.mp4"

    out = os.path.join(DOWNLOAD_DIR, name)
    with open(out, "wb") as f:
        for c in r.iter_content(1024 * 1024):
            if c:
                f.write(c)
    return out

def gofile_download(url):
    page = requests.get(url).text
    token = re.search(r'"token":"(.*?)"', page).group(1)
    cid = GOFILE_RE.search(url).group(1)

    api = requests.get(
        f"https://api.gofile.io/getContent?contentId={cid}&token={token}"
    ).json()

    for item in api["data"]["contents"].values():
        if item["type"] == "file" and item["name"].lower().endswith((".mp4",".mkv",".webm",".avi")):
            out = os.path.join(DOWNLOAD_DIR, item["name"])
            r = requests.get(item["link"], stream=True)
            with open(out, "wb") as f:
                for c in r.iter_content(1024 * 1024):
                    if c:
                        f.write(c)
            return out

    raise Exception("No video found")

def yt_dlp_download(url):
    out = os.path.join(DOWNLOAD_DIR, "%(title)s.%(ext)s")
    subprocess.run(
        ["yt-dlp", "-f", "bv*+ba/b", "--merge-output-format", "mp4", "-o", out, url],
        check=True
    )
    for f in os.listdir(DOWNLOAD_DIR):
        return os.path.join(DOWNLOAD_DIR, f)

# ================= PROCESS =================

async def process(msg: Message):
    status = await msg.reply("⬇️ Downloading...")
    start = time.time()

    try:
        text = msg.text or ""

        if PIXELDRAIN_RE.search(text):
            file = pixeldrain_download(text)
        elif GOFILE_RE.search(text):
            file = gofile_download(text)
        else:
            file = yt_dlp_download(text)

        if not is_video(file):
            cleanup(file)
            await status.edit("❌ Not a video")
            return

        file = mkv_to_mp4(file)
        faststart_mp4(file)

        parts = split_video(file)

        for p in parts:
            await msg.reply_video(
                p,
                supports_streaming=True,
                progress=progress,
                progress_args=(status, start, "Uploading")
            )
            cleanup(p)

        await status.delete()

    except Exception as e:
        cleanup(DOWNLOAD_DIR)
        await status.edit(f"❌ Error: {e}")

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
