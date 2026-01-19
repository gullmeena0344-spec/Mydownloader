import os
import re
import asyncio
import shutil
import logging
import subprocess
from pathlib import Path

from pyrogram import Client, filters
from pyrogram.types import Message

from run import GoFile

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")

DOWNLOAD_DIR = Path("output")
MAX_TG_SIZE = 2 * 1024 * 1024 * 1024
THUMB_TIME = 20

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("BOT")

app = Client(
    "gofile-userbot",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING,
)

def cmd(cmd):
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def faststart(src, dst):
    cmd(["ffmpeg", "-y", "-i", src, "-map", "0", "-c", "copy", "-movflags", "+faststart", dst])

def remux(src, dst):
    cmd(["ffmpeg", "-y", "-err_detect", "ignore_err", "-i", src, "-map", "0", "-c", "copy", dst])

def thumb(video, out):
    cmd(["ffmpeg", "-y", "-ss", str(THUMB_TIME), "-i", video, "-frames:v", "1", out])

def split(path):
    if os.path.getsize(path) <= MAX_TG_SIZE:
        return [path]

    base = Path(path)
    pattern = base.with_name(f"{base.stem}_part%03d.mp4")
    cmd(["ffmpeg", "-i", path, "-map", "0", "-c", "copy", "-f", "segment", "-segment_time", "3600", str(pattern)])
    os.remove(path)
    return sorted(str(p) for p in base.parent.glob(f"{base.stem}_part*.mp4"))

GOFILE_RE = re.compile(r"https?://gofile\.io/d/\w+", re.I)

@app.on_message(filters.text)
async def handler(client, message: Message):
    if not message.text:
        return
    m = GOFILE_RE.search(message.text)
    if not m:
        return

    url = m.group(0)
    status = await message.reply("⬇️ Downloading...")
    shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
    DOWNLOAD_DIR.mkdir(exist_ok=True)

    GoFile().execute(dir=str(DOWNLOAD_DIR), url=url, num_threads=1)

    files = [p for p in DOWNLOAD_DIR.rglob("*") if p.is_file()]
    if not files:
        return await status.edit("❌ No files found")

    for f in files:
        fixed = f.with_suffix(".fixed.mp4")
        remux(str(f), str(fixed))
        faststart(str(fixed), str(f))
        os.remove(fixed)

        thumb_path = f.with_suffix(".jpg")
        thumb(str(f), str(thumb_path))

        parts = split(str(f))
        for p in parts:
            await client.send_video(
                "me",
                video=p,
                thumb=str(thumb_path),
                supports_streaming=True
            )
            os.remove(p)

        os.remove(thumb_path)

    shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
    await status.edit("✅ Done")

app.run()
