import os
import re
import shutil
import asyncio
import subprocess
import time
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

PIXELDRAIN_RE = re.compile(r"https?://pixeldrain\.com/u/([A-Za-z0-9]+)")
GOFILE_FOLDER_RE = re.compile(r"https?://gofile\.io/d/([A-Za-z0-9]+)")

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

def disk_ok(min_free_mb=500):
    stat = shutil.disk_usage("/")
    return (stat.free / (1024 * 1024)) > min_free_mb

def sizeof_fmt(num):
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if num < 1024:
            return f"{num:.2f}{unit}"
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
            f"Speed: {sizeof_fmt(speed)}/s"
        )
    except:
        pass

# ================= DOWNLOADERS =================

def pixeldrain_download(url):
    fid = PIXELDRAIN_RE.search(url).group(1)
    info = requests.get(f"https://pixeldrain.com/api/file/{fid}/info").json()
    ext = info.get("mime_type", "").split("/")[-1] or "mp4"
    out = os.path.join(DOWNLOAD_DIR, f"{fid}.{ext}")

    with requests.get(f"https://pixeldrain.com/api/file/{fid}", stream=True) as r:
        r.raise_for_status()
        with open(out, "wb") as f:
            for chunk in r.iter_content(1024 * 1024):
                if chunk:
                    f.write(chunk)

    return out

def gofile_direct_download(url):
    name = url.split("/")[-1].split("?")[0]
    out = os.path.join(DOWNLOAD_DIR, name)

    with requests.get(url, stream=True, timeout=30) as r:
        r.raise_for_status()
        with open(out, "wb") as f:
            for chunk in r.iter_content(1024 * 1024):
                if chunk:
                    f.write(chunk)

    return out

def gofile_folder_download(url):
    page = requests.get(url).text
    token_match = re.search(r'"token":"(.*?)"', page)
    if not token_match:
        raise Exception("GoFile token not found")

    token = token_match.group(1)
    cid = GOFILE_FOLDER_RE.search(url).group(1)

    api = requests.get(
        f"https://api.gofile.io/getContent?contentId={cid}&token={token}"
    ).json()

    for f in api["data"]["contents"].values():
        if f["name"].lower().endswith((".mp4", ".mkv", ".webm", ".avi", ".mov")):
            return gofile_direct_download(f["link"])

    raise Exception("No video found in GoFile folder")

def yt_dlp_download(url):
    out = os.path.join(DOWNLOAD_DIR, "%(title)s.%(ext)s")
    subprocess.run(
        [
            "yt-dlp",
            "-f",
            "bv*+ba/b",
            "--merge-output-format",
            "mp4",
            "--no-playlist",
            "-o",
            out,
            url,
        ],
        check=True,
    )

    for f in os.listdir(DOWNLOAD_DIR):
        if f.lower().endswith((".mp4", ".mkv", ".webm", ".avi", ".mov")):
            return os.path.join(DOWNLOAD_DIR, f)

    raise Exception("yt-dlp produced no video")

# ================= QUEUE PROCESS =================

async def process(msg: Message):
    if not disk_ok():
        await msg.reply("❌ Disk almost full, wait for cleanup")
        return

    status = await msg.reply("⬇️ Downloading...")
    start = time.time()

    try:
        text = msg.text or ""

        if PIXELDRAIN_RE.search(text):
            file = pixeldrain_download(text)

        elif "gofile.io/download/web/" in text:
            file = gofile_direct_download(text)

        elif GOFILE_FOLDER_RE.search(text):
            file = gofile_folder_download(text)

        elif text.startswith("http"):
            file = yt_dlp_download(text)

        else:
            await status.edit("❌ Unsupported link")
            return

        await status.edit("⬆️ Uploading...")
        await msg.reply_video(
            file,
            supports_streaming=True,
            progress=progress,
            progress_args=(status, start, "Uploading"),
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

# ================= START =================

app.run()
