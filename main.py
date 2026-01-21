import os
import re
import asyncio
import shutil
import time
import logging
import subprocess
import requests
import math
from pathlib import Path
from pyrogram import Client, filters
from pyrogram.types import Message

# --- Config ---
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")

DOWNLOAD_DIR = Path("downloads")
TG_SPLIT_SIZE = 1500 * 1024 * 1024  # 1.5 GB
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("BOT")

app = Client("gofile-userbot", api_id=API_ID, api_hash=API_HASH, session_string=SESSION_STRING)

# ---------------- UTILS ----------------

def format_bytes(size):
    for u in ["B", "KB", "MB", "GB"]:
        if size < 1024: return f"{size:.2f} {u}"
        size /= 1024

async def edit_progress(current, total, status, title, last_time):
    now = time.time()
    if now - last_time[0] < 3: return
    last_time[0] = now
    p = (current * 100 / total) if total > 0 else 0
    bar = f"[{'█'*int(p//10)}{'░'*(10-int(p//10))}]"
    try:
        await status.edit(f"**{title}**\n`{bar}` {p:.1f}%\n{format_bytes(current)} / {format_bytes(total)}")
    except: pass

# ---------------- GOFILE FIXED LOGIC ----------------

def get_gofile_token():
    try:
        r = requests.post("https://api.gofile.io/accounts").json()
        return r["data"]["token"]
    except: return None

def get_gofile_links(content_id):
    token = get_gofile_token()
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.get(f"https://api.gofile.io/contents/{content_id}?wt=4fd6s8ada6ad", headers=headers).json()
    if r["status"] == "ok":
        return r["data"]["children"].values()
    return []

# ---------------- DOWNLOADER ----------------

async def download_file(url, path, status, title):
    last_time = [0]
    # Using aria2c for better speed and stability on 4GB disks
    cmd = [
        "aria2c", "-x", "8", "-s", "8", "-k", "1M", "--out", os.path.basename(path),
        "--dir", str(DOWNLOAD_DIR), url
    ]
    
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    
    while True:
        line = await proc.stdout.readline()
        if not line: break
        line_str = line.decode()
        # Extract progress from aria2c output
        if "(" in line_str and "%)" in line_str:
            try:
                p_str = re.search(r"\((\d+)%\)", line_str).group(1)
                await edit_progress(int(p_str), 100, status, title, last_time)
            except: pass
    await proc.wait()

# ---------------- VIDEO PROCESSING ----------------

def make_thumbnail(video):
    thumb = video + ".jpg"
    # Seek 10s to avoid blue screens
    cmd = ["ffmpeg", "-y", "-ss", "00:00:10", "-i", video, "-vframes", "1", "-vf", "scale=320:-1", thumb]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return thumb if os.path.exists(thumb) else None

def get_duration(video):
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", video]
    return float(subprocess.check_output(cmd))

# ---------------- SPLIT & UPLOAD ----------------

async def split_and_upload(file_path, status):
    file_size = os.path.getsize(file_path)
    file_name = os.path.basename(file_path)
    
    if file_size <= TG_SPLIT_SIZE:
        await status.edit(f"Uploading: {file_name}")
        thumb = make_thumbnail(file_path)
        await app.send_video("me", file_path, caption=file_name, thumb=thumb, supports_streaming=True)
        if thumb: os.remove(thumb)
        os.remove(file_path)
        return

    # Split Logic
    duration = get_duration(file_path)
    parts = math.ceil(file_size / TG_SPLIT_SIZE)
    part_dur = duration / parts

    for i in range(parts):
        part_path = f"{file_path}.part{i+1}.mp4"
        await status.edit(f"Splitting Part {i+1}...")
        
        # Stream splitting (fast and disk-safe)
        subprocess.run([
            "ffmpeg", "-y", "-ss", str(i * part_dur), "-t", str(part_dur),
            "-i", file_path, "-c", "copy", "-movflags", "+faststart", part_path
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
        thumb = make_thumbnail(part_path)
        await status.edit(f"Uploading Part {i+1} of {parts}...")
        await app.send_video("me", part_path, caption=f"{file_name} (Part {i+1}/{parts})", thumb=thumb, supports_streaming=True)
        
        # Cleanup part immediately
        os.remove(part_path)
        if thumb: os.remove(thumb)
    
    os.remove(file_path)

# ---------------- MAIN HANDLER ----------------

@app.on_message(filters.text & (filters.outgoing | filters.private))
async def handler(client, message: Message):
    url = message.text.strip()
    if not url.startswith("http"): return

    status = await message.reply("🔎 Analyzing Link...")
    
    try:
        download_list = []

        # 1. IDENTIFY LINKS
        if "pixeldrain.com/l/" in url:
            list_id = url.split("/")[-1]
            data = requests.get(f"https://pixeldrain.com/api/list/{list_id}").json()
            for f in data["files"]:
                download_list.append({
                    "url": f"https://pixeldrain.com/api/file/{f['id']}",
                    "name": f["name"]
                })
        
        elif "gofile.io/d/" in url:
            content_id = url.split("/")[-1]
            files = get_gofile_links(content_id)
            for f in files:
                download_list.append({"url": f["directLink"], "name": f["name"]})
        
        else:
            # Single file (Pixeldrain/Generic)
            if "pixeldrain.com/u/" in url:
                file_id = url.split("/")[-1]
                url = f"https://pixeldrain.com/api/file/{file_id}"
            download_list.append({"url": url, "name": "video.mp4"})

        # 2. SEQUENTIAL PROCESS (To save 4GB disk)
        for i, item in enumerate(download_list, 1):
            file_path = str(DOWNLOAD_DIR / item["name"])
            await status.edit(f"Downloading {i}/{len(download_list)}...")
            
            await download_file(item["url"], file_path, status, f"Downloading {i}/{len(download_list)}")
            
            if os.path.exists(file_path):
                await split_and_upload(file_path, status)

        await status.edit("✅ All tasks completed.")

    except Exception as e:
        log.exception(e)
        await status.edit(f"❌ Error: {str(e)[:200]}")
    finally:
        shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

app.run()
