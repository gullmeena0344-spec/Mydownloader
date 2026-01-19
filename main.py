import os
import re
import asyncio
import shutil
import logging
import subprocess
from pathlib import Path
from collections import deque

from pyrogram import Client, filters
from pyrogram.types import Message

from run import GoFile, Downloader, File

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

# ================= PROGRESS =================

async def tg_progress(current, total, msg, prefix):
    if cancel_flag:
        raise asyncio.CancelledError
    if total:
        await msg.edit(
            f"{prefix}\n"
            f"{current/1024/1024:.1f} / {total/1024/1024:.1f} MB"
        )

# ================= WORKER =================

async def worker(client: Client):
    global active, cancel_flag
    active = True

    while queue:
        url, msg = queue.popleft()
        cancel_flag = False

        shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
        DOWNLOAD_DIR.mkdir(exist_ok=True)

        await msg.edit("⬇️ Fetching files...")

        gf = GoFile()
        gf.update_token()
        files = gf.get_files(url, output=str(DOWNLOAD_DIR))

        if not files:
            await msg.edit("❌ No files found")
            continue

        dl = Downloader(gf.token)

        for f in files:
            if cancel_flag:
                break

            await msg.edit(f"⬇️ Downloading `{f.name}`")

            parts = list(dl.download_in_chunks(f))
            total_parts = len(parts)

            for idx, part in enumerate(parts, start=1):
                if cancel_flag:
                    break

                await msg.edit(
                    f"⬇️ Downloading `{f.name}`\n"
                    f"📦 Chunk {idx} / {total_parts}"
                )

                fixed = part + ".fixed.mp4"
                remux(part, fixed)
                faststart(fixed, part)
                os.remove(fixed)

                t = part + ".jpg"
                thumb(part, t)

                await client.send_video(
                    "me",
                    video=part,
                    thumb=t,
                    supports_streaming=True,
                    progress=tg_progress,
                    progress_args=(msg, "⬆️ Uploading"),
                )

                os.remove(part)
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
    await msg.reply("🛑 Cancelled")

@app.on_message(filters.text)
async def handler(client: Client, msg: Message):
    global active

    m = GOFILE_RE.search(msg.text or "")
    if not m:
        return

    reply = await msg.reply("📥 Added to queue")
    queue.append((m.group(0), reply))

    if not active:
        asyncio.create_task(worker(client))

app.run()
