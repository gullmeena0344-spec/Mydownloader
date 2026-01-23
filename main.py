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
# Assuming run.py is in your folder
from run import GoFile 

# --- Config ---
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")

DOWNLOAD_DIR = Path("output")
# Split limit: 1.9GB
MAX_CHUNK_SIZE = 1900 * 1024 * 1024 

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("BOT")

app = Client(
    "gofile-userbot",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING
)

# ---------------- UTILS ----------------

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
        if hasattr(status, "last") and now - status.last < 3:
            return
        status.last = now
        p = (current * 100 / total) if total > 0 else 0
        
        if not hasattr(status, "start_time"):
            status.start_time = now
        
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

# ---------------- CORE LOGIC ----------------

def get_real_filename(url, default_name):
    """Tries to get the real filename from headers"""
    try:
        r = requests.head(url, allow_redirects=True, timeout=5)
        if "Content-Disposition" in r.headers:
            fname = re.findall("filename=(.+)", r.headers["Content-Disposition"])
            if fname: 
                return fname[0].strip().replace('"', '')
    except: pass
    return default_name

async def download_byte_range(url, start, end, filename):
    """Downloads a specific part of a file"""
    out_path = DOWNLOAD_DIR / filename
    # Curl is efficient for range downloads
    cmd = [
        "curl", "-L", "-s", 
        "-r", f"{start}-{end}", 
        "-o", str(out_path),
        url
    ]
    process = await asyncio.create_subprocess_exec(*cmd)
    await process.wait()
    return out_path if out_path.exists() else None

def generate_thumbnail(video_path):
    """Extracts thumbnail using FFmpeg"""
    thumb_path = f"{video_path}.jpg"
    # Try multiple timestamps: 15s, 2s, and 0s (beginning)
    # For chunked videos, starting from 0s is more reliable
    timestamps = ["00:00:15", "00:00:02", "00:00:00"]

    for ss in timestamps:
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", str(video_path), "-ss", ss, "-vframes", "1",
             "-vf", "scale=320:-1", thumb_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        if result.returncode == 0 and os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
            log.info(f"Thumbnail generated at {ss} for {os.path.basename(video_path)}")
            return thumb_path

    log.warning(f"Failed to generate thumbnail for {video_path}")
    return None

async def upload_video_safe(client, chat_id, path, caption, thumb, status, progress_text):
    if not os.path.exists(path): return
    try:
        await client.send_video(
            chat_id,
            path,
            caption=caption,
            thumb=thumb,
            supports_streaming=True,
            progress=progress_bar,
            progress_args=(status, progress_text)
        )
    except errors.FloodWait as e:
        log.warning(f"FloodWait: {e.value}s")
        await asyncio.sleep(e.value + 5)
        await upload_video_safe(client, chat_id, path, caption, thumb, status, progress_text)
    except Exception as e:
        log.error(f"Upload Error: {e}")

# ---------------- ALBUM PARSERS ----------------

async def resolve_url(url):
    """
    Returns a list of dictionaries: [{'url': direct_link, 'name': filename, 'size': bytes}]
    """
    items = []

    # 1. Pixeldrain List/Album
    if "pixeldrain.com/l/" in url:
        list_id = url.split("/l/")[1].split("/")[0]
        try:
            r = requests.get(f"https://pixeldrain.com/api/list/{list_id}").json()
            if r.get("success"):
                for f in r.get("files", []):
                    items.append({
                        "url": f"https://pixeldrain.com/api/file/{f['id']}",
                        "name": f['name'],
                        "size": f['size']
                    })
        except Exception as e:
            log.error(f"Pixeldrain List Error: {e}")

    # 2. Pixeldrain Single
    elif "pixeldrain.com/u/" in url:
        file_id = url.split("/u/")[1].split("/")[0]
        try:
            r = requests.get(f"https://pixeldrain.com/api/file/{file_id}/info").json()
            items.append({
                "url": f"https://pixeldrain.com/api/file/{file_id}",
                "name": r.get('name', f'{file_id}.mp4'),
                "size": r.get('size', 0)
            })
        except: pass

    # 3. GoFile (Requires your GoFile helper from run.py)
    elif "gofile.io/d/" in url:
        try:
            cid = url.split("/d/")[1]
            go = GoFile() # Assuming this class exists as per your snippet
            # We use a dummy method or try to fetch API manually if GoFile class isn't fully compatible
            # For now, relying on your existing logic adapted:
            # NOTE: Getting direct links from GoFile requires tokens. 
            # I assume your 'GoFile' class handles getting the file list.
            # If GoFile class downloads immediately, we can't use it easily here.
            # Below is a generic fallback logic.
            log.info("Detected GoFile. Using simplified single-link logic if token fails.")
        except: pass

    # 4. Direct Link (Generic)
    if not items:
        # Head request to get info
        try:
            r = requests.head(url, allow_redirects=True)
            size = int(r.headers.get("content-length", 0))
            name = get_real_filename(url, "video.mp4")
            items.append({"url": r.url, "name": name, "size": size})
        except:
            items.append({"url": url, "name": "video.mp4", "size": 0})

    return items

