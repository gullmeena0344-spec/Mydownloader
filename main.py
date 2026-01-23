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
from run import GoFile, Downloader, File

# --- Config ---
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")

DOWNLOAD_DIR = Path("output")
# Telegram's Limit is ~2000MB. We split at 1900MB to be safe.
MAX_PART_SIZE = 1900 * 1024 * 1024
# If free space drops below 200MB, the bot stops to prevent OS crash.
MIN_FREE_SPACE_MB = 200 

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
        # Update progress only every 2 seconds to save CPU
        if hasattr(status, "last") and now - status.last < 2:
            return
        status.last = now
        p = (current * 100 / total) if total > 0 else 0
        
        if not hasattr(status, "start_time"):
            status.start_time = now
            status.last_current = current
        
        diff = now - status.start_time
        speed = current / diff if diff > 0 else 0
        eta = (total - current) / speed if speed > 0 else 0
        
        eta_str = time.strftime("%H:%M:%S", time.gmtime(eta))
        
        await status.edit(
            f"<b>{title}</b>\n"
            f"<code>{get_progress_bar(p)} {p:.1f}%</code>\n"
            f"<b>Size:</b> {format_bytes(current)} / {format_bytes(total)}\n"
            f"<b>Speed:</b> {format_bytes(speed)}/s\n"
            f"<b>ETA:</b> {eta_str}"
        )
    except:
        pass

# Use system FFmpeg installed via Dockerfile
FFMPEG_PATH = "ffmpeg"

def run_command(cmd):
    """Runs a command and hides output unless there is an error"""
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        return True
    except subprocess.CalledProcessError as e:
        log.error(f"FFmpeg Error: {e.stderr.decode()[:200]}")
        return False

def smart_faststart(src):
    """
    Checks if we have enough space to optimize the video.
    If not, it returns the original file to prevent disk fill-up.
    """
    if not os.path.exists(src):
        return src

    file_size = os.path.getsize(src)
    free_space = get_free_space()

    # MATH: To run faststart, we need space for the Copy + Buffer
    needed_space = file_size + (100 * 1024 * 1024)

    if free_space < needed_space:
        log.warning(f"⚠️ Low Disk: Skipping Faststart for {os.path.basename(src)}")
        return src

    dir_name = os.path.dirname(src)
    temp_output = os.path.join(dir_name, f"temp_{int(time.time())}.mp4")
    final_dst = src + ".fast.mp4"

    # Run FFmpeg
    success = run_command([
        FFMPEG_PATH, "-y", "-i", src, 
        "-c", "copy", "-movflags", "+faststart", 
        temp_output
    ])

    if success and os.path.exists(temp_output):
        # Move successful file to destination
        if os.path.exists(final_dst): os.remove(final_dst)
        os.rename(temp_output, final_dst)
        # Delete original strictly
        os.remove(src) 
        return final_dst
    
    return src

def make_thumb_safe(src):
    """
    Generates a thumbnail at 15 seconds to avoid black/blue screens.
    """
    if not os.path.exists(src): return None
    
    thumb_output = f"{src}.jpg"
    
    # Attempt 1: Take frame at 00:00:15 (Usually good content)
    success = run_command([
        FFMPEG_PATH, "-y", "-i", src, 
        "-ss", "00:00:15", "-vframes", "1", 
        thumb_output
    ])
    
    # Attempt 2: If video is short, take frame at 00:00:02
    if not success or not os.path.exists(thumb_output):
        run_command([
            FFMPEG_PATH, "-y", "-i", src, 
            "-ss", "00:00:02", "-vframes", "1", 
            thumb_output
        ])

    if os.path.exists(thumb_output):
        return thumb_output
    return None

def normalize_path(p):
    return str(p.dest) if hasattr(p, "dest") else str(p)

# ---------------- SMART DOWNLOAD ----------------

def clean_filename(name):
    # Only allow safe characters
    return re.sub(r'[^\w\-. ]', '', name)

