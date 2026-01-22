import static_ffmpeg
    static_ffmpeg.add_paths()
except ImportError:
    print("Static FFmpeg not installed. Please add 'static-ffmpeg' to requirements.txt")

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
# Assuming run.py contains these classes
from run import GoFile, Downloader, File

# --- Config ---
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")

DOWNLOAD_DIR = Path("output")
MAX_TG_SIZE = 1990 * 1024 * 1024
MIN_FREE_SPACE_MB = 500
MAX_PART_SIZE = 1900 * 1024 * 1024  # Adjusted to ~1.9GB for max efficiency

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
        if hasattr(status, "last") and now - status.last < 1.5:
            return
        status.last = now
        p = (current * 100 / total) if total > 0 else 0
        
        # Calculate speed and ETA
        if not hasattr(status, "start_time"):
            status.start_time = now
            status.last_current = current
        
        diff = now - status.start_time
        speed = current / diff if diff > 0 else 0
        eta = (total - current) / speed if speed > 0 else 0
        
        eta_str = time.strftime("%M:%S", time.gmtime(eta)) if eta < 3600 else "Wait..."
        
        await status.edit(
            f"<b>{title}</b>\n"
            f"<code>{get_progress_bar(p)} {p:.1f}%</code>\n"
            f"<b>Size:</b> {format_bytes(current)} / {format_bytes(total)}\n"
            f"<b>Speed:</b> {format_bytes(speed)}/s\n"
            f"<b>ETA:</b> {eta_str}"
        )
    except:
        pass

def faststart_mp4(src):
    dst = src + ".fast.mp4"
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", src, "-c", "copy", "-movflags", "+faststart", dst],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        if os.path.exists(dst):
            return dst
    except:
        pass
    return src

def make_thumb(src):
    thumb = src + ".jpg"
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", src, "-ss", "00:00:01", "-vframes", "1", thumb],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
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
    # 1. Pixeldrain List (Album)
    m = re.search(r"pixeldrain\.com/l/(\w+)", url)
    if m:
        try:
            r = requests.get(f"https://pixeldrain.com/api/list/{m.group(1)}").json()
            files = []
            for idx, f in enumerate(r.get("files", []), 1):
                # Clean filename
                safe_name = re.sub(r'[\\/*?:"<>|]', "", f['name'])
                out = DOWNLOAD_DIR / f"{idx}_{safe_name}"
                
                # Use API URL
                api_url = f"https://pixeldrain.com/api/file/{f['id']}"
                subprocess.run(
                    ["aria2c", "-x", "4", "-k", "1M", "-o", str(out), api_url],
                    check=True
                )
                files.append(out)
            return files
        except Exception as e:
            log.error(f"Pixeldrain List Error: {e}")
            return []

    # 2. Pixeldrain Single File
    m = re.search(r"pixeldrain\.com/u/(\w+)", url)
    if m:
        file_id = m.group(1)
        api_url = f"https://pixeldrain.com/api/file/{file_id}"
        
        # Try to get real filename via API, fallback to ID.mp4
        try:
            info = requests.get(f"https://pixeldrain.com/api/file/{file_id}/info").json()
            filename = re.sub(r'[\\/*?:"<>|]', "", info.get("name", f"{file_id}.mp4"))
        except:
            filename = f"{file_id}.mp4"

        out = DOWNLOAD_DIR / filename
        
        # Use fewer connections (-x 4) for Pixeldrain to avoid dropping
        subprocess.run(
            ["aria2c", "-x", "4", "-k", "1M", "-o", str(filename), "-d", str(DOWNLOAD_DIR), api_url],
            check=True
        )
        return [out]

    # 3. Direct Links (MP4/MOV)
    if re.search(r"\.(mp4|mov|mkv)(\?|$)", url):
        try:
            # Try aria2c first for speed
            out_name = "direct_video.mp4"
            subprocess.run(
                ["aria2c", "-x", "8", "-s", "8", "-k", "1M", "-d", str(DOWNLOAD_DIR), url],
                check=True
            )
            return list(DOWNLOAD_DIR.glob("*"))
        except subprocess.CalledProcessError:
            # Fallback to yt-dlp if aria2c fails (e.g. headers required)
            pass

    # 4. Generic yt-dlp (YouTube, etc)
    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--merge-output-format", "mp4",
        "-o", str(DOWNLOAD_DIR / "%(title)s.%(ext)s"),
        url
    ]
    subprocess.run(cmd, check=True)
    return list(DOWNLOAD_DIR.glob("*"))

# ---------------- handler ----------------

