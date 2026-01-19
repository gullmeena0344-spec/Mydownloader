import os
import sys
import asyncio
import math
import shutil
from pyrogram import Client, filters
from pyrogram.types import Message
from run import GoFile

print(">>> main.py started", flush=True)

# ---------- ENV HANDLER (FIX) ---------- #

def env(name, cast=str):
    value = os.getenv(name)
    if not value:
        print(f"❌ Missing environment variable: {name}", file=sys.stderr, flush=True)
        sys.exit(1)
    try:
        return cast(value)
    except Exception:
        print(f"❌ Invalid value for environment variable: {name}", file=sys.stderr, flush=True)
        sys.exit(1)

API_ID = env("API_ID", int)
API_HASH = env("API_HASH")
SESSION_STRING = env("SESSION_STRING")

# ---------- CONFIG ---------- #

DOWNLOAD_DIR = "output"
MAX_SPLIT = 2 * 1024 * 1024 * 1024  # 2GB

app = Client(
    name="gofile-userbot",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING,
)

# ---------- HELPERS ---------- #

def clean(path):
    try:
        if os.path.isfile(path):
            os.remove(path)
        elif os.path.isdir(path):
            shutil.rmtree(path)
    except Exception:
        pass


async def progress(current, total, message, start, label):
    percent = current * 100 / total
    speed = current / max(1, asyncio.get_event_loop().time() - start)
    eta = (total - current) / speed if speed > 0 else 0
    await message.edit_text(
        f"{label}\n"
        f"{percent:.2f}% | {current/1024/1024:.2f}MB / {total/1024/1024:.2f}MB\n"
        f"Speed: {speed/1024/1024:.2f} MB/s | ETA: {eta:.1f}s"
    )


def split_file(path):
    size = os.path.getsize(path)
    if size <= MAX_SPLIT:
        return [path]

    parts = []
    with open(path, "rb") as f:
        i = 1
        while True:
            chunk = f.read(MAX_SPLIT)
            if not chunk:
                break
            part = f"{path}.part{i}"
            with open(part, "wb") as p:
                p.write(chunk)
            parts.append(part)
            i += 1
    return parts

# ---------- HANDLER ---------- #

@app.on_message(filters.me & filters.regex(r"https://gofile.io/d/"))
async def gofile_handler(_, message: Message):
    url = message.text.strip()
    status = await message.reply_text("📥 Downloading...")

    try:
        GoFile().execute(
            dir=DOWNLOAD_DIR,
            url=url,
            num_threads=4
        )

        for root, _, files in os.walk(DOWNLOAD_DIR):
            for file in files:
                full = os.path.join(root, file)
                parts = split_file(full)

                for part in parts:
                    start = asyncio.get_event_loop().time()
                    await app.send_document(
                        chat_id="me",  # Saved Messages
                        document=part,
                        progress=progress,
                        progress_args=(status, start, "📤 Uploading"),
                        thumb=None
                    )
                    clean(part)

                clean(full)

        await status.edit_text("✅ Done")

    except Exception as e:
        await status.edit_text(f"❌ Error: {e}")

    finally:
        clean(DOWNLOAD_DIR)

# ---------- START ---------- #

print(">>> userbot starting", flush=True)
app.run()
