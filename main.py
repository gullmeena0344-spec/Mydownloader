import os
import re
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
MAX_SIZE = 1900 * 1024 * 1024

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
        "📥 Send a video URL\n"
        "✔ Streamable\n"
        "✔ Thumbnail fixed\n"
        "✔ No blue screen"
    )

# ================= UTIL =================

def clean():
    shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
    os.makedirs(DOWNLOAD_DIR)

def yt_download(url):
    out = f"{DOWNLOAD_DIR}/video.%(ext)s"
    cmd = [
        "yt-dlp",
        "-f", "bv*+ba/b",
        "--merge-output-format", "mp4",
        "--no-playlist",
        "--force-overwrites",
        "--concurrent-fragments", "4",
        "--downloader", "ffmpeg",
        "-o", out,
        url
    ]
    subprocess.run(cmd, check=True)
    return f"{DOWNLOAD_DIR}/video.mp4"

def pixeldrain_download(fid):
    r = requests.get(f"https://pixeldrain.com/api/file/{fid}", stream=True)
    path = f"{DOWNLOAD_DIR}/video.mp4"
    with open(path, "wb") as f:
        for c in r.iter_content(1024 * 1024):
            f.write(c)
    return path

def fix_mp4(input_file):
    fixed = f"{DOWNLOAD_DIR}/fixed.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-i", input_file,
            "-map", "0:v:0",
            "-map", "0:a?",
            "-c:v", "copy",
            "-c:a", "aac",
            "-movflags", "+faststart",
            "-reset_timestamps", "1",
            fixed
        ],
        check=True
    )
    return fixed

def make_thumb(video):
    thumb = f"{DOWNLOAD_DIR}/thumb.jpg"
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-i", video,
            "-vf", "thumbnail,scale=640:-1",
            "-frames:v", "1",
            thumb
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    return thumb

# ================= HANDLER =================

@app.on_message(filters.text & ~filters.command("start"))
async def handler(_, m: Message):
    clean()
    url = m.text.strip()
    msg = await m.reply("⬇ Downloading...")

    try:
        match = PIXELDRAIN_RE.search(url)
        file = pixeldrain_download(match.group(1)) if match else yt_download(url)

        if os.path.getsize(file) > MAX_SIZE:
            return await msg.edit("❌ File too large")

        await msg.edit("🛠 Fixing stream...")
        fixed = fix_mp4(file)

        thumb = make_thumb(fixed)

        await msg.edit("⬆ Uploading...")
        await m.reply_video(
            fixed,
            supports_streaming=True,
            thumb=thumb
        )

    except Exception as e:
        await msg.edit(f"❌ Error:\n`{e}`")

    clean()

# ================= RUN =================

print("Bot running...")
app.run()
