import os
import re
import math
import asyncio
import shutil
import time
import logging
import subprocess
import requests
from pathlib import Path

from pyrogram import Client, filters, errors
from pyrogram.types import Message

# --- Import GoFile (From your working script) ---
try:
    from run import GoFile, Downloader, File
except ImportError:
    GoFile = None
    Downloader = None
    print("Warning: run.py not found. GoFile logic will fail.")

# --- Config ---
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")

DOWNLOAD_DIR = Path("output")
MAX_CHUNK_SIZE = 1900 * 1024 * 1024  # ~2GB
MIN_FREE_SPACE_MB = 500

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("BOT")

app = Client(
    "gofile-userbot",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING
)

# ---------------- UTILS ----------------

def get_free_space():
    return shutil.disk_usage(os.getcwd()).free

def format_bytes(size):
    if not size: return "0B"
    for u in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024:
            return f"{size:.2f} {u}"
        size /= 1024

def get_progress_bar(percent, total=15):
    filled = int(total * percent // 100)
    return f"▰{'▰'*filled}{'▱'*(total-filled-1)}▱"

async def progress_bar(current, total, status, title):
    try:
        now = time.time()
        if not hasattr(status, "start"):
            status.start = now
            status.last = 0

        if now - status.last < 3:
            return
        status.last = now

        percent = (current * 100 / total) if total else 0
        elapsed = now - status.start
        speed = current / elapsed if elapsed > 0 else 0
        eta = (total - current) / speed if speed > 0 else 0

        await status.edit(
            f"<b>{title}</b>\n"
            f"<code>{get_progress_bar(percent)} {percent:.1f}%</code>\n"
            f"<b>Size:</b> {format_bytes(current)} / {format_bytes(total)}\n"
            f"<b>Speed:</b> {format_bytes(speed)}/s | <b>ETA:</b> {int(eta)}s"
        )
    except:
        pass

# ---------------- FFMPEG HELPERS ----------------

def faststart_mp4(src):
    dst = src + ".fast.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-i", src, "-c", "copy", "-movflags", "+faststart", dst],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    return dst if os.path.exists(dst) else src

def generate_thumbnail(video_path):
    thumb_path = f"{video_path}.jpg"
    if not os.path.exists(video_path):
        return None

    for ss in ["00:00:15", "00:00:02", "00:00:00"]:
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(video_path), "-ss", ss, "-vframes", "1", thumb_path],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 100:
                return thumb_path
        except:
            continue
    return None

# ---------------- GENERIC HELPERS ----------------

async def ytdlp_download(url, out_path):
    cmd = [
        "yt-dlp",
        "-f", "bv*+ba/b",
        "--merge-output-format", "mp4",
        "--no-playlist",
        "-o", str(out_path),
        url
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL
    )
    await proc.wait()
    return proc.returncode == 0

def split_video(path):
    if os.path.getsize(path) <= MAX_CHUNK_SIZE:
        return [path]

    base = Path(path)
    out = base.with_suffix("")
    parts = []

    subprocess.run(
        [
            "ffmpeg", "-i", str(path),
            "-map", "0", "-c", "copy",
            "-f", "segment",
            "-segment_size", str(MAX_CHUNK_SIZE),
            "-reset_timestamps", "1",
            f"{out}.part%03d.mp4"
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )

    for f in sorted(base.parent.glob(f"{out.name}.part*.mp4")):
        parts.append(f)

    return parts

# ---------------- GENERIC HANDLER ----------------

async def resolve_generic_url(url):
    items = []
    if "pixeldrain.com" in url:
        if "/l/" in url:
            lid = url.split("/l/")[1].split("/")[0]
            try:
                r = requests.get(f"https://pixeldrain.com/api/list/{lid}").json()
                if r.get("success"):
                    for f in r.get("files", []):
                        items.append({
                            "url": f"https://pixeldrain.com/api/file/{f['id']}",
                            "name": f["name"],
                            "size": f["size"]
                        })
            except:
                pass
        elif "/u/" in url:
            fid = url.split("/u/")[1].split("/")[0]
            try:
                r = requests.get(f"https://pixeldrain.com/api/file/{fid}/info").json()
                items.append({
                    "url": f"https://pixeldrain.com/api/file/{fid}",
                    "name": r.get("name", f"{fid}.mp4"),
                    "size": r.get("size", 0)
                })
            except:
                pass
    else:
        items.append({"url": url, "name": "video.mp4", "size": 0})
    return items

async def handle_generic_logic(client, message, status, url):
    file_list = await resolve_generic_url(url)
    if not file_list:
        await status.edit("❌ No files found.")
        return

    for idx, item in enumerate(file_list, 1):
        name = re.sub(r"[^\w\-. ]", "", item["name"])
        path = DOWNLOAD_DIR / name

        await status.edit(f"Downloading: {name}")
        ok = await ytdlp_download(item["url"], path)
        if not ok or not path.exists():
            await status.edit("❌ Download failed.")
            continue

        parts = await asyncio.to_thread(split_video, path)

        for i, part in enumerate(parts, 1):
            thumb = await asyncio.to_thread(generate_thumbnail, part)

            await client.send_video(
                "me",
                str(part),
                caption=f"{name} [{i}/{len(parts)}]" if len(parts) > 1 else name,
                supports_streaming=True,
                thumb=thumb,
                progress=progress_bar,
                progress_args=(status, f"Uploading {i}/{len(parts)}")
            )

            if thumb:
                os.remove(thumb)
            os.remove(part)

        if path.exists():
            os.remove(path)

    await status.edit("✅ Done!")

# ---------------- MAIN ----------------

@app.on_message(filters.text & (filters.outgoing | filters.private))
async def handler(client, message: Message):
    text = message.text.strip()
    if not text.startswith("http"):
        return

    status = await message.reply("Analysing link...")

    shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    try:
        if "gofile.io" in text:
            await handle_gofile_logic(client, message, status, text)
        else:
            await handle_generic_logic(client, message, status, text)
    finally:
        shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)

if __name__ == "__main__":
    app.run()
