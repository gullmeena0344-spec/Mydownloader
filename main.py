import os
import sys
import shutil
import asyncio
import subprocess
from pyrogram import Client, filters
from pyrogram.types import Message
from yt_dlp import YoutubeDL

# ================= CONFIG =================

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")

DOWNLOAD_DIR = "downloads"
TEMP_DIR = "temp"
MAX_SIZE = 2 * 1024 * 1024 * 1024  # 2GB

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)

# ================= CLEANUP =================

def clean_dirs():
    shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
    shutil.rmtree(TEMP_DIR, ignore_errors=True)
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    os.makedirs(TEMP_DIR, exist_ok=True)

# ================= SPLIT =================

def split_file(path):
    parts = []
    size = os.path.getsize(path)
    if size <= MAX_SIZE:
        return [path]

    base = os.path.splitext(path)[0]
    with open(path, "rb") as f:
        i = 1
        while True:
            chunk = f.read(MAX_SIZE)
            if not chunk:
                break
            part = f"{base}.part{i}.mp4"
            with open(part, "wb") as pf:
                pf.write(chunk)
            parts.append(part)
            i += 1

    os.remove(path)
    return parts

# ================= YT-DLP =================

def ytdlp_download(url):
    ydl_opts = {
        "outtmpl": f"{DOWNLOAD_DIR}/%(title)s.%(ext)s",
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [],
        "logger": None,
        "noplaylist": True,
    }

    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return ydl.prepare_filename(info)

# ================= GOFILE =================

def gofile_download(url):
    subprocess.run(
        [
            sys.executable,
            "run.py",
            url,
            "-d",
            DOWNLOAD_DIR,
            "-t",
            "4"
        ],
        check=True
    )

    files = []
    for root, _, names in os.walk(DOWNLOAD_DIR):
        for n in names:
            files.append(os.path.join(root, n))

    if not files:
        raise Exception("GoFile download failed")

    return files

# ================= BOT =================

app = Client(
    "userbot",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING
)

@app.on_message(filters.me & filters.text)
async def downloader(_, msg: Message):
    url = msg.text.strip()
    status = await msg.reply("⬇️ Downloading...")

    try:
        clean_dirs()

        if "gofile.io" in url:
            files = gofile_download(url)
        else:
            file = ytdlp_download(url)
            files = split_file(file)

        for f in files:
            await msg.reply_document(
                f,
                caption=os.path.basename(f),
                progress=lambda c, t: asyncio.get_event_loop().create_task(
                    status.edit_text(f"⬆️ Uploading {c * 100 // t}%")
                )
            )

        await status.edit("✅ Done")

    except Exception as e:
        await status.edit(f"❌ Error:\n{e}")

    finally:
        clean_dirs()

app.run()
