import os
import re
import time
import shutil
import subprocess
import requests
from pyrogram import Client, filters
from pyrogram.types import Message

# ================= CONFIG =================

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")

DOWNLOAD_DIR = "downloads"
MAX_SIZE = 1900 * 1024 * 1024  # 1.9GB safety

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

PIXELDRAIN_RE = re.compile(r"https?://pixeldrain\.com/u/([A-Za-z0-9]+)")

# ================= BOT =================

app = Client(
    "downloader-bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

# ================= HELP =================

@app.on_message(filters.command("start"))
async def start(_, m: Message):
    await m.reply_text(
        "📥 **Video Downloader Bot**\n\n"
        "Send any:\n"
        "• Direct video link\n"
        "• m3u8 / HLS link\n"
        "• Pixeldrain URL\n\n"
        "Streamable ✔ Thumbnail ✔ Auto cleanup ✔"
    )

# ================= UTILITIES =================

def clean():
    if os.path.exists(DOWNLOAD_DIR):
        shutil.rmtree(DOWNLOAD_DIR)
        os.makedirs(DOWNLOAD_DIR)

def progress_bar(current, total):
    percent = current * 100 / total if total else 0
    filled = int(percent // 10)
    return f"[{'█'*filled}{'░'*(10-filled)}] {percent:.1f}%"

async def upload_progress(current, total, msg):
    try:
        await msg.edit_text(
            f"⬆ Uploading\n{progress_bar(current, total)}"
        )
    except:
        pass

# ================= DOWNLOAD =================

def pixeldrain_download(file_id):
    url = f"https://pixeldrain.com/api/file/{file_id}"
    r = requests.get(url, stream=True)
    name = r.headers.get("Content-Disposition", "video.mp4").split("filename=")[-1]
    path = f"{DOWNLOAD_DIR}/{name}"

    with open(path, "wb") as f:
        for chunk in r.iter_content(1024 * 1024):
            f.write(chunk)

    return path

def ytdlp_download(url):
    out = f"{DOWNLOAD_DIR}/%(title)s.%(ext)s"
    cmd = [
        "yt-dlp",
        "-f", "bv*+ba/b",
        "--merge-output-format", "mp4",
        "--no-playlist",
        "--hls-use-mpegts",
        "--remux-video", "mp4",
        "-o", out,
        url
    ]
    subprocess.run(cmd, check=True)
    return max(
        [os.path.join(DOWNLOAD_DIR, f) for f in os.listdir(DOWNLOAD_DIR)],
        key=os.path.getsize
    )

def fix_video(file):
    fixed = file.replace(".mp4", "_fixed.mp4")
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-i", file,
            "-map", "0",
            "-c", "copy",
            "-movflags", "+faststart",
            fixed
        ],
        check=True
    )
    os.remove(file)
    return fixed

def generate_thumb(video):
    thumb = video.replace(".mp4", ".jpg")
    subprocess.run(
        ["ffmpeg", "-y", "-i", video, "-ss", "00:00:01", "-vframes", "1", thumb],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    return thumb if os.path.exists(thumb) else None

# ================= HANDLER =================

@app.on_message(filters.text & ~filters.command(["start"]))
async def downloader(_, m: Message):
    url = m.text.strip()
    status = await m.reply_text("⬇ Downloading...")

    clean()

    try:
        match = PIXELDRAIN_RE.search(url)
        if match:
            file = pixeldrain_download(match.group(1))
        else:
            file = ytdlp_download(url)

        if os.path.getsize(file) > MAX_SIZE:
            await status.edit_text("❌ File too large")
            clean()
            return

        fixed = fix_video(file)
        thumb = generate_thumb(fixed)

        await status.edit_text("⬆ Uploading...")

        await m.reply_video(
            fixed,
            thumb=thumb,
            supports_streaming=True,
            progress=upload_progress,
            progress_args=(status,)
        )

    except Exception as e:
        await status.edit_text(f"❌ Error:\n`{e}`")

    clean()

# ================= START =================

print("Bot started")
app.run()
