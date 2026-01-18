import os, re, shutil, subprocess, time, math, asyncio
from pyrogram import Client, filters
from pyrogram.types import Message

# ================= CONFIG =================

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")

DOWNLOAD_DIR = "downloads"
COOKIES_FILE = "cookies.txt"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
MAX_SIZE = 1900 * 1024 * 1024

VIDEO_EXT = (".mp4", ".mkv", ".webm", ".avi", ".mov")

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ================= BOT =================

app = Client(
    "downloader-bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

queue_lock = asyncio.Lock()

# ================= UTILITIES =================

def cleanup():
    shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

def extract_url(text):
    m = re.search(r"(https?://[^\s]+)", text)
    return m.group(1) if m else None

def normalize_embed(url):
    return url.replace("/embed/", "/") if "/embed/" in url else url

# ================= VIDEO CHECK =================

def is_real_video(path):
    if not path.lower().endswith(VIDEO_EXT):
        return False
    if not os.path.exists(path) or os.path.getsize(path) < 500 * 1024:
        return False
    try:
        subprocess.check_output(
            ["ffprobe", "-v", "error", "-select_streams", "v",
             "-show_entries", "stream=index", "-of", "csv=p=0", path],
            stderr=subprocess.DEVNULL
        )
        return True
    except:
        return False

# ================= PROGRESS =================

async def progress_func(cur, total, msg, tag, start):
    speed = cur / max(1, time.time() - start)
    try:
        await msg.edit(
            f"**{tag}**\n"
            f"`{cur/1024/1024:.2f} / {total/1024/1024:.2f} MB`\n"
            f"⚡ `{speed/1024/1024:.2f} MB/s`"
        )
    except:
        pass

# ================= YT-DLP =================

def download_ytdlp(url):
    url = normalize_embed(url)
    out = f"{DOWNLOAD_DIR}/%(title)s.%(ext)s"

    base_cmd = [
        "yt-dlp",
        "--no-playlist",
        "--user-agent", UA,
        "--merge-output-format", "mp4",
        "-o", out,
        url
    ]

    if os.path.exists(COOKIES_FILE):
        try:
            subprocess.run(
                ["yt-dlp", "--cookies", COOKIES_FILE, *base_cmd[1:]],
                check=True
            )
            return
        except subprocess.CalledProcessError:
            pass

    subprocess.run(base_cmd, check=True)

# ================= VIDEO FIX =================

def fix_and_thumb(src):
    fixed = src.rsplit(".", 1)[0] + "_fixed.mp4"
    thumb = src.rsplit(".", 1)[0] + ".jpg"

    subprocess.run(
        ["ffmpeg", "-y", "-i", src, "-movflags", "+faststart", "-c", "copy", fixed],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )

    subprocess.run(
        ["ffmpeg", "-y", "-i", fixed, "-ss", "00:00:20", "-vframes", "1", thumb],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )

    if not os.path.exists(fixed):
        return None, None

    os.remove(src)
    return fixed, thumb if os.path.exists(thumb) else None

# ================= SPLIT =================

def split_video(path):
    parts = []
    size = os.path.getsize(path)
    count = math.ceil(size / MAX_SIZE)

    with open(path, "rb") as f:
        for i in range(count):
            part = f"{path}.part{i+1}.mp4"
            with open(part, "wb") as o:
                o.write(f.read(MAX_SIZE))
            parts.append(part)

    os.remove(path)
    return parts

# ================= HANDLER =================

@app.on_message(filters.private & filters.text)
async def handler(_, m: Message):
    url = extract_url(m.text)
    if not url:
        return

    async with queue_lock:
        status = await m.reply("⏬ **Queued & Downloading...**")
        start = time.time()

        cleanup()

        try:
            download_ytdlp(url)
            found = False

            for f in os.listdir(DOWNLOAD_DIR):
                path = os.path.join(DOWNLOAD_DIR, f)

                if not is_real_video(path):
                    continue

                fixed, thumb = fix_and_thumb(path)
                if not fixed:
                    continue

                files = (
                    split_video(fixed)
                    if os.path.getsize(fixed) > MAX_SIZE
                    else [fixed]
                )

                found = True
                for i, file in enumerate(files, 1):
                    await status.edit(f"⏫ **Uploading part {i}/{len(files)}**")
                    await m.reply_video(
                        video=file,
                        thumb=thumb if thumb and os.path.exists(thumb) else None,
                        supports_streaming=True,
                        progress=progress_func,
                        progress_args=(status, "Uploading", start)
                    )
                    os.remove(file)

            if not found:
                await status.edit("❌ No valid video found")

        except Exception as e:
            await status.edit(f"❌ `{e}`")

        cleanup()

# ================= START =================

print("Bot started")
app.run()
