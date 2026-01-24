import os
import re
import math
import time
import logging
import asyncio
import shutil
import subprocess
import requests
from pathlib import Path

from pyrogram import Client, filters
from pyrogram.types import Message
import yt_dlp

# --- Import GoFile (From your working script) ---
try:
    from run import GoFile, Downloader, File
except ImportError:
    GoFile = None
    Downloader = None
    print("⚠️ Warning: run.py not found. GoFile logic will fail.")

# --- Config ---
API_ID = int(os.getenv("API_ID", "12345"))
API_HASH = os.getenv("API_HASH", "YOUR_HASH")
SESSION_STRING = os.getenv("SESSION_STRING", "YOUR_SESSION")

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Min free space (MB)
MIN_FREE_SPACE_MB = 500

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("BOT")

app = Client(
    "universal-bot",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING
)

# ---------------- UTILS ----------------

def get_free_space():
    return shutil.disk_usage(os.getcwd()).free

def format_bytes(size):
    if not size: return "0B"
    power = 2**10
    n = 0
    power_labels = {0 : '', 1: 'K', 2: 'M', 3: 'G', 4: 'T'}
    while size > power:
        size /= power
        n += 1
    return f"{size:.2f} {power_labels[n]}B"

def get_progress_bar(percent, total=15):
    filled = int(total * percent // 100)
    return f"▰{'▰'*filled}{'▱'*(total-filled-1)}▱"

async def progress_hook(current, total, status, title):
    try:
        now = time.time()
        if hasattr(status, "last_update") and now - status.last_update < 3:
            return
        status.last_update = now
        
        percent = (current / total) * 100 if total > 0 else 0
        text = (
            f"<b>{title}</b>\n"
            f"<code>{get_progress_bar(percent)} {percent:.1f}%</code>\n"
            f"<b>{format_bytes(current)} / {format_bytes(total)}</b>"
        )
        await status.edit(text)
    except Exception:
        pass

# ---------------- FFMPEG HELPERS ----------------

async def generate_thumbnail(video_path):
    """Generates a JPG thumbnail."""
    thumb_path = f"{video_path}.jpg"
    if not os.path.exists(video_path): return None
    
    # Try different timestamps
    for ss in ["00:00:15", "00:00:05", "00:00:01"]:
        try:
            cmd = [
                "ffmpeg", "-y", "-i", str(video_path), 
                "-ss", ss, "-vframes", "1", str(thumb_path)
            ]
            proc = await asyncio.create_subprocess_exec(*cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            await proc.wait()
            
            if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 1000:
                return thumb_path
        except: continue
    return None

async def faststart_video(video_path):
    """Optimizes video for streaming (Faststart)."""
    if not str(video_path).lower().endswith(".mp4"):
        return video_path
        
    temp_path = f"{video_path}.temp.mp4"
    cmd = [
        "ffmpeg", "-y", "-i", str(video_path),
        "-c", "copy", "-movflags", "+faststart",
        str(temp_path)
    ]
    process = await asyncio.create_subprocess_exec(*cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    await process.wait()
    
    if os.path.exists(temp_path):
        os.remove(video_path)
        os.rename(temp_path, video_path)
    return video_path

# ==============================================================================
# 1. GOFILE LOGIC (Unchanged - Uses run.py)
# ==============================================================================

async def handle_gofile(client, message, status, url):
    try:
        if not GoFile:
            await status.edit("❌ run.py is missing!")
            return

        go = GoFile()
        m = re.search(r"gofile\.io/d/([\w\-]+)", url)
        if not m:
            await status.edit("❌ Invalid GoFile URL.")
            return

        files = go.get_files(dir=str(DOWNLOAD_DIR), content_id=m.group(1))
        
        if not files:
            await status.edit("❌ No files found.")
            return

        await status.edit(f"Found {len(files)} file(s) on GoFile.")

        for idx, file in enumerate(files, 1):
            if get_free_space() < MIN_FREE_SPACE_MB * 1024 * 1024:
                await status.edit("❌ Disk Full.")
                break

            file_name = os.path.basename(file.dest)
            await status.edit(f"[{idx}/{len(files)}] GoFile: {file_name}...")

            # Async Queue Logic
            upload_queue = asyncio.Queue()
            download_complete = asyncio.Event()
            loop = asyncio.get_running_loop()

            def on_part_ready(path, part_num, total_parts, size):
                asyncio.run_coroutine_threadsafe(
                    upload_queue.put((path, part_num, total_parts)),
                    loop
                )

            async def download_task():
                try:
                    await asyncio.to_thread(
                        Downloader(token=go.token).download, file, 1, on_part_ready
                    )
                except Exception as e:
                    log.error(f"DL error: {e}")
                finally:
                    download_complete.set()

            async def upload_task():
                while True:
                    try:
                        get_task = asyncio.create_task(upload_queue.get())
                        wait_task = asyncio.create_task(download_complete.wait())
                        
                        done, pending = await asyncio.wait(
                            [get_task, wait_task], return_when=asyncio.FIRST_COMPLETED
                        )

                        if get_task in done:
                            path, part_num, total_parts = await get_task
                            if wait_task in pending: wait_task.cancel()

                            if not os.path.exists(path): continue

                            caption = f"{file_name} [{part_num}/{total_parts}]" if total_parts > 1 else file_name
                            await status.edit(f"[{idx}/{len(files)}] Uploading Part {part_num}/{total_parts}...")

                            fixed_path = await faststart_video(str(path))
                            thumb_path = await generate_thumbnail(fixed_path)

                            try:
                                await client.send_video("me", video=fixed_path, caption=caption, supports_streaming=True, thumb=thumb_path, progress=progress_hook, progress_args=(status, f"UP: {part_num}/{total_parts}"))
                            except Exception as e: log.error(f"Send Error: {e}")

                            for f in [path, fixed_path, thumb_path]:
                                if f and os.path.exists(f): 
                                    try: os.remove(f)
                                    except: pass
                        else:
                            if get_task in pending: get_task.cancel()
                            if upload_queue.empty(): break
                    except asyncio.CancelledError: break
            
            await asyncio.gather(download_task(), upload_task())

        await status.edit("✅ GoFile Done!")
    except Exception as e:
        log.exception(e)
        await status.edit(f"GoFile Error: {e}")

# ==============================================================================
# 2. PIXELDRAIN LOGIC (Specific API + Curl)
# ==============================================================================

async def handle_pixeldrain(client, message, status, url):
    items = []
    # Resolve List or File
    try:
        if "/l/" in url:
            lid = url.split("/l/")[1].split("/")[0]
            r = requests.get(f"https://pixeldrain.com/api/list/{lid}").json()
            if r.get("success"):
                for f in r.get("files", []):
                    items.append({"url": f"https://pixeldrain.com/api/file/{f['id']}", "name": f['name']})
        elif "/u/" in url:
            fid = url.split("/u/")[1].split("/")[0]
            r = requests.get(f"https://pixeldrain.com/api/file/{fid}/info").json()
            items.append({"url": f"https://pixeldrain.com/api/file/{fid}", "name": r.get('name', f'{fid}.mp4')})
    except Exception as e:
        await status.edit(f"❌ Pixeldrain API Error: {e}")
        return

    if not items:
        await status.edit("❌ No files found on Pixeldrain.")
        return

    for idx, item in enumerate(items, 1):
        name = re.sub(r'[^\w\-. ]', '', item["name"])
        link = item["url"]
        path = DOWNLOAD_DIR / name
        
        await status.edit(f"[{idx}/{len(items)}] PD: {name}\nDownloading via Curl...")

        # Use Curl for specific Pixeldrain speed
        cmd = ["curl", "-L", "-o", str(path), link]
        proc = await asyncio.create_subprocess_exec(*cmd)
        await proc.wait()
        
        if path.exists():
            await status.edit(f"[{idx}/{len(items)}] Processing: {name}")
            final_path = await faststart_video(str(path))
            thumb = await generate_thumbnail(final_path)
            
            await status.edit(f"[{idx}/{len(items)}] Uploading: {name}")
            await client.send_video("me", str(final_path), caption=name, thumb=thumb, supports_streaming=True, progress=progress_hook, progress_args=(status, "Uploading"))
            
            # Cleanup
            try:
                os.remove(final_path)
                if thumb: os.remove(thumb)
            except: pass
        else:
            await status.edit(f"❌ Download failed for {name}")

    await status.edit("✅ Pixeldrain Done!")

# ==============================================================================
# 3. UNIVERSAL LOGIC (yt-dlp for m3u8, direct, mkv, av1)
# ==============================================================================

class YTDLProgress:
    def __init__(self, status_msg):
        self.status_msg = status_msg
        self.last_time = 0
    def hook(self, d):
        if d['status'] == 'downloading':
            now = time.time()
            if now - self.last_time > 3: 
                self.last_time = now
                total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
                downloaded = d.get('downloaded_bytes', 0)
                try:
                    asyncio.run_coroutine_threadsafe(
                        progress_hook(downloaded, total, self.status_msg, "Downloading (Universal)..."),
                        app.loop
                    )
                except: pass

async def handle_universal(client, message, status, url):
    timestamp = int(time.time())
    # % (title)s will automatically get filename from m3u8 header or direct link
    output_template = str(DOWNLOAD_DIR / f"%(title)s_{timestamp}.%(ext)s")
    
    ydl_opts = {
        'outtmpl': output_template,
        'format': 'bestvideo+bestaudio/best', # Gets best AV1/MKV/MP4
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
        'progress_hooks': [YTDLProgress(status).hook],
        # Helps with direct links that block generic user agents
        'http_headers': {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
    }

    try:
        def run_ydl():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                return ydl.prepare_filename(info)

        filepath = await asyncio.to_thread(run_ydl)
        
        if not filepath or not os.path.exists(filepath):
            await status.edit("❌ Universal Download Failed.")
            return

        filename = os.path.basename(filepath)
        await status.edit(f"✅ Downloaded: {filename}\n⚙️ Processing...")

        final_path = await faststart_video(filepath)
        thumb = await generate_thumbnail(final_path)

        await status.edit(f"⬆️ Uploading: {filename}")
        await client.send_video(
            "me",
            video=final_path,
            caption=f"{filename}\n🔗 {url}",
            thumb=thumb,
            supports_streaming=True,
            progress=progress_hook,
            progress_args=(status, "Uploading")
        )

        try:
            os.remove(final_path)
            if thumb: os.remove(thumb)
        except: pass
        
        await status.delete()

    except Exception as e:
        log.error(e)
        await status.edit(f"Universal Error: {str(e)}")

# ==============================================================================
# MAIN DISPATCHER
# ==============================================================================

@app.on_message(filters.text & (filters.outgoing | filters.private))
async def main_handler(client, message: Message):
    text = message.text.strip()
    if not text.startswith(("http://", "https://")): return

    status = await message.reply("🔍 **Analyzing Link...**")
    
    # Clean Start
    shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    try:
        # 1. GoFile
        if "gofile.io" in text:
            await handle_gofile(client, message, status, text)
        
        # 2. Pixeldrain (Don't change, use specific logic)
        elif "pixeldrain.com" in text:
            await handle_pixeldrain(client, message, status, text)
        
        # 3. Universal (yt-dlp for everything else: m3u8, direct, mkv, av1)
        else:
            await handle_universal(client, message, status, text)
            
    except Exception as e:
        log.error(e)
        await status.edit(f"Error: {e}")
    finally:
        shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)

if __name__ == "__main__":
    print("Bot Started...")
    app.run()
