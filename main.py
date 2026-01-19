import os
import re
import asyncio
import shutil
import subprocess
from pathlib import Path
from collections import deque

from pyrogram import Client, filters
from pyrogram.types import Message, InputMediaVideo

from run import GoFile, Downloader

# ================= CONFIG =================

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")

DOWNLOAD_DIR = Path("output")
MAX_TG_SIZE = 2 * 1024 * 1024 * 1024
THUMB_TIME = 20

GOFILE_RE = re.compile(r"https?://gofile\.io/d/\w+", re.I)

queue = deque()
active = False
cancel_flag = False

# ================= APP =================

app = Client(
    "gofile-userbot",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING,
)

# ================= FFMPEG =================

def run(cmd):
    subprocess.run(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False
    )

def remux(src, dst):
    run(["ffmpeg", "-y", "-err_detect", "ignore_err", "-i", src, "-map", "0", "-c", "copy", dst])

def faststart(src, dst):
    run(["ffmpeg", "-y", "-i", src, "-map", "0", "-c", "copy", "-movflags", "+faststart", dst])

def thumb(video, out):
    run(["ffmpeg", "-y", "-ss", str(THUMB_TIME), "-i", video, "-frames:v", "1", out])

# ================= ALBUM =================

async def send_album(client, videos, thumbs):
    media = []
    for v, t in zip(videos, thumbs):
        media.append(
            InputMediaVideo(
                media=v,
                thumb=t,
                supports_streaming=True
            )
        )
    await client.send_media_group("me", media)

# ================= WORKER =================

async def worker(client: Client):
    global active, cancel_flag
    active = True
    loop = asyncio.get_running_loop()

    while queue:
        url, status = queue.popleft()
        cancel_flag = False

        shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
        DOWNLOAD_DIR.mkdir(exist_ok=True)

        await status.edit("⬇️ Fetching files...")

        gf = GoFile()
        gf.update_token()

        files = await loop.run_in_executor(
            None, gf.get_files, url, str(DOWNLOAD_DIR)
        )

        if not files:
            await status.edit("❌ No files found")
            continue

        dl = Downloader(gf.token)

        for f in files:
            if cancel_flag:
                break

            await status.edit(f"⬇️ Downloading `{f.name}`")

            parts = await loop.run_in_executor(
                None, lambda: list(dl.download_in_chunks(f))
            )

            album_videos = []
            album_thumbs = []

            for part in parts:
                if cancel_flag:
                    break

                if os.path.getsize(part) > MAX_TG_SIZE:
                    os.remove(part)
                    continue

                fixed = part + ".fixed.mp4"
                remux(part, fixed)
                faststart(fixed, part)
                os.remove(fixed)

                t = part + ".jpg"
                thumb(part, t)

                album_videos.append(part)
                album_thumbs.append(t)

                if len(album_videos) == 10:
                    await send_album(client, album_videos, album_thumbs)
                    for v, th in zip(album_videos, album_thumbs):
                        os.remove(v)
                        os.remove(th)
                    album_videos.clear()
                    album_thumbs.clear()

            if album_videos:
                await send_album(client, album_videos, album_thumbs)
                for v, th in zip(album_videos, album_thumbs):
                    os.remove(v)
                    os.remove(th)

        shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
        await status.edit("✅ Done")

    active = False

# ================= COMMANDS =================

@app.on_message(filters.command("cancel"))
async def cancel(_, msg):
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
