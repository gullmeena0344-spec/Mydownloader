import os
import re
import shutil
import asyncio
import zipfile
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
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

PIXELDRAIN_RE = re.compile(r"https?://pixeldrain\.com/u/(\w+)")
GOFILE_RE = re.compile(r"https?://gofile\.io/d/(\w+)")

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
    for unit in ['B','KB','MB','GB','TB']:
        if num < 1024:
            return f"{num:.2f}{unit}"
        num /= 1024

async def progress(current, total, msg, start, label):
    now = time.time()
    diff = now - start
    if diff == 0:
        return
    speed = current / diff
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

# ================= DOWNLOADERS =================

def pixeldrain_download(url):
    fid = PIXELDRAIN_RE.search(url).group(1)
    out = os.path.join(DOWNLOAD_DIR, f"{fid}.bin")
    r = requests.get(f"https://pixeldrain.com/api/file/{fid}", stream=True)
    with open(out, "wb") as f:
        for c in r.iter_content(1024 * 1024):
            f.write(c)
    return out

def gofile_download(url):
    page = requests.get(url).text
    token = re.search(r'"token":"(.*?)"', page).group(1)
    content_id = GOFILE_RE.search(url).group(1)
    api = requests.get(
        f"https://api.gofile.io/getContent?contentId={content_id}&token={token}"
    ).json()
    file_url = list(api["data"]["contents"].values())[0]["link"]
    name = list(api["data"]["contents"].values())[0]["name"]
    out = os.path.join(DOWNLOAD_DIR, name)
    r = requests.get(file_url, stream=True)
    with open(out, "wb") as f:
        for c in r.iter_content(1024 * 1024):
            f.write(c)
    return out

def yt_dlp_download(url):
    out = os.path.join(DOWNLOAD_DIR, "%(title)s.%(ext)s")
    subprocess.run(
        ["yt-dlp", "-o", out, "--no-playlist", url],
        check=True
    )
    for f in os.listdir(DOWNLOAD_DIR):
        return os.path.join(DOWNLOAD_DIR, f)

def aria2_download(url):
    subprocess.run(
        ["aria2c", "-x", "8", "-s", "8", "-d", DOWNLOAD_DIR, url],
        check=True
    )
    for f in os.listdir(DOWNLOAD_DIR):
        return os.path.join(DOWNLOAD_DIR, f)

# ================= QUEUE SYSTEM =================

async def process(msg: Message):
    status = await msg.reply("⬇️ Downloading...")
    start = time.time()

    try:
        text = msg.text or ""
        if PIXELDRAIN_RE.search(text):
            file = pixeldrain_download(text)
        elif GOFILE_RE.search(text):
            file = gofile_download(text)
        elif text.startswith("http"):
            file = yt_dlp_download(text)
        else:
            await status.edit("❌ Unsupported link")
            return

        await status.edit("⬆️ Uploading...")
        await msg.reply_document(
            file,
            progress=progress,
            progress_args=(status, start, "Uploading")
        )
        cleanup(file)

    except Exception as e:
        await status.edit(f"❌ Error: {e}")
        cleanup(DOWNLOAD_DIR)

# ================= HANDLERS =================

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

@app.on_message(filters.document & filters.user(ADMIN_ID))
async def zip_handler(_, msg: Message):
    if not msg.document.file_name.endswith(".zip"):
        return

    status = await msg.reply("📦 Downloading ZIP...")
    zip_path = await msg.download(file_name=DOWNLOAD_DIR)
    extract_dir = os.path.join(DOWNLOAD_DIR, "extracted")
    os.makedirs(extract_dir, exist_ok=True)

    with zipfile.ZipFile(zip_path) as z:
        z.extractall(extract_dir)

    cleanup(zip_path)

    for f in os.listdir(extract_dir):
        path = os.path.join(extract_dir, f)
        await msg.reply_document(path)
        cleanup(path)

    cleanup(extract_dir)
    await status.edit("✅ ZIP processed")

# ================= START =================

app.run()