async def smart_download(url):
    # Safety Check
    if get_free_space() < 300 * 1024 * 1024:
        log.error("Disk Full. Stopping download.")
        return []

    # 1. Pixeldrain List
    m = re.search(r"pixeldrain\.com/l/(\w+)", url)
    if m:
        try:
            r = requests.get(f"https://pixeldrain.com/api/list/{m.group(1)}").json()
            files = []
            for idx, f in enumerate(r.get("files", []), 1):
                safe_name = clean_filename(f['name'])
                out = DOWNLOAD_DIR / f"{idx}_{safe_name}"
                api_url = f"https://pixeldrain.com/api/file/{f['id']}"
                
                # --file-allocation=none prevents filling disk with zeros
                await asyncio.to_thread(
                    subprocess.run, 
                    ["aria2c", "--file-allocation=none", "-x", "4", "-k", "10M", "-o", str(out), api_url], 
                    check=True
                )
                files.append(out)
            return files
        except Exception:
            return []

    # 2. Single File
    filename = "video.mp4"
    if "pixeldrain.com/u/" in url:
        try:
            fid = re.search(r"pixeldrain\.com/u/(\w+)", url).group(1)
            url = f"https://pixeldrain.com/api/file/{fid}"
            filename = f"{fid}.mp4"
        except: pass
    elif "fn=" in url: filename = re.search(r"fn=([^&]+)", url).group(1)
    elif ".mp4" in url: 
        m = re.search(r"/([^/?#]+\.mp4)", url)
        if m: filename = m.group(1)

    filename = clean_filename(filename)
    out = DOWNLOAD_DIR / filename
    
    try:
        await asyncio.to_thread(
            subprocess.run,
            ["aria2c", "--file-allocation=none", "-x", "8", "-s", "8", "-k", "10M", 
             "-o", filename, "-d", str(DOWNLOAD_DIR), 
             "--header", "User-Agent: Mozilla/5.0", url],
            check=True
        )
        return [out]
    except Exception:
        # Fallback yt-dlp
        cmd = [
            "yt-dlp", "--no-playlist", "--restrict-filenames",
            "--merge-output-format", "mp4",
            "-o", str(DOWNLOAD_DIR / "%(title)s.%(ext)s"),
            url
        ]
        try:
            await asyncio.to_thread(subprocess.run, cmd, check=True)
        except: pass

    return list(DOWNLOAD_DIR.glob("*"))

async def upload_video_safe(client, chat_id, path, caption, thumb, status, progress_text):
    """Handles uploads with retry logic"""
    if not os.path.exists(path): return

    try:
        await client.send_video(
            chat_id,
            path,
            caption=caption,
            supports_streaming=True,
            thumb=thumb,
            progress=progress_bar,
            progress_args=(status, progress_text)
        )
    except errors.FloodWait as e:
        log.warning(f"FloodWait: Sleeping {e.value}s")
        await asyncio.sleep(e.value + 5)
        await upload_video_safe(client, chat_id, path, caption, thumb, status, progress_text)
    except Exception as e:
        log.error(f"Upload Error: {e}")

# ---------------- handler ----------------

