import os
import re
import math
import shutil
import asyncio
import subprocess
import time
import requests
from pyrogram import Client, filters
from pyrogram.types import Message
from urllib.parse import urlparse

# ================== CONFIG ==================

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")

DOWNLOAD_DIR = "downloads"
SPLIT_SIZE = 2 * 1024 * 1024 * 1024  # 2GB

PIXELDRAIN_RE = re.compile(r"https?://pixeldrain\.com/u/([A-Za-z0-9]+)")
GOFILE_RE = re.compile(r"https?://gofile\.io/d/([A-Za-z0-9]+)")

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

app = Client(
    "gofile_pixeldrain_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

# ================== HELPERS ==================

async def progress(current, total, message: Message, start, action):
    now = time.time()
    if now - start < 1:
        return
    percent = current * 100 / total
    speed = current / (now - start)
    eta = (total - current) / speed if speed > 0 else 0

    bar = "█" * int(percent / 10) + "░" * (10 - int(percent / 10))
    text = (
        f"{action}\n"
        f"[{bar}] {percent:.1f}%\n"
        f"{current // (1024*1024)} / {total // (1024*1024)} MB\n"
        f"Speed: {speed / (1024*1024):.2f} MB/s\n"
        f"ETA: {int(eta)}s"
    )
    try:
        await message.edit(text)
    except:
        pass


def clean_dir(path):
    if os.path.exists(path):
        shutil.rmtree(path, ignore_errors=True)


def split_file(filepath):
    parts = []
    size = os.path.getsize(filepath)
    if size <= SPLIT_SIZE:
        return [filepath]

    base = os.path.splitext(filepath)[0]
    with open(filepath, "rb") as f:
        part = 1
        while True:
            chunk = f.read(SPLIT_SIZE)
            if not chunk:
                break
            part_path = f"{base}.part{part}.mp4"
            with open(part_path, "wb") as p:
                p.write(chunk)
            parts.append(part_path)
            part += 1

    os.remove(filepath)
    return parts


def make_thumbnail(video, thumb):
    subprocess.run([
        "ffmpeg", "-y", "-i", video,
        "-ss", "00:00:01", "-vframes", "1", thumb
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ================== DOWNLOADERS ==================

def download_pixeldrain(url, out_dir):
    file_id = PIXELDRAIN_RE.search(url).group(1)
    info = requests.get(f"https://pixeldrain.com/api/file/{file_id}/info").json()
    name = info["name"]

    path = os.path.join(out_dir, name)
    with requests.get(f"https://pixeldrain.com/api/file/{file_id}", stream=True) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_content(1024 * 1024):
                f.write(chunk)
    return path


def download_gofile(url, out_dir):
    from run import GoFile  # uses your remembered run.py

    go = GoFile()
    go.update_token()
    go.update_wt()
    files = go.get_files(dir=out_dir, url=url)
    return [f.path for f in files]


# ================== BOT ==================

@app.on_message(filters.private & filters.text)
async def handle(client, message: Message):
    url = message.text.strip()

    if not (PIXELDRAIN_RE.match(url) or GOFILE_RE.match(url)):
        return await message.reply("❌ Send a valid GoFile or Pixeldrain URL")

    workdir = os.path.join(DOWNLOAD_DIR, str(message.id))
    os.makedirs(workdir, exist_ok=True)

    status = await message.reply("📥 Downloading...")
    start = time.time()

    try:
        paths = []
        if PIXELDRAIN_RE.match(url):
            paths.append(download_pixeldrain(url, workdir))
        else:
            paths.extend(download_gofile(url, workdir))

        media = []
        for path in paths:
            parts = split_file(path)
            for part in parts:
                thumb = part + ".jpg"
                make_thumbnail(part, thumb)

                media.append(
                    await client.send_video(
                        chat_id=message.chat.id,
                        video=part,
                        thumb=thumb if os.path.exists(thumb) else None,
                        progress=progress,
                        progress_args=(status, start, "📤 Uploading")
                    )
                )

        await status.delete()

    except Exception as e:
        await status.edit(f"❌ Error: {e}")

    finally:
        clean_dir(workdir)


app.run()
