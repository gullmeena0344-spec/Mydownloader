import os
import asyncio
import subprocess
import mimetypes
import shutil
import time

from pyrogram import Client, filters
from pyrogram.types import Message

# ================= CONFIG =================

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")

BASE_DIR = os.getcwd()
OUTPUT_DIR = os.path.join(BASE_DIR, "output")

# ================= CLIENT =================

app = Client(
    "userbot",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING,
)

# ================= UTILS =================

def clean(path):
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except:
            pass


def is_video(path):
    mime, _ = mimetypes.guess_type(path)
    return mime and mime.startswith("video")


def faststart(src):
    fixed = src + ".fast.mp4"
    cmd = [
        "ffmpeg", "-y",
        "-i", src,
        "-map", "0",
        "-c", "copy",
        "-movflags", "+faststart",
        fixed
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return fixed if os.path.exists(fixed) else src


def make_thumb(src):
    thumb = src + ".jpg"
    cmd = [
        "ffmpeg", "-y",
        "-i", src,
        "-ss", "00:00:01",
        "-vframes", "1",
        thumb
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return thumb if os.path.exists(thumb) else None


async def progress(current, total, msg, start, label):
    if total == 0:
        return
    percent = current * 100 / total
    elapsed = time.time() - start
    speed = current / elapsed if elapsed > 0 else 0
    text = (
        f"{label}\n"
        f"{percent:.1f}%\n"
        f"{current/1024/1024:.2f} MB / {total/1024/1024:.2f} MB\n"
        f"Speed: {speed/1024/1024:.2f} MB/s"
    )
    try:
        await msg.edit(text)
    except:
        pass


# ================= DOWNLOAD HANDLER =================

async def process_files(msg: Message, url: str):
    status = await msg.reply("⬇️ Starting download...")
    start = time.time()

    # Run run.py
    proc = await asyncio.create_subprocess_exec(
        "python", "run.py", url,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    await proc.communicate()

    if not os.path.exists(OUTPUT_DIR):
        return await status.edit("❌ No files downloaded")

    files = []
    for root, _, filenames in os.walk(OUTPUT_DIR):
        for f in filenames:
            files.append(os.path.join(root, f))

    if not files:
        return await status.edit("❌ Empty folder")

    await status.edit(f"📦 {len(files)} files found\nUploading one by one...")

    # ================= ONE BY ONE =================

    for idx, file in enumerate(sorted(files), start=1):
        start = time.time()
        await status.edit(f"📤 Uploading {idx}/{len(files)}")

        if is_video(file):
            fixed = faststart(file)
            thumb = make_thumb(fixed)

            await app.send_video(
                chat_id="me",
                video=fixed,
                thumb=thumb,
                supports_streaming=True,
                progress=progress,
                progress_args=(status, start, "📤 Uploading video"),
            )

            clean(fixed)
            clean(thumb)

        else:
            await app.send_document(
                chat_id="me",
                document=file,
                progress=progress,
                progress_args=(status, start, "📤 Uploading file"),
            )

        clean(file)

    shutil.rmtree(OUTPUT_DIR, ignore_errors=True)
    await status.edit("✅ All files uploaded & cleaned")


# ================= COMMAND =================

@app.on_message(filters.me & filters.regex(r"https?://"))
async def downloader(_, msg: Message):
    await process_files(msg, msg.text.strip())


# ================= START =================

app.run()
