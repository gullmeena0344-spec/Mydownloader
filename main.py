import os, re, math, shutil, subprocess, requests, time
from urllib.parse import urlparse
from pyrogram import Client, filters
from pyrogram.types import Message

# ================= CONFIG =================

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
GOFILE_API_TOKEN = os.getenv("GOFILE_API_TOKEN")

DOWNLOAD_DIR = "downloads"
SPLIT_SIZE = 1900 * 1024 * 1024
COOKIES_FILE = "cookies.txt"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

ALLOWED_EXT = (".mp4", ".mkv", ".webm", ".avi", ".mov")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

PIXELDRAIN_RE = re.compile(r"https?://pixeldrain\.com/u/([A-Za-z0-9]+)")
GOFILE_RE = re.compile(r"https?://gofile\.io/d/([A-Za-z0-9]+)")
EMBED_RE = re.compile(r"/embed/")

# ================= BOT =================

app = Client(
    "downloader-bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

# ================= PROGRESS =================

def get_pb(cur, total):
    percent = (cur / total) * 100 if total else 0
    done = int(percent / 10)
    return f"[{'█'*done}{'░'*(10-done)}] {percent:.1f}%"

async def progress_func(cur, total, msg, tag, start):
    now = time.time()
    speed = cur / (now - start + 1)
    try:
        await msg.edit(
            f"**{tag}**\n"
            f"{get_pb(cur,total)}\n"
            f"`{cur/1024/1024:.2f}/{total/1024/1024:.2f} MB`\n"
            f"⚡ `{speed/1024/1024:.2f} MB/s`"
        )
    except:
        pass

# ================= HELPERS =================

def clean_url(text):
    m = re.search(r"(https?://[^\s]+)", text)
    return m.group(1) if m else None

def normalize_embed(url):
    if EMBED_RE.search(url):
        return url.replace("/embed/", "/")
    return url

def cleanup():
    shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ================= VIDEO FIX =================

def faststart_thumb(src):
    base = src.rsplit(".",1)[0]
    fixed = base + "_fixed.mp4"
    thumb = base + ".jpg"

    subprocess.run([
        "ffmpeg","-y","-i",src,
        "-movflags","+faststart","-c","copy",fixed
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    subprocess.run([
        "ffmpeg","-y","-i",fixed,
        "-ss","00:00:20","-vframes","1",thumb
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    os.remove(src)
    return fixed, thumb if os.path.exists(thumb) else None

# ================= YT-DLP =================

def ytdlp_download(url):
    url = normalize_embed(url)
    out = f"{DOWNLOAD_DIR}/%(title)s.%(ext)s"
    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--cookies", COOKIES_FILE,
        "--user-agent", UA,
        "--merge-output-format","mp4",
        "-o", out,
        url
    ]
    subprocess.run(cmd, check=True)

# ================= HANDLER =================

@app.on_message(filters.private & filters.text)
async def handler(_, m: Message):
    url = clean_url(m.text)
    if not url:
        return

    status = await m.reply("⏬ **Downloading...**")
    start = time.time()

    try:
        if PIXELDRAIN_RE.search(url) or GOFILE_RE.search(url):
            ytdlp_download(url)
        else:
            ytdlp_download(url)

        for f in os.listdir(DOWNLOAD_DIR):
            path = os.path.join(DOWNLOAD_DIR, f)
            if not path.lower().endswith(ALLOWED_EXT):
                continue

            fixed, thumb = faststart_thumb(path)
            size = os.path.getsize(fixed)

            await status.edit("⏫ **Uploading...**")
            await m.reply_video(
                fixed,
                thumb=thumb,
                supports_streaming=True,
                progress=progress_func,
                progress_args=("Uploading", start)
            )

    except Exception as e:
        await status.edit(f"❌ `{e}`")

    cleanup()

app.run()
