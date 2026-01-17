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

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
ALLOWED_EXT = (".mp4", ".mkv", ".webm", ".avi", ".mov")

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

PIXELDRAIN_RE = re.compile(r"https?://pixeldrain\.com/u/([A-Za-z0-9]+)")
GOFILE_RE = re.compile(r"https?://gofile\.io/d/([A-Za-z0-9]+)")

# ================= BOT =================

app = Client(
    "downloader-bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

# ================= DISK GUARD (FREE TIER SAFE) =================

def disk_guard(limit_mb=3500):
    total = 0
    for root, _, files in os.walk(DOWNLOAD_DIR):
        for f in files:
            total += os.path.getsize(os.path.join(root, f))
    if total > limit_mb * 1024 * 1024:
        shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ================= PROGRESS + SPEED =================

def get_pb(current, total):
    percent = (current / total * 100) if total else 0
    done = int(percent / 10)
    return f"[{'█'*done}{'░'*(10-done)}] {percent:.1f}%"

async def progress_func(current, total, message, tag):
    now = time.time()

    if not hasattr(progress_func, "last"):
        progress_func.last = now
        progress_func.last_bytes = current
        return

    diff = now - progress_func.last
    if diff < 4:
        return

    speed = (current - progress_func.last_bytes) / diff / 1024 / 1024
    progress_func.last = now
    progress_func.last_bytes = current

    try:
        await message.edit(
            f"**{tag}**\n"
            f"{get_pb(current, total)}\n"
            f"`{current/1024/1024:.2f}/{total/1024/1024:.2f} MB`\n"
            f"⚡ `{speed:.2f} MB/s`"
        )
    except:
        pass

# ================= HELPERS =================

def extract_clean_url(text):
    m = re.search(r'(https?://[^\s\n]+)', text)
    return m.group(1) if m else None

# ================= GOFILE =================

async def download_gofile(cid, status):
    headers = {
        "Authorization": f"Bearer {GOFILE_API_TOKEN}",
        "User-Agent": UA
    }
    info = requests.get(f"https://api.gofile.io/contents/{cid}", headers=headers).json()
    contents = info["data"]["children"]

    for item in contents.values():
        if item["type"] != "file":
            continue

        out = os.path.join(DOWNLOAD_DIR, item["name"])
        with requests.get(item["directLink"], stream=True, headers=headers) as r:
            total = int(r.headers.get("content-length", 0))
            cur = 0
            with open(out, "wb") as f:
                for chunk in r.iter_content(1024*1024):
                    f.write(chunk)
                    cur += len(chunk)
                    await progress_func(cur, total, status, "Downloading")
        disk_guard()

# ================= YT-DLP =================

def download_ytdlp(url):
    out = f"{DOWNLOAD_DIR}/video.%(ext)s"
    parsed = urlparse(url)
    referer = f"{parsed.scheme}://{parsed.netloc}/"
    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--cookies", COOKIES_FILE,
        "--user-agent", UA,
        "--add-header", f"Referer:{referer}",
        "--merge-output-format", "mp4",
        "-o", out,
        url
    ]
    subprocess.run(cmd, check=True)
    return f"{DOWNLOAD_DIR}/video.mp4"

# ================= VIDEO FIX =================

def faststart_and_thumb(src):
    fixed = src.replace(".mp4", "_fixed.mp4")
    thumb = src.replace(".mp4", ".jpg")

    subprocess.run(
        ["ffmpeg", "-y", "-i", src, "-movflags", "+faststart", "-c", "copy", fixed],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )

    subprocess.run(
        ["ffmpeg", "-y", "-ss", "00:00:02", "-i", fixed, "-vframes", "1", thumb],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )

    if os.path.exists(src):
        os.remove(src)

    return fixed, (thumb if os.path.exists(thumb) else None)

def split_file(path):
    parts = []
    size = os.path.getsize(path)
    count = math.ceil(size / SPLIT_SIZE)

    with open(path, "rb") as f:
        for i in range(count):
            part = f"{path}.part{i+1}.mp4"
            with open(part, "wb") as o:
                o.write(f.read(SPLIT_SIZE))
            parts.append(part)

    os.remove(path)
    return parts

# ================= HANDLER =================

@app.on_message(filters.private & filters.text & ~filters.command("start"))
async def handler(_, m: Message):
    disk_guard()
    url = extract_clean_url(m.text)
    if not url:
        return

    status = await m.reply("⬇ Downloading...")

    try:
        if GOFILE_RE.search(url):
            await download_gofile(GOFILE_RE.search(url).group(1), status)
            files = [os.path.join(DOWNLOAD_DIR, f) for f in os.listdir(DOWNLOAD_DIR)]

            for f in files:
                fixed, thumb = faststart_and_thumb(f)
                await m.reply_video(
                    fixed,
                    thumb=thumb,
                    supports_streaming=True,
                    progress=progress_func,
                    progress_args=(status, "Uploading")
                )
                disk_guard()
            return

        else:
            file = download_ytdlp(url)

        fixed, thumb = faststart_and_thumb(file)

        if os.path.getsize(fixed) > SPLIT_SIZE:
            for part in split_file(fixed):
                await m.reply_video(
                    part,
                    supports_streaming=True,
                    progress=progress_func,
                    progress_args=(status, "Uploading")
                )
                disk_guard()
        else:
            await m.reply_video(
                fixed,
                thumb=thumb,
                supports_streaming=True,
                progress=progress_func,
                progress_args=(status, "Uploading")
            )

    except Exception as e:
        await status.edit(f"❌ Error:\n`{e}`")

    disk_guard()

# ================= START =================

print("Bot running...")
app.run()
