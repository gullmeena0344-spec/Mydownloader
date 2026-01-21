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

from pyrogram import Client, filters
from pyrogram.types import Message
from run import GoFile, Downloader, File

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")

DOWNLOAD_DIR = Path("output")
MAX_TG_SIZE = 1990 * 1024 * 1024
MIN_FREE_SPACE_MB = 500
MAX_PART_SIZE = 1600 * 1024 * 1024  # 1.6 GB per part

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
    thumb = src + ".jpg"
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", src, "-ss", "00:00:01", "-vframes", "1", thumb],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True
        )
        if os.path.exists(thumb):
            return thumb
    except:
        pass
    return None

def normalize_path(p):
    return str(p.dest) if hasattr(p, "dest") else str(p)

# ---------------- SMART DOWNLOAD ----------------

async def smart_download(url):
    m = re.search(r"pixeldrain\.com/l/(\w+)", url)
    if m:
        r = requests.get(f"https://pixeldrain.com/api/list/{m.group(1)}").json()
        files = []
        for idx, f in enumerate(r.get("files", []), 1):
            out = DOWNLOAD_DIR / f"{idx}_{f['name']}"
            try:
                subprocess.run(
                    ["aria2c","-x","8","-s","8","-k","1M","-o",str(out),f"https://pixeldrain.com/api/file/{f['id']}"],
                    check=True
                )
                files.append(out)
            except subprocess.CalledProcessError as e:
                log.warning(f"Failed downloading {f['name']}: {e}")
        return files

    if re.search(r"pixeldrain\.com/u/", url):
        out = DOWNLOAD_DIR / "pixeldrain.mp4"
        subprocess.run(
            ["aria2c","-x","8","-s","8","-k","1M","-o",str(out),url],
            check=True
        )
        return [out]

    if re.search(r"\.(mp4|mov)(\?|$)", url):
        out = DOWNLOAD_DIR / "%(title)s.%(ext)s"
        try:
            subprocess.run(
                ["aria2c","-x","8","-s","8","-k","1M","-o",str(DOWNLOAD_DIR / "direct.mp4"),url],
                check=True
            )
        except subprocess.CalledProcessError:
            subprocess.run(
                ["yt-dlp","-o",str(out),"--merge-output-format","mp4",url],
                check=True
            )
        return list(DOWNLOAD_DIR.glob("*"))

    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--merge-output-format","mp4",
        "--force-generic-extractor",
        "--hls-use-mpegts",
        "--downloader","aria2c",
        "--downloader-args","aria2c:-x 8 -s 8 -k 1M",
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
        m = re.search(r"gofile\.io/d/([\w\-]+)", text)
        if m:
            go = GoFile()
            files = go.get_files(dir=str(DOWNLOAD_DIR), content_id=m.group(1))

            for idx, f in enumerate(files, 1):
                file_name = os.path.basename(f.dest)
                real_path = normalize_path(f)
                file_size = os.path.getsize(real_path)
                parts = max(1, math.ceil(file_size / MAX_PART_SIZE))

                upload_queue = asyncio.Queue()
                download_done = asyncio.Event()
                loop = asyncio.get_running_loop()

                def on_part(path, part, total, size):
                    asyncio.run_coroutine_threadsafe(upload_queue.put((path, part, total)), loop)

                async def download_task():
                    try:
                        await asyncio.to_thread(Downloader(token=go.token).download, f, 1, on_part)
                    finally:
                        download_done.set()

                async def upload_task():
                    while True:
                        get_task = asyncio.create_task(upload_queue.get())
                        wait_task = asyncio.create_task(download_done.wait())
                        done_set, pending = await asyncio.wait([get_task, wait_task], return_when=asyncio.FIRST_COMPLETED)

                        if get_task in done_set:
                            path, part_num, total_parts = await get_task
                            if wait_task in pending: wait_task.cancel()
                            if not os.path.exists(path): continue

                            # Split manually into 1.6GB chunks if bigger
                            if parts > 1:
                                for i in range(parts):
                                    start = i * MAX_PART_SIZE
                                    end = min(start + MAX_PART_SIZE, file_size)
                                    chunk_path = f"{real_path}.part{i+1}"
                                    with open(real_path, "rb") as src, open(chunk_path, "wb") as dst:
                                        src.seek(start)
                                        dst.write(src.read(end - start))

                                    fixed = faststart_mp4(chunk_path)
                                    thumb = make_thumb(fixed)  # always generate thumbnail for each part
                                    caption = f"{file_name} [Part {i+1}/{parts}]"

                                    await client.send_video(
                                        "me",
                                        fixed,
                                        caption=caption,
                                        supports_streaming=True,
                                        thumb=thumb,
                                        progress=progress_bar,
                                        progress_args=(status,f"[{idx}/{len(files)}] Uploading {i+1}/{parts}")
                                    )
                                    os.remove(chunk_path)
                            else:
                                fixed = faststart_mp4(real_path)
                                thumb = make_thumb(fixed)
                                await client.send_video(
                                    "me",
                                    fixed,
                                    caption=file_name,
                                    supports_streaming=True,
                                    thumb=thumb,
                                    progress=progress_bar,
                                    progress_args=(status,f"[{idx}/{len(files)}] Uploading")
                                )
                                os.remove(real_path)

                        else:
                            if get_task in pending: get_task.cancel()
                            if upload_queue.empty(): break

                await asyncio.gather(download_task(), upload_task())

        else:
            await status.edit("Downloading...")
            files = await smart_download(text)

            for idx, f in enumerate(files, 1):
                fixed = faststart_mp4(str(f))
                thumb = make_thumb(fixed)
                # split large files
                file_size = os.path.getsize(f)
                parts = max(1, math.ceil(file_size / MAX_PART_SIZE))
                if parts > 1:
                    for i in range(parts):
                        start = i * MAX_PART_SIZE
                        end = min(start + MAX_PART_SIZE, file_size)
                        chunk_path = f"{f}.part{i+1}"
                        with open(f, "rb") as src, open(chunk_path, "wb") as dst:
                            src.seek(start)
                            dst.write(src.read(end - start))

                        fixed_chunk = faststart_mp4(chunk_path)
                        thumb_chunk = make_thumb(fixed_chunk)
                        caption = f"{f.name} [Part {i+1}/{parts}]"

                        await client.send_video(
                            "me",
                            fixed_chunk,
                            caption=caption,
                            supports_streaming=True,
                            thumb=thumb_chunk,
                            progress=progress_bar,
                            progress_args=(status,f"Uploading {i+1}/{parts}")
                        )
                        os.remove(chunk_path)
                else:
                    await client.send_video(
                        "me",
                        fixed,
                        caption=f.name,
                        supports_streaming=True,
                        thumb=thumb,
                        progress=progress_bar,
                        progress_args=(status,"Uploading")
                    )
                os.remove(f)
                os.remove(fixed)
                if thumb: os.remove(thumb)

        await status.edit("All done!")

    except Exception as e:
        log.exception(e)
        await status.edit(f"Error: {str(e)[:100]}")

    shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)

app.run()
