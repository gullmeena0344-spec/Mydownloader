import os
import re
import math
import asyncio
import shutil
import time
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
MAX_TG_SIZE = 1990 * 1024 * 1024
MIN_FREE_SPACE_MB = 300  # safety buffer
MAX_DISK_USAGE = 3.5 * 1024 * 1024 * 1024  # 3.5GB hard cap

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("BOT")

app = Client(
    "gofile-userbot",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING
)

# ---------------- HELPERS ----------------

def get_free_space():
    return shutil.disk_usage(os.getcwd()).free

def get_used_space():
    total = 0
    for f in DOWNLOAD_DIR.rglob("*"):
        if f.is_file():
            total += f.stat().st_size
    return total

def format_bytes(size):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024:
            return f"{size:.2f} {unit}"
        size /= 1024

async def progress_bar(current, total, status_msg, action):
    try:
        now = time.time()
        if hasattr(status_msg, "last_update") and now - status_msg.last_update < 4:
            return
        status_msg.last_update = now
        pct = current * 100 / total
        await status_msg.edit(
            f"{action}\n"
            f"{pct:.1f}%\n"
            f"{format_bytes(current)} / {format_bytes(total)}"
        )
    except:
        pass

# ---------------- FFMPEG ----------------

def cmd(cmd_list):
    subprocess.run(cmd_list, check=True, capture_output=True)

def get_duration(path):
    try:
        out = subprocess.check_output([
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            path
        ])
        return float(out.decode().strip())
    except:
        return 0

def process_video_sync(src, dst):
    cmd(["ffmpeg", "-y", "-i", src, "-map", "0", "-c", "copy", "-movflags", "+faststart", dst])

def thumb_sync(video, out):
    seek = "00:00:01" if get_duration(video) < 20 else "00:00:20"
    cmd(["ffmpeg", "-y", "-ss", seek, "-i", video, "-frames:v", "1", out])

def split_sync(path):
    size = os.path.getsize(path)
    if size <= MAX_TG_SIZE:
        return [path]

    duration = get_duration(path)
    parts = math.ceil(size / MAX_TG_SIZE)
    seg_time = int(duration / parts) if duration else 1200

    pattern = Path(path).with_name("part_%03d.mp4")
    cmd([
        "ffmpeg", "-i", path,
        "-map", "0", "-c", "copy",
        "-f", "segment",
        "-segment_time", str(seg_time),
        "-reset_timestamps", "1",
        str(pattern)
    ])
    os.remove(path)
    return sorted(str(p) for p in Path(path).parent.glob("part_*.mp4"))

# ---------------- DOWNLOAD MONITOR ----------------

async def monitor_download(status):
    while True:
        await asyncio.sleep(3)
        used = get_used_space()
        if used > MAX_DISK_USAGE:
            raise RuntimeError("Disk limit reached")

        try:
            await status.edit(
                f"⬇️ Downloading\n"
                f"Used: {format_bytes(used)} / 4GB"
            )
        except:
            pass

# ---------------- HANDLER ----------------

@app.on_message(filters.text & (filters.outgoing | filters.private))
async def handler(client, message: Message):
    m = re.search(r"gofile\.io/d/([\w\-]+)", message.text)
    if not m:
        return

    if get_free_space() < MIN_FREE_SPACE_MB * 1024 * 1024:
        return await message.reply("❌ Disk full")

    gofile_id = m.group(1)
    status = await message.reply("⬇️ Starting download")

    shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    downloader = GoFile()
    monitor = asyncio.create_task(monitor_download(status))

    try:
        await asyncio.to_thread(
            downloader.execute,
            dir=str(DOWNLOAD_DIR),
            content_id=gofile_id,
            num_threads=1
        )
    except RuntimeError:
        pass
    finally:
        monitor.cancel()

    files = sorted(
        [f for f in DOWNLOAD_DIR.rglob("*") if f.is_file()],
        key=lambda x: x.stat().st_size
    )

    for f in files:
        clean = re.sub(r'[^a-zA-Z0-9.]', '_', f.name)
        new_path = f.parent / clean
        os.rename(f, new_path)

        fixed = new_path.with_suffix(".fixed.mp4")
        try:
            await asyncio.to_thread(process_video_sync, str(new_path), str(fixed))
            if fixed.exists():
                os.replace(fixed, new_path)
        except:
            if fixed.exists():
                fixed.unlink()

        thumb = new_path.with_suffix(".jpg")
        try:
            await asyncio.to_thread(thumb_sync, str(new_path), str(thumb))
        except:
            pass

        parts = await asyncio.to_thread(split_sync, str(new_path))

        for i, p in enumerate(parts):
            try:
                await client.send_video(
                    "me",
                    video=p,
                    thumb=str(thumb) if thumb.exists() else None,
                    caption=f"Part {i+1}/{len(parts)}",
                    progress=progress_bar,
                    progress_args=(status, f"⬆️ Uploading {i+1}")
                )
            except:
                await client.send_document(
                    "me",
                    document=p,
                    caption=f"Part {i+1}/{len(parts)}",
                    progress=progress_bar,
                    progress_args=(status, f"⬆️ Uploading {i+1}")
                )
            os.remove(p)

        if thumb.exists():
            thumb.unlink()

    shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
    await status.edit("✅ Done")

app.run()
