import os
import asyncio
import math
import shutil
from pyrogram import Client, filters
from pyrogram.types import Message
from run import GoFile

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION = os.getenv("SESSION_STRING")

DOWNLOAD_DIR = "output"
MAX_SPLIT = 2 * 1024 * 1024 * 1024  # 2GB

app = Client(
    "gofile-userbot",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION,
)

# ---------------- UTILS ---------------- #

def clean(path):
    try:
        if os.path.isfile(path):
            os.remove(path)
        elif os.path.isdir(path):
            shutil.rmtree(path)
    except Exception:
        pass


async def progress(current, total, msg, start, label):
    percent = current * 100 / total
    speed = current / (asyncio.get_event_loop().time() - start + 1)
    eta = (total - current) / speed if speed > 0 else 0
    await msg.edit_text(
        f"{label}\n"
        f"{percent:.2f}% | "
        f"{current / 1024 / 1024:.2f}MB / {total / 1024 / 1024:.2f}MB\n"
        f"Speed: {speed / 1024 / 1024:.2f} MB/s | ETA: {eta:.1f}s"
    )


def split_file(path):
    size = os.path.getsize(path)
    if size <= MAX_SPLIT:
        return [path]

    parts = []
    with open(path, "rb") as f:
        i = 0
        while True:
            chunk = f.read(MAX_SPLIT)
            if not chunk:
                break
            part = f"{path}.part{i+1}"
            with open(part, "wb") as p:
                p.write(chunk)
            parts.append(part)
            i += 1
    return parts

# ---------------- HANDLER ---------------- #

@app.on_message(filters.me & filters.regex(r"https://gofile.io/d/"))
async def gofile_handler(_, msg: Message):
    url = msg.text.strip()
    status = await msg.reply_text("📥 Downloading...")

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

                for p in parts:
                    start = asyncio.get_event_loop().time()
                    await app.send_document(
                        chat_id=msg.chat.id,
                        document=p,
                        progress=progress,
                        progress_args=(status, start, "📤 Uploading"),
                        thumb=None
                    )
                    clean(p)

                clean(full)

        await status.edit_text("✅ Done")

    except Exception as e:
        await status.edit_text(f"❌ Error: {e}")

    finally:
        clean(DOWNLOAD_DIR)

# ---------------- START ---------------- #

app.run()