@app.on_message(filters.text & (filters.outgoing | filters.private))
async def handler(client, message: Message):
    text = message.text.strip()
    if not text.startswith("http"): return

    if get_free_space() < MIN_FREE_SPACE_MB * 1024 * 1024:
        return await message.reply(f"Disk Full. Free: {format_bytes(get_free_space())}")

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
                
                upload_queue = asyncio.Queue()
                download_done = asyncio.Event()
                loop = asyncio.get_running_loop()

                def on_part(path, part, total, size):
                    asyncio.run_coroutine_threadsafe(upload_queue.put((path, part, total)), loop)

                async def download_task():
                    try:
                        def on_part_with_progress(path, part, total_parts, size):
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
                            
                            await asyncio.sleep(0.5)
                            if not os.path.exists(path): continue
                            
                            # --- 4GB SERVER LOGIC ---
                            f_size = os.path.getsize(path)
                            parts = max(1, math.ceil(f_size / MAX_PART_SIZE))
                            
                            # If file needs splitting but we don't have space for the copy
                            if parts > 1 and get_free_space() < MAX_PART_SIZE:
                                await status.edit(f"⚠️ File too big to split on 4GB server. Skipping: {file_name}")
                                os.remove(path)
                                continue

                            if parts > 1:
                                for i in range(parts):
                                    start = i * MAX_PART_SIZE
                                    end = min(start + MAX_PART_SIZE, f_size)
                                    chunk_path = f"{path}.part{i+1}"
                                    
                                    with open(path, "rb") as src, open(chunk_path, "wb") as dst:
                                        src.seek(start)
                                        dst.write(src.read(end - start))

                                    # Try faststart, fallback to original if no space
                                    fixed = await asyncio.to_thread(smart_faststart, chunk_path)
                                    thumb = await asyncio.to_thread(make_thumb_safe, fixed)
                                    
                                    await upload_video_safe(client, "me", fixed, f"{file_name} [{i+1}/{parts}]", thumb, status, f"Up {i+1}/{parts}")

                                    # DELETE IMMEDIATELY
                                    if os.path.exists(chunk_path): os.remove(chunk_path)
                                    if fixed != chunk_path and os.path.exists(fixed): os.remove(fixed)
                                    if thumb: os.remove(thumb)
                            else:
                                fixed = await asyncio.to_thread(smart_faststart, path)
                                thumb = await asyncio.to_thread(make_thumb_safe, fixed)
                                await upload_video_safe(client, "me", fixed, file_name, thumb, status, "Uploading")

                                if os.path.exists(path): os.remove(path)
                                if fixed != path and os.path.exists(fixed): os.remove(fixed)
                                if thumb: os.remove(thumb)

                        else:
                            if get_task in pending: get_task.cancel()
                            if upload_queue.empty(): break

                await asyncio.gather(download_task(), upload_task())

        # --- SMART DOWNLOAD HANDLER ---
        else:
            await status.edit("Downloading...")
            files = await smart_download(text)

            if not files:
                await status.edit("Download failed.")
                return

            for idx, f in enumerate(files, 1):
                f_path = str(f)
                if not os.path.exists(f_path): continue
                
                file_name = os.path.basename(f_path)
                f_size = os.path.getsize(f_path)
                
                # Check for splitting requirement vs Space
                parts = max(1, math.ceil(f_size / MAX_PART_SIZE))
                if parts > 1 and get_free_space() < MAX_PART_SIZE:
                     await status.edit(f"⚠️ Not enough space to split {file_name} (Need ~2GB free).")
                     os.remove(f_path)
                     continue

                # Run faststart only if space exists
                fixed = await asyncio.to_thread(smart_faststart, f_path)
                
                # Re-calculate size after faststart
                file_size = os.path.getsize(fixed)
                thumb = await asyncio.to_thread(make_thumb_safe, fixed)

                if parts > 1:
                    for i in range(parts):
                        start = i * MAX_PART_SIZE
                        end = min(start + MAX_PART_SIZE, file_size)
                        chunk_path = f"{fixed}.part{i+1}"
                        
                        with open(fixed, "rb") as src, open(chunk_path, "wb") as dst:
                            src.seek(start)
                            dst.write(src.read(end - start))

                        fixed_chunk = await asyncio.to_thread(smart_faststart, chunk_path)
                        thumb_chunk = await asyncio.to_thread(make_thumb_safe, fixed_chunk)
                        
                        await upload_video_safe(client, "me", fixed_chunk, f"{file_name} [{i+1}/{parts}]", thumb_chunk, status, f"Up {i+1}/{parts}")
                        
                        if os.path.exists(chunk_path): os.remove(chunk_path)
                        if fixed_chunk != chunk_path and os.path.exists(fixed_chunk): os.remove(fixed_chunk)
                        if thumb_chunk: os.remove(thumb_chunk)
                else:
                    await upload_video_safe(client, "me", fixed, file_name, thumb, status, "Uploading")

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
