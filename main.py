import os
import re
import math
import asyncio
import shutil
import time
import logging
import subprocess
from pathlib import Path
from queue import Queue
from threading import Thread

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

def get_free_space():
    return shutil.disk_usage(os.getcwd()).free

def format_bytes(size):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024:
            return f"{size:.2f} {unit}"
        size /= 1024

def get_progress_bar(percent, total=20):
    filled = int(total * percent // 100)
    bar = "█" * filled + "░" * (total - filled)
    return f"[{bar}] {percent:.1f}%"

async def progress_bar(current, total, status_msg, action_name):
    try:
        now = time.time()
        if hasattr(status_msg, "last_update") and (now - status_msg.last_update) < 2:
            return
        status_msg.last_update = now
        perc = current * 100 / total
        bar = get_progress_bar(perc)
        await status_msg.edit(
            f"{action_name}\n{bar}\n"
            f"{format_bytes(current)} / {format_bytes(total)}"
        )
    except Exception as e:
        log.debug(f"Progress update error: {e}")

# ✅ ADDED (only for thumbnail + streaming fix)
def faststart_mp4(src):
    dst = src + ".fast.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-i", src, "-c", "copy", "-movflags", "+faststart", dst],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    return dst

def make_thumb(src):
    thumb = src + ".jpg"
    subprocess.run(
        ["ffmpeg", "-y", "-i", src, "-ss", "00:00:01", "-vframes", "1", thumb],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    return thumb if os.path.exists(thumb) else None

@app.on_message(filters.text & (filters.outgoing | filters.private))
async def handler(client, message: Message):
    text = message.text

    # Determine link type
    gofile_match = re.search(r"gofile\.io/d/([\w\-]+)", text)
    direct_match = any(re.search(p, text) for p in [
        r"\.mp4$", r"\.mov$", r"\.m3u8$",
        r"^https?:\/\/(www\.)?youtube\.com",
        r"^https?:\/\/(www\.)?youtu\.be",
        r"^https?:\/\/(www\.)?pixeldrain\.com",
        r"saint2\.cr/embed"
    ])

    if not gofile_match and not direct_match:
        return

    if get_free_space() < MIN_FREE_SPACE_MB * 1024 * 1024:
        return await message.reply("Disk Full.")

    status = await message.reply("Starting Download...")
    shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    try:
        files = []

        # ---- GoFile files ----
        if gofile_match:
            go = GoFile()
            files = go.get_files(dir=str(DOWNLOAD_DIR), content_id=gofile_match.group(1))
        
        # ---- Direct / yt-dlp URLs ----
        elif direct_match:
            from yt_dlp import YoutubeDL

            ydl_opts = {
                "outtmpl": str(DOWNLOAD_DIR / "%(title)s.%(ext)s"),
                "format": "bestvideo+bestaudio/best",
                "merge_output_format": "mp4",
                "external_downloader": "aria2c",
                "external_downloader_args": "-x16 -k1M",
                "noplaylist": True,
            }

            with YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(text, download=True)
                # Normalize output files
                if "entries" in info:
                    # playlist
                    for entry in info["entries"]:
                        files.append(Path(ydl.prepare_filename(entry)))
                else:
                    files.append(Path(ydl.prepare_filename(info)))

        if not files:
            await status.edit("No files found.")
            return

        await status.edit(f"Found {len(files)} file(s). Processing...")

        # ---- Normalize everything to Path strings ----
        normalized_files = []
        for f in files:
            if isinstance(f, File):
                normalized_files.append(f.dest)
            else:
                normalized_files.append(str(f))

        for idx, file_path in enumerate(normalized_files, 1):
            if get_free_space() < MIN_FREE_SPACE_MB * 1024 * 1024:
                await status.edit("Disk Full. Stopped.")
                break

            file_name = os.path.basename(file_path)
            await status.edit(f"[{idx}/{len(normalized_files)}] Downloading: {file_name[:30]}...")

            upload_queue = asyncio.Queue()
            download_complete = asyncio.Event()
            loop = asyncio.get_running_loop()

            # For GoFile this will be called by Downloader, for direct files we just put it immediately
            def on_part_ready(path, part_num, total_parts, size):
                asyncio.run_coroutine_threadsafe(
                    upload_queue.put((path, part_num, total_parts)),
                    loop
                )

            # If GoFile: use downloader, else skip
            async def download_task():
                try:
                    if gofile_match:
                        Downloader(token=go.token).download(File(file_path), 1, on_part_ready)
                    else:
                        # For direct/yt-dlp URLs, treat as single ready part
                        on_part_ready(file_path, 1, 1, os.path.getsize(file_path))
                except Exception as e:
                    log.error(f"Download error: {e}")
                    await status.edit(f"Download failed: {str(e)[:50]}")
                finally:
                    download_complete.set()

            async def upload_task():
                while True:
                    try:
                        get_task = asyncio.create_task(upload_queue.get())
                        wait_task = asyncio.create_task(download_complete.wait())
                        done, pending = await asyncio.wait(
                            [get_task, wait_task],
                            return_when=asyncio.FIRST_COMPLETED
                        )

                        if get_task in done:
                            path, part_num, total_parts = await get_task
                            if wait_task in pending:
                                wait_task.cancel()

                            if not os.path.exists(path):
                                continue

                            caption = file_name
                            if total_parts > 1:
                                caption = f"{file_name} [Part {part_num}/{total_parts}]"

                            await status.edit(
                                f"[{idx}/{len(normalized_files)}] Uploading part {part_num}/{total_parts}..."
                            )

                            fixed = faststart_mp4(str(path))
                            thumb = make_thumb(fixed)

                            try:
                                await client.send_video(
                                    "me",
                                    video=fixed,
                                    caption=caption,
                                    supports_streaming=True,
                                    thumb=thumb,
                                    progress=progress_bar,
                                    progress_args=(
                                        status,
                                        f"[{idx}/{len(normalized_files)}] Uploading {part_num}/{total_parts}"
                                    )
                                )
                            except Exception as send_err:
                                log.error(f"Send error: {send_err}")

                            for f in (path, fixed, thumb):
                                try:
                                    if f and os.path.exists(f):
                                        os.remove(f)
                                except:
                                    pass
                        else:
                            if get_task in pending:
                                get_task.cancel()
                            if upload_queue.empty():
                                break

                    except asyncio.CancelledError:
                        break
                    except Exception as e:
                        log.error(f"Upload task error: {e}")

            await asyncio.gather(download_task(), upload_task())

        shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
        await status.edit("All done!")

    except Exception as e:
        log.error(f"Handler error: {e}")
        await status.edit(f"Error: {str(e)[:100]}")
        shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)

app.run()