@app.on_message(filters.text & (filters.outgoing | filters.private))
async def handler(client, message: Message):
    text = message.text.strip()
    if not text.startswith("http"): return

    if get_free_space() < MIN_FREE_SPACE_MB * 1024 * 1024:
        return await message.reply("Disk Full.")

    status = await message.reply("Starting...")
    shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    try:
        # --- GOFILE HANDLER ---
        m = re.search(r"gofile\.io/d/([\w\-]+)", text)
        if m:
            go = GoFile()
            files = go.get_files(dir=str(DOWNLOAD_DIR), content_id=m.group(1))

            for idx, f in enumerate(files, 1):
                file_name = os.path.basename(f.dest)
                real_path = normalize_path(f)
                
                # FIXED: Don't calculate size here, file doesn't exist yet!
                
                upload_queue = asyncio.Queue()
                download_done = asyncio.Event()
                loop = asyncio.get_running_loop()

                def on_part(path, part, total, size):
                    asyncio.run_coroutine_threadsafe(upload_queue.put((path, part, total)), loop)

                async def download_task():
                    try:
                        # Add a wrapper to track download progress for GoFile
                        def on_part_with_progress(path, part, total_parts, size):
                            # We can't easily get 'current' total across all parts here without more state,
                            # but we can at least signal part completion.
                            on_part(path, part, total_parts, size)

                        await asyncio.to_thread(Downloader(token=go.token).download, f, 1, on_part_with_progress)
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
                            
                            # Wait briefly for IO buffers to flush
                            await asyncio.sleep(0.5)
                            
                            if not os.path.exists(path): continue
                            
                            # NOW it is safe to check size
                            current_file_size = os.path.getsize(path)
                            parts = max(1, math.ceil(current_file_size / MAX_PART_SIZE))

                            # Split manually into chunks if bigger
                            if parts > 1:
                                for i in range(parts):
                                    start = i * MAX_PART_SIZE
                                    end = min(start + MAX_PART_SIZE, current_file_size)
                                    chunk_path = f"{path}.part{i+1}"
                                    
                                    with open(path, "rb") as src, open(chunk_path, "wb") as dst:
                                        src.seek(start)
                                        dst.write(src.read(end - start))

                                    fixed = faststart_mp4(chunk_path)
                                    thumb = make_thumb(fixed)
                                    caption = f"{file_name} [Part {i+1}/{parts}]"

                                    try:
                                        await client.send_video(
                                            "me",
                                            fixed,
                                            caption=caption,
                                            supports_streaming=True,
                                            thumb=thumb,
                                            progress=progress_bar,
                                            progress_args=(status,f"[{idx}/{len(files)}] Uploading {i+1}/{parts}")
                                        )
                                    except Exception as e:
                                        log.error(f"Upload failed: {e}")

                                    if os.path.exists(chunk_path): os.remove(chunk_path)
                                    if fixed != chunk_path and os.path.exists(fixed): os.remove(fixed)
                            else:
                                fixed = faststart_mp4(path)
                                thumb = make_thumb(fixed)
                                try:
                                    await client.send_video(
                                        "me",
                                        fixed,
                                        caption=file_name,
                                        supports_streaming=True,
                                        thumb=thumb,
                                        progress=progress_bar,
                                        progress_args=(status,f"[{idx}/{len(files)}] Uploading")
                                    )
                                except Exception as e:
                                    log.error(f"Upload failed: {e}")

                                if os.path.exists(path): os.remove(path)
                                if fixed != path and os.path.exists(fixed): os.remove(fixed)

                        else:
                            if get_task in pending: get_task.cancel()
                            if upload_queue.empty(): break

                await asyncio.gather(download_task(), upload_task())

        # --- SMART DOWNLOAD HANDLER ---
        else:
            await status.edit("Downloading...")
            files = await smart_download(text)

            if not files:
                await status.edit("No files found or download failed.")
                return

            for idx, f in enumerate(files, 1):
                f_path = str(f)
                if not os.path.exists(f_path): continue
                
                file_name = os.path.basename(f_path)
                fixed = faststart_mp4(f_path)
                
                # Check size for splitting
                file_size = os.path.getsize(fixed)
                parts = max(1, math.ceil(file_size / MAX_PART_SIZE))
                
                thumb = make_thumb(fixed)

                if parts > 1:
                    for i in range(parts):
                        start = i * MAX_PART_SIZE
                        end = min(start + MAX_PART_SIZE, file_size)
                        chunk_path = f"{fixed}.part{i+1}"
                        
                        with open(fixed, "rb") as src, open(chunk_path, "wb") as dst:
                            src.seek(start)
                            dst.write(src.read(end - start))

                        # Faststart the chunk too (helps telegram stream it)
                        fixed_chunk = faststart_mp4(chunk_path)
                        thumb_chunk = make_thumb(fixed_chunk)
                        caption = f"{file_name} [Part {i+1}/{parts}]"

                        await client.send_video(
                            "me",
                            fixed_chunk,
                            caption=caption,
                            supports_streaming=True,
                            thumb=thumb_chunk,
                            progress=progress_bar,
                            progress_args=(status,f"Uploading {i+1}/{parts}")
                        )
                        
                        if os.path.exists(chunk_path): os.remove(chunk_path)
                        if fixed_chunk != chunk_path and os.path.exists(fixed_chunk): os.remove(fixed_chunk)
                        if thumb_chunk: os.remove(thumb_chunk)
                else:
                    await client.send_video(
                        "me",
                        fixed,
                        caption=file_name,
                        supports_streaming=True,
                        thumb=thumb,
                        progress=progress_bar,
                        progress_args=(status,"Uploading")
                    )

                if os.path.exists(f_path): os.remove(f_path)
                if fixed != f_path and os.path.exists(fixed): os.remove(fixed)
                if thumb: os.remove(thumb)

        await status.edit("All done!")

    except Exception as e:
        log.exception(e)
        await status.edit(f"Error: {str(e)[:100]}")

    shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)

if __name__ == "__main__":
    app.run()
