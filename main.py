import os
import asyncio
import shutil
import time
import logging
import subprocess
import re
from pathlib import Path
from pyrogram import Client, filters
from pyrogram.types import Message
from run import GoFile, Downloader, File

# --- Config ---
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
SESSION_STRING = os.getenv("SESSION_STRING", "")

BASE_OUTPUT_DIR = Path("output")
MIN_FREE_SPACE_MB = 400

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("BOT")

app = Client("gofile-userbot", api_id=API_ID, api_hash=API_HASH, session_string=SESSION_STRING)

# ---------------- UTILS ----------------

def get_free_space():
    return shutil.disk_usage(os.getcwd()).free

def format_bytes(size):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024: return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} TB"

def get_progress_bar(percent, total=20):
    filled = int(total * percent // 100)
    bar = "█" * filled + "░" * (total - filled)
    return f"[{bar}] {percent:.1f}%"

async def progress_bar(current, total, status_msg, action_name):
    try:
        now = time.time()
        if hasattr(status_msg, "last_update") and (now - status_msg.last_update) < 2: return
        status_msg.last_update = now
        perc = (current * 100 / total) if total > 0 else 0
        bar = get_progress_bar(perc)
        await status_msg.edit(f"{action_name}\n{bar}\n{format_bytes(current)} / {format_bytes(total)}")
    except: pass

# --- IMPROVED MEDIA HANDLING ---

def get_video_duration(src):
    """Get video duration in seconds using ffprobe."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", 
             "-of", "default=noprint_wrappers=1:nokey=1", src],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        return float(result.stdout.strip())
    except:
        return 0

def make_thumb(src):
    """
    Smart Thumbnail Generator:
    1. Gets duration.
    2. Takes screenshot at 20% of the video (skips intro).
    3. Resizes to width 320px (Crucial for Telegram).
    """
    thumb_path = src + ".jpg"
    
    # 1. Get Duration to calculate percentage
    duration = get_video_duration(src)
    
    # Default to 5 seconds if duration fails, otherwise 20% of video
    seek_time = "00:00:05" 
    if duration > 0:
        seconds = int(duration * 0.20) # 20% mark
        seek_time = str(seconds)

    try:
        # 2. Run FFmpeg with SCALE filter
        # -ss comes AFTER -i for accuracy in this context, or before for speed.
        # We put it before for speed, but rely on the calculated seconds.
        cmd = [
            "ffmpeg", "-y", 
            "-ss", seek_time,      # Seek to calculated time
            "-i", src, 
            "-vframes", "1",       # 1 frame only
            "-vf", "scale=320:-1", # Resize width to 320px (keep aspect ratio)
            "-q:v", "5",           # JPEG quality (1-31, lower is better)
            thumb_path
        ]
        
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        
        if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
            return thumb_path
            
    except Exception as e:
        log.error(f"Thumb generation failed: {e}")
    
    return None

def faststart_mp4(src):
    dst = src + ".fast.mp4"
    try:
        subprocess.run(["ffmpeg", "-y", "-i", src, "-c", "copy", "-movflags", "+faststart", dst],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        if os.path.exists(dst):
            if os.path.exists(src): os.remove(src)
            return dst
    except: pass
    return src

# ---------------- 1. GOFILE HANDLER ----------------

async def handle_gofile(client, status, content_id, download_path):
    go = GoFile()
    files = go.get_files(dir=str(download_path), content_id=content_id)
    if not files: return await status.edit("GoFile: No files found.")

    for idx, file in enumerate(files, 1):
        file_name = os.path.basename(file.dest)
        upload_queue = asyncio.Queue()
        download_complete = asyncio.Event()
        loop = asyncio.get_running_loop()

        def on_part_ready(path, p_num, t_parts, size):
            asyncio.run_coroutine_threadsafe(upload_queue.put((path, p_num, t_parts)), loop)

        async def download_task():
            try: await asyncio.to_thread(Downloader(token=go.token).download, file, 1, on_part_ready)
            finally: download_complete.set()

        async def upload_task():
            while True:
                get_task = asyncio.create_task(upload_queue.get())
                wait_task = asyncio.create_task(download_complete.wait())
                done, pending = await asyncio.wait([get_task, wait_task], return_when=asyncio.FIRST_COMPLETED)

                if get_task in done:
                    path, part_num, total_parts = await get_task
                    if wait_task in pending: wait_task.cancel()
                    
                    fixed = faststart_mp4(str(path))
                    thumb = make_thumb(fixed) # Uses new smart thumb
                    
                    caption = file_name if total_parts == 1 else f"{file_name} [Part {part_num}/{total_parts}]"
                    
                    try:
                        await client.send_video("me", video=fixed, caption=caption, thumb=thumb, supports_streaming=True,
                                             progress=progress_bar, progress_args=(status, f"Uploading Part {part_num}"))
                    except Exception as e: log.error(f"Upload fail: {e}")
                    
                    for f in [fixed, thumb]:
                        if f and os.path.exists(f): os.remove(f)
                else:
                    if get_task in pending: get_task.cancel()
                    if upload_queue.empty(): break

        await asyncio.gather(download_task(), upload_task())

# ---------------- 2. PIXELDRAIN HANDLER ----------------

async def handle_pixeldrain(client, status, url, download_path):
    await status.edit("Pixeldrain: Processing...")
    match = re.search(r"pixeldrain\.com/u/([a-zA-Z0-9]+)", url)
    if not match:
        return await status.edit("❌ Invalid Pixeldrain URL.")
    
    file_id = match.group(1)
    direct_url = f"https://pixeldrain.com/api/file/{file_id}"
    file_name = f"{file_id}.mp4"
    await handle_direct(client, status, direct_url, download_path, custom_filename=file_name)

# ---------------- 3. YT-DLP HANDLER ----------------

async def handle_ytdlp(client, status, url, download_path):
    await status.edit("YT-DLP: Downloading...")
    out_tpl = str(download_path / "%(title)s.%(ext)s")
    
    cmd = ["yt-dlp", "-o", out_tpl, "--merge-output-format", "mp4", url]
    
    process = await asyncio.to_thread(subprocess.run, cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    if process.returncode != 0:
        log.error(f"YT-DLP Error: {process.stderr}")
        return await status.edit(f"❌ Download Failed.")
    
    files_found = list(download_path.glob("*"))
    if not files_found: return await status.edit("❌ No files found.")

    for f in files_found:
        if f.suffix.lower() in [".mp4", ".mkv", ".mov", ".webm"]:
            fixed = faststart_mp4(str(f))
            thumb = make_thumb(fixed) # Uses new smart thumb
            try:
                await client.send_video("me", video=fixed, caption=f.name, thumb=thumb, supports_streaming=True,
                                     progress=progress_bar, progress_args=(status, "Uploading Video"))
            except Exception as e: log.error(e)
            for x in [fixed, thumb]:
                if x and os.path.exists(str(x)): os.remove(str(x))

# ---------------- 4. DIRECT LINK HANDLER ----------------

async def handle_direct(client, status, url, download_path, custom_filename=None):
    await status.edit("Direct Link: Downloading...")
    
    if custom_filename:
        file_name = custom_filename
    else:
        raw_name = url.split("/")[-1].split("?")[0]
        file_name = raw_name if raw_name else "video.mp4"
    
    file_obj = File(url, str(download_path / file_name))
    upload_queue = asyncio.Queue()
    download_complete = asyncio.Event()
    loop = asyncio.get_running_loop()

    def on_part_ready(p, p_num, t_parts, sz):
        asyncio.run_coroutine_threadsafe(upload_queue.put((p, p_num, t_parts)), loop)

    async def dl_task():
        try: await asyncio.to_thread(Downloader(token="").download, file_obj, 1, on_part_ready)
        finally: download_complete.set()

    async def up_task():
        while True:
            get_t = asyncio.create_task(upload_queue.get())
            wait_t = asyncio.create_task(download_complete.wait())
            done, _ = await asyncio.wait([get_t, wait_t], return_when=asyncio.FIRST_COMPLETED)
            if get_t in done:
                path, p_num, t_parts = await get_t
                fixed = faststart_mp4(str(path))
                thumb = make_thumb(fixed) # Uses new smart thumb
                try:
                    await client.send_video("me", video=fixed, caption=f"{file_name} P{p_num}", thumb=thumb,
                                         supports_streaming=True, progress=progress_bar, progress_args=(status, "Uploading Part"))
                except Exception as e: log.error(e)
                for f in [fixed, thumb]:
                    if f and os.path.exists(str(f)): os.remove(str(f))
            else: break

    await asyncio.gather(dl_task(), up_task())

# ---------------- MAIN ROUTER ----------------

@app.on_message(filters.text & (filters.outgoing | filters.private))
async def main_handler(client, message: Message):
    text = message.text.strip()
    if not text.startswith("http"): return
    
    if get_free_space() < MIN_FREE_SPACE_MB * 1024 * 1024:
        return await message.reply("Disk Full.")

    status = await message.reply("Analyzing Link...")
    unique_dir = BASE_OUTPUT_DIR / str(message.id)
    shutil.rmtree(unique_dir, ignore_errors=True)
    unique_dir.mkdir(parents=True, exist_ok=True)

    try:
        if "gofile.io/d/" in text:
            content_id = text.split("/")[-1]
            await handle_gofile(client, status, content_id, unique_dir)
        elif "pixeldrain.com" in text:
            await handle_pixeldrain(client, status, text, unique_dir)
        elif any(x in text for x in ["youtube", "twitter", "instagram", "youtu.be"]):
            await handle_ytdlp(client, status, text, unique_dir)
        else:
            await handle_direct(client, status, text, unique_dir)
            
        await status.edit("✅ All finished.")
    except Exception as e:
        log.exception(e)
        await status.edit(f"❌ Error: {str(e)[:150]}")
    finally:
        shutil.rmtree(unique_dir, ignore_errors=True)

if __name__ == "__main__":
    app.run()
