import os
import shutil
import subprocess
import math
from pyrogram import Client, filters
from pyrogram.types import Message
from yt_dlp import YoutubeDL

# ================= CONFIG =================

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")

DOWNLOAD_DIR = "downloads"
TEMP_DIR = "temp"
TG_LIMIT = 2 * 1024 * 1024 * 1024  # 2GB

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)

app = Client(
    "userbot",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING
)

# ================= UTILS =================

def clean_disk():
    shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
    shutil.rmtree(TEMP_DIR, ignore_errors=True)
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    os.makedirs(TEMP_DIR, exist_ok=True)

def split_file(path):
    size = os.path.getsize(path)
    if size <= TG_LIMIT:
        return [path]

    parts = []
    base = os.path.basename(path)
    part_size = TG_LIMIT
    with open(path, "rb") as f:
        i = 1
        while True:
            chunk = f.read(part_size)
            if not chunk:
                break
            part_path = f"{path}.part{i}"
            with open(part_path, "wb") as p:
                p.write(chunk)
            parts.append(part_path)
            i += 1
    os.remove(path)
    return parts

def fix_video(path):
    fixed = path + ".fixed.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-i", path,
        "-map", "0", "-c", "copy",
        "-movflags", "+faststart",
        fixed
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.replace(fixed, path)

# ================= YTDLP =================

def ytdlp_download(url, msg):
    def hook(d):
        if d["status"] == "downloading":
            percent = d.get("_percent_str", "")
            speed = d.get("_speed_str", "")
            app.loop.create_task(
                msg.edit(f"⬇️ Downloading\n{percent} | {speed}")
            )

    ydl_opts = {
        "outtmpl": f"{DOWNLOAD_DIR}/%(title)s.%(ext)s",
        "merge_output_format": "mp4",
        "progress_hooks": [hook],
        "noplaylist": True,
        "quiet": True
    }

    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return ydl.prepare_filename(info)

# ================= HANDLER =================

@app.on_message(filters.private & filters.text)
async def handler(_, message: Message):
    url = message.text.strip()
    status = await message.reply("🔍 Processing...")

    try:
        clean_disk()

        # ===== GOFILE =====
        if "gofile.io" in url:
            await status.edit("📁 GoFile detected")
            subprocess.run([
                "python", "run.py", url,
                "-d", DOWNLOAD_DIR,
                "-t", "4"
            ], check=True)

            files = []
            for root, _, fns in os.walk(DOWNLOAD_DIR):
                for f in fns:
                    files.append(os.path.join(root, f))

        # ===== YTDLP =====
        else:
            await status.edit("🎬 yt-dlp source detected")
            file_path = ytdlp_download(url, status)
            fix_video(file_path)
            files = split_file(file_path)

        # ===== UPLOAD =====
        for f in files:
            await message.reply_document(
                f,
                progress=lambda c, t: app.loop.create_task(
                    status.edit(f"⬆️ Uploading {math.floor(c * 100 / t)}%")
                )
            )

        await status.edit("✅ Done")

    except Exception as e:
        await status.edit(f"❌ Error:\n{e}")

    finally:
        clean_disk()

# ================= START =================

app.run()
