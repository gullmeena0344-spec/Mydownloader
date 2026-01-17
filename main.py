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
    await m.reply(
        "📥 **Downloader Bot**\n\n"
        "Send:\n"
        "• Direct video link\n"
        "• m3u8 / HLS\n"
        "• Pixeldrain\n"
        "• GoFile\n\n"
        "✔ Streamable\n✔ No blue screen\n✔ Thumbnail safe"
    )

# ================= UTIL =================

def clean():
    shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
    os.makedirs(DOWNLOAD_DIR)

def progress_bar(current, total):
    percent = (current / total * 100) if total else 0
    done = int(percent / 10)
    return f"[{'█'*done}{'░'*(10-done)}] {percent:.1f}%"

async def progress_cb(current, total, msg, tag):
    if not hasattr(progress_cb, "last"):
        progress_cb.last = 0
    if time.time() - progress_cb.last < 3:
        return
    progress_cb.last = time.time()
    try:
        await msg.edit(f"**{tag}**\n{progress_bar(current, total)}")
    except:
        pass

# ================= DOWNLOADERS =================

def download_pixeldrain(fid):
    r = requests.get(f"https://pixeldrain.com/api/file/{fid}", stream=True)
    path = f"{DOWNLOAD_DIR}/video.mp4"
    with open(path, "wb") as f:
        for c in r.iter_content(1024*1024):
            f.write(c)
    return path

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
        path = os.path.join(DOWNLOAD_DIR, item["name"])
        with requests.get(item["directLink"], stream=True, headers=headers) as r:
            total = int(r.headers.get("content-length", 0))
            cur = 0
            with open(path, "wb") as f:
                for chunk in r.iter_content(1024*1024):
                    f.write(chunk)
                    cur += len(chunk)
                    await progress_cb(cur, total, status, "Downloading GoFile")

def download_ytdlp(url):
    out = f"{DOWNLOAD_DIR}/video.%(ext)s"
    parsed = urlparse(url)
    referer = f"{parsed.scheme}://{parsed.netloc}/"
    cmd = [
        "yt-dlp",
        "-f", "bv*+ba/b",
        "--merge-output-format", "mp4",
        "--no-playlist",
        "--cookies", COOKIES_FILE,
        "--user-agent", UA,
        "--add-header", f"Referer:{referer}",
        "--downloader", "ffmpeg",
        "-o", out,
        url
    ]
    subprocess.run(cmd, check=True)
    return f"{DOWNLOAD_DIR}/video.mp4"

# ================= VIDEO FIX =================

def fix_and_thumb(src):
    fixed = f"{DOWNLOAD_DIR}/fixed.mp4"
    thumb = f"{DOWNLOAD_DIR}/thumb.jpg"

    subprocess.run(
        [
            "ffmpeg", "-y",
            "-i", src,
            "-map", "0:v:0",
            "-map", "0:a?",
            "-c:v", "copy",
            "-c:a", "aac",
            "-movflags", "+faststart",
            "-reset_timestamps", "1",
            fixed
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )

    subprocess.run(
        ["ffmpeg", "-y", "-ss", "00:00:02", "-i", fixed, "-frames:v", "1", thumb],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
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
    clean()
    url = m.text.strip()
    status = await m.reply("⬇ Downloading...")

    try:
        if PIXELDRAIN_RE.search(url):
            file = download_pixeldrain(PIXELDRAIN_RE.search(url).group(1))

        elif GOFILE_RE.search(url):
            await download_gofile(GOFILE_RE.search(url).group(1), status)
            files = [os.path.join(DOWNLOAD_DIR, f) for f in os.listdir(DOWNLOAD_DIR)]

            for f in files:
                fixed, thumb = fix_and_thumb(f)
                await m.reply_video(fixed, thumb=thumb, supports_streaming=True)
            clean()
            return

        else:
            file = download_ytdlp(url)

        fixed, thumb = fix_and_thumb(file)

        if os.path.getsize(fixed) > SPLIT_SIZE:
            parts = split_file(fixed)
            for p in parts:
                await m.reply_video(p, supports_streaming=True)
        else:
            await m.reply_video(
                fixed,
                supports_streaming=True,
                thumb=thumb if thumb else None
            )

    except Exception as e:
        await status.edit(f"❌ Error:\n`{e}`")

    clean()

# ================= RUN =================

print("Bot running...")
app.run()
