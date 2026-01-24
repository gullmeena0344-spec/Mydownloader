import os
import time
import math
import logging
import asyncio
import shutil
import subprocess
from pathlib import Path
from pyrogram import Client, filters
from pyrogram.types import Message
import yt_dlp

# --- Config ---
API_ID = int(os.getenv("API_ID", "12345"))
API_HASH = os.getenv("API_HASH", "your_hash_here")
SESSION_STRING = os.getenv("SESSION_STRING", "your_session_string")

# Folder to store downloads temporarily
DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("UNIVERSAL_BOT")

app = Client(
    "universal-userbot",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING
)

# ---------------- UTILS & UI ----------------

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
        # Update only every 3 seconds to avoid floodwait
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

# ---------------- FFMPEG / FILE HELPERS ----------------

async def generate_thumbnail(video_path):
    """Generates a JPG thumbnail from the video."""
    thumb_path = f"{video_path}.jpg"
    try:
        cmd = [
            "ffmpeg", "-y", "-i", str(video_path),
            "-ss", "00:00:05", "-vframes", "1",
            str(thumb_path)
        ]
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        await proc.wait()
        
        if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 1024:
            return thumb_path
    except Exception as e:
        log.error(f"Thumb error: {e}")
    return None

async def faststart_video(video_path):
    """Moves atoms to the beginning for streaming (Faststart)."""
    # Only useful for MP4, but safe to run generic
    if not str(video_path).endswith(".mp4"):
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

# ---------------- YT-DLP WRAPPER ----------------

class YTDLProgress:
    """Hooks into yt-dlp to update Telegram message."""
    def __init__(self, status_msg):
        self.status_msg = status_msg
        self.last_time = 0

    def hook(self, d):
        if d['status'] == 'downloading':
            now = time.time()
            if now - self.last_time > 3: # Update every 3 secs
                self.last_time = now
                total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
                downloaded = d.get('downloaded_bytes', 0)
                
                # Run async edit in the background sync loop
                try:
                    asyncio.run_coroutine_threadsafe(
                        progress_hook(downloaded, total, self.status_msg, "Downloading..."),
                        app.loop
                    )
                except: pass

async def download_universal(url, status_msg):
    """
    Downloads ANYTHING using yt-dlp.
    Handles: Direct links, m3u8, mp4, mkv, av1, youtube, etc.
    """
    
    # 1. Setup paths
    timestamp = int(time.time())
    output_template = str(DOWNLOAD_DIR / f"%(title)s_{timestamp}.%(ext)s")
    
    # 2. Configure options
    ydl_opts = {
        'outtmpl': output_template,
        'format': 'bestvideo+bestaudio/best', # Download best quality
        'noplaylist': True, # Only download single video
        'quiet': True,
        'no_warnings': True,
        # 'allow_unplayable_formats': True, # helpful for AV1 sometimes
        'progress_hooks': [YTDLProgress(status_msg).hook],
        # Header manipulation for tricky direct links
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
    }

    info_dict = None
    filepath = None

    # 3. Run Download in Thread (yt-dlp is blocking)
    try:
        def run_ydl():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                return info, ydl.prepare_filename(info)

        info_dict, filepath = await asyncio.to_thread(run_ydl)

    except Exception as e:
        await status_msg.edit(f"❌ Download Failed:\n{str(e)}")
        return None

    return filepath

# ---------------- MAIN HANDLER ----------------

@app.on_message(filters.text & filters.private & filters.outgoing)
async def universal_handler(client, message: Message):
    text = message.text.strip()
    
    # Simple check for URL
    if not text.startswith(("http://", "https://")):
        return

    status = await message.reply("🔍 **Analyzing Link...**")

    try:
        # 1. DOWNLOAD
        fpath = await download_universal(text, status)
        
        if not fpath or not os.path.exists(fpath):
            await status.edit("❌ File not found after download.")
            return

        file_size = os.path.getsize(fpath)
        file_name = os.path.basename(fpath)

        await status.edit(f"✅ Downloaded: `{file_name}`\n📦 Size: {format_bytes(file_size)}\n⚙️ Processing...")

        # 2. POST-PROCESSING (Faststart for streaming)
        # Note: yt-dlp usually handles merging m3u8 to mp4 automatically.
        fpath = await faststart_video(fpath)

        # 3. THUMBNAIL
        thumb = await generate_thumbnail(fpath)

        # 4. UPLOAD
        await status.edit(f"⬆️ **Uploading:** `{file_name}`")
        
        try:
            # Telegram Limit Check (2GB for bots, 2GB/4GB for Users)
            # This is a basic upload. For >2GB, you need specific chunking logic not included here for brevity.
            await client.send_video(
                chat_id="me", # Sends to Saved Messages
                video=fpath,
                caption=f"🎥 **{file_name}**\n🔗 `{text}`",
                thumb=thumb,
                supports_streaming=True, # Allows streaming inside telegram
                progress=progress_hook,
                progress_args=(status, "Uploading")
            )
            await status.delete()
        except Exception as e:
            await status.edit(f"❌ Upload Error: {e}")
            log.error(e)

        # 5. CLEANUP
        try:
            os.remove(fpath)
            if thumb: os.remove(thumb)
        except: pass

    except Exception as e:
        await status.edit(f"❌ Critical Error: {e}")
        log.error(e)

if __name__ == "__main__":
    print("Bot Started...")
    app.run()
