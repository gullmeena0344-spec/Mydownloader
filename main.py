import os
import re
import asyncio
import shutil
import logging
import subprocess
import threading
import time
from pathlib import Path
from collections import deque

from pyrogram import Client, filters
from pyrogram.types import Message

from run import GoFile

# ================= CONFIG =================

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")

DOWNLOAD_DIR = Path("output")
MAX_TG_SIZE = 2 * 1024 * 1024 * 1024
THUMB_TIME = 20

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("GOFILE-USERBOT")

app = Client(
    "gofile-userbot",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING,
)

GOFILE_RE = re.compile(r"https?://gofile\.io/d/\w+", re.I)

# ================= QUEUE / STATE =================

queue = deque()
active = False
cancel_flag = False

# ================= FFMPEG =================

def run(cmd):
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

def remux(src, dst):
    run(["ffmpeg", "-y", "-err_detect", "ignore_err", "-i", src, "-map", "0", "-c", "copy", dst])

def faststart(src, dst):
    run(["ffmpeg", "-y", "-i", src, "-map", "0", "-c", "copy", "-movflags", "+faststart", dst])

def thumb(video, out):
    run(["ffmpeg", "-y", "-ss", str(THUMB_TIME), "-i", video, "-frames:v", "1", out])

def split_video(path):
    if os.path.getsize(path) <= MAX_TG_SIZE:
        return [path]

    base = Path(path)
    pattern = base.with_name(f"{base.stem}_part%03d.mp4")

    run([
        "ffmpeg", "-y", "-i", path,
        "-map", "0", "-c", "copy",
        "-f", "segment",
        "-segment_time", "3600",
        str(pattern)
    ])

    os.remove(path)
    return sorted(str(p) for p in base.parent.glob(f"{base.stem}_part*.mp4"))

# ================= PROGRESS =================

async def tg_progress(current, total, msg, prefix):
    if cancel_flag:
        raise asyncio.CancelledError
    if total:
        pct = current * 100 / total
        await msg.edit(
            f"{prefix}\n"
            f"📊 {pct:.1f}%\n"
            f"📦 {current/1024/1024:.1f} / {total/1024/1024:.1f} MB"
        )

# ================= DOWNLOAD WITH PROGRESS =================

def download_with_progress(url, msg):
    global cancel_flag
    downloader = GoFile()

    last = 0
    while True:
        if cancel_flag:
            return

        downloader.execute(dir=str(DOWNLOAD_DIR), url=url, num_threads=1)
        break

# ================= WORKER =================

async def worker(client: Client):
    global active, cancel_flag

    while queue:
        url, msg = queue.popleft()
        active = True
        cancel_flag = False

        shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
        DOWNLOAD_DIR.mkdir(exist_ok=True)

        await msg.edit("⬇️ Downloading...")

        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, download_with_progress, url, msg)
        except Exception as e:
            await msg.edit(f"❌ Download failed:\n`{e}`")
            continue

        files = sorted(p for p in DOWNLOAD_DIR.rglob("*") if p.is_file())
        if not files:
            await msg.edit("❌ No files found")
            continue

        for f in files:
            if cancel_flag:
                break

            fixed = f.with_suffix(".fixed.mp4")
            remux(str(f), str(fixed))
            faststart(str(fixed), str(f))
            os.remove(fixed)

            t = f.with_suffix(".jpg")
            thumb(str(f), str(t))

            parts = split_video(str(f))
            media = []

            for p in parts:
                await client.send_video(
                    "me",
                    video=p,
                    thumb=str(t),
                    supports_streaming=True,
                    progress=tg_progress,
                    progress_args=(msg, "⬆️ Uploading"),
                )
                os.remove(p)

            os.remove(t)

        shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
        await msg.edit("✅ Done")

    active = False

# ================= COMMANDS =================

@app.on_message(filters.command("cancel"))
async def cancel(client, msg):
    global cancel_flag
    cancel_flag = True
    queue.clear()
    shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
    await msg.reply("🛑 Cancelled & cleaned")

@app.on_message(filters.text)
async def handler(client: Client, msg: Message):
    global active

    text = msg.text or ""
    m = GOFILE_RE.search(text)
    if not m:
        return

    url = m.group(0)
    reply = await msg.reply("📥 Added to queue")

    queue.append((url, reply))

    if not active:
        asyncio.create_task(worker(client))

app.run()
