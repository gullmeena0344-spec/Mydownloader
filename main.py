import os
import re
import asyncio
import shutil
import time
import logging
import subprocess
import requests
from pathlib import Path

from pyrogram import Client, filters
from pyrogram.types import Message
from run import GoFile, Downloader, File

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")

DOWNLOAD_DIR = Path("output")
MAX_TG_SIZE = 1990 * 1024 * 1024
MIN_FREE_SPACE_MB = 500

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("BOT")

app = Client(
    "gofile-userbot",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING
)

# ---------------- utils ----------------

def get_free_space():
    return shutil.disk_usage(os.getcwd()).free

def format_bytes(size):
    for u in ["B", "KB", "MB", "GB"]:
        if size < 1024:
            return f"{size:.2f} {u}"
        size /= 1024

def get_progress_bar(percent, total=20):
    filled = int(total * percent // 100)
    return f"[{'█'*filled}{'░'*(total-filled)}] {percent:.1f}%"

async def progress_bar(current, total, status, title):
    now = time.time()
    if hasattr(status, "last") and now - status.last < 2:
        return
    status.last = now
    p = current * 100 / total
    await status.edit(
        f"{title}\n{get_progress_bar(p)}\n"
        f"{format_bytes(current)} / {format_bytes(total)}"
    )

def faststart_mp4(src):
    dst = src + ".fast.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-i", src, "-c", "copy", "-movflags", "+faststart", dst],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    return dst if os.path.exists(dst) else src

def make_thumb(src):
    t = src + ".jpg"

    # try at 1 second
    subprocess.run(
        ["ffmpeg", "-y", "-i", src, "-ss", "00:00:01", "-vframes", "1", t],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )

    # fallback: first frame
    if not os.path.exists(t):
        subprocess.run(
            ["ffmpeg", "-y", "-i", src, "-vframes", "1", t],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )

    return t if os.path.exists(t) else None

def normalize_path(p):
    return str(p.dest) if hasattr(p, "dest") else str(p)

# ---------------- SMART DOWNLOAD ----------------

async def smart_download(url):
    # ---------- PIXELDRAIN ALBUM (API, like GoFile) ----------
    m = re.search(r"pixeldrain\.com/l/(\w+)", url)
    if m:
        r = requests.get(f"https://pixeldrain.com/api/list/{m.group(1)}").json()
        files = []

        for f in r.get("files", []):
            out = DOWNLOAD_DIR / f["name"]
            subprocess.run(
                [
                    "aria2c",
                    "-x", "8", "-s", "8", "-k", "1M",
                    "-o", str(out),
                    f"https://pixeldrain.com/api/file/{f['id']}"
                ],
                check=True
            )
            files.append(out)

        return files

    # ---------- PIXELDRAIN SINGLE ----------
    if re.search(r"pixeldrain\.com/u/", url):
        out = DOWNLOAD_DIR / "pixeldrain.mp4"
        subprocess.run(
            ["aria2c", "-x", "8", "-s", "8", "-k", "1M", "-o", str(out), url],
            check=True
        )
        return [out]

    # ---------- DIRECT MP4 / MOV (aria2 → yt-dlp fallback) ----------
    if re.search(r"\.(mp4|mov)(\?|$)", url):
        out = DOWNLOAD_DIR / "direct.mp4"
        try:
            subprocess.run(
                ["aria2c", "-x", "8", "-s", "8", "-k", "1M", "-o", str(out), url],
                check=True
            )
        except subprocess.CalledProcessError:
            subprocess.run(
                [
                    "yt-dlp",
                    "-o", str(out),
                    "--merge-output-format", "mp4",
                    url
                ],
                check=True
            )
        return [out]

    # ---------- EMBED / HLS / OTHER ----------
    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--merge-output-format", "mp4",
        "--force-generic-extractor",
        "--hls-use-mpegts",
        "--downloader", "aria2c",
        "--downloader-args", "aria2c:-x 8 -s 8 -k 1M",
        "-o", str(DOWNLOAD_DIR / "%(title)s.%(ext)s"),
        url
    ]

    subprocess.run(cmd, check=True)
    return list(DOWNLOAD_DIR.glob("*"))

# ---------------- handler ----------------

@app.on_message(filters.text & (filters.outgoing | filters.private))
async def handler(client, message: Message):
    text = message.text.strip()

    if get_free_space() < MIN_FREE_SPACE_MB * 1024 * 1024:
        return await message.reply("Disk Full.")

    status = await message.reply("Starting...")
    shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    try:
        # ---------- GOFILE ----------
        m = re.search(r"gofile\.io/d/([\w\-]+)", text)
        if m:
            go = GoFile()
            files = go.get_files(dir=str(DOWNLOAD_DIR), content_id=m.group(1))

            for f in files:
                q = asyncio.Queue()
                done = asyncio.Event()
                loop = asyncio.get_running_loop()

                def on_part(path, part, total, size):
                    asyncio.run_coroutine_threadsafe(
                        q.put((path, part, total)), loop
                    )

                async def dl():
                    try:
                        await asyncio.to_thread(
                            Downloader(token=go.token).download,
                            f, 1, on_part
                        )
                    finally:
                        done.set()

                async def up():
                    while True:
                        g = asyncio.create_task(q.get())
                        w = asyncio.create_task(done.wait())
                        done_set, _ = await asyncio.wait(
                            [g, w], return_when=asyncio.FIRST_COMPLETED
                        )

                        if g in done_set:
                            path, part, total = await g
                            real = normalize_path(path)
                            fixed = faststart_mp4(real)
                            thumb = make_thumb(fixed)

                            await client.send_video(
                                "me",
                                fixed,
                                caption=f"{os.path.basename(real)} [{part}/{total}]",
                                supports_streaming=True,
                                thumb=thumb,
                                progress=progress_bar,
                                progress_args=(status, "Uploading")
                            )

                            for x in (real, fixed, thumb):
                                if x and os.path.exists(x):
                                    os.remove(x)
                        else:
                            break

                await asyncio.gather(dl(), up())

        # ---------- SMART DOWNLOAD ----------
        else:
            await status.edit("Downloading...")
            files = await smart_download(text)

            for f in files:
                fixed = faststart_mp4(str(f))
                thumb = make_thumb(fixed)

                await client.send_video(
                    "me",
                    fixed,
                    caption=f.name,
                    supports_streaming=True,
                    thumb=thumb,
                    progress=progress_bar,
                    progress_args=(status, "Uploading")
                )

                for x in (f, fixed, thumb):
                    if x and os.path.exists(x):
                        os.remove(x)

        await status.edit("All done!")

    except Exception as e:
        log.exception(e)
        await status.edit(f"Error: {str(e)[:100]}")

    shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)

app.run()