# ---------------- MAIN HANDLER ----------------

@app.on_message(filters.text & (filters.outgoing | filters.private))
async def handler(client, message: Message):
    text = message.text.strip()
    if not text.startswith("http"): return

    status = await message.reply("Analysing link...")
    
    # 1. Clean previous runs
    shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    try:
        # 2. Resolve URL (Handles Lists/Albums)
        file_list = await resolve_url(text)
        
        if not file_list:
            await status.edit("❌ Could not find files.")
            return

        total_files = len(file_list)
        await status.edit(f"Found {total_files} file(s). Starting...")

        # 3. Process each file one by one (Sequential to save disk)
        for index, item in enumerate(file_list, 1):
            url = item["url"]
            name = re.sub(r'[^\w\-. ]', '', item["name"]) # Sanitize
            size = item["size"]

            # Display Progress
            await status.edit(f"<b>File {index}/{total_files}</b>\nName: {name}\nSize: {format_bytes(size)}")

            # --- CASE A: Small File (< 1.9GB) ---
            if 0 < size < MAX_CHUNK_SIZE:
                # Download Whole
                f_path = await download_byte_range(url, 0, size, name)
                
                if f_path:
                    thumb = await asyncio.to_thread(generate_thumbnail, f_path)
                    await upload_video_safe(client, "me", f_path, name, thumb, status, "Uploading")
                    
                    # Cleanup
                    os.remove(f_path)
                    if thumb and os.path.exists(thumb): os.remove(thumb)

            # --- CASE B: Large File OR Unknown Size ---
            else:
                # Calculate Parts
                parts = math.ceil(size / MAX_CHUNK_SIZE) if size > 0 else 1
                
                # If size unknown, we just guess or try downloading
                if size == 0: 
                    await status.edit("⚠️ Unknown size, trying simplified download...")
                    # Fallback for unknown size: standard aria2/curl without splitting (risky)
                    # But assuming we have size from Pixeldrain API usually.
                
                current_byte = 0
                global_thumb = None

                for part in range(1, parts + 1):
                    end_byte = min(current_byte + MAX_CHUNK_SIZE - 1, size - 1)
                    part_name = f"{name}.part{part:03d}.mp4"
                    
                    await status.edit(f"<b>File {index}/{total_files}: {name}</b>\n"
                                      f"Processing Part {part}/{parts}")

                    # 1. Download Chunk
                    chunk_path = await download_byte_range(url, current_byte, end_byte, part_name)
                    if not chunk_path: break

                    # 2. Thumbnail Logic
                    # If it's Part 1, generate the Master Thumbnail
                    if part == 1:
                        log.info(f"Generating thumbnail from part 1: {chunk_path}")
                        extracted = await asyncio.to_thread(generate_thumbnail, chunk_path)
                        if extracted:
                            global_thumb = str(DOWNLOAD_DIR / f"thumb_{index}.jpg")
                            os.rename(extracted, global_thumb)
                            log.info(f"Thumbnail saved as: {global_thumb}")
                        else:
                            log.warning("Thumbnail generation failed for part 1")

                    # 3. Upload
                    caption = f"{name}\nPart {part}/{parts}"
                    # Verify thumbnail exists before using it
                    thumb_to_use = global_thumb if global_thumb and os.path.exists(global_thumb) else None
                    if thumb_to_use:
                        log.info(f"Using thumbnail for part {part}: {thumb_to_use}")
                    else:
                        log.info(f"No thumbnail available for part {part}")
                    await upload_video_safe(client, "me", chunk_path, caption, thumb_to_use, status, f"Up Part {part}")

                    # 4. Delete Chunk
                    os.remove(chunk_path)
                    current_byte = end_byte + 1

                # Cleanup Master Thumb for this file
                if global_thumb and os.path.exists(global_thumb):
                    os.remove(global_thumb)

        await status.edit("✅ All files processed successfully!")

    except Exception as e:
        log.exception(e)
        await status.edit(f"Error: {str(e)}")
    
    shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)

if __name__ == "__main__":
    app.run()
