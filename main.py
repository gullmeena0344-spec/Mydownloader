import os
import re
import asyncio
import time
import logging
import subprocess
from pathlib import Path

from pyrogram import Client, filters
from pyrogram.types import Message

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")

MAX_CHUNK_SIZE = 1900 * 1024 * 1024  # ~2GB

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("BOT")

app = Client(
    "zero-disk-userbot",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING
)

# ---------------- PROGRESS ----------------

async def progress_bar(current, total, status, title):
    try:
        percent = current * 100 / total if total else 0
        await status.edit(f"{title}\n{percent:.1f}%")
    except:
        pass

# ---------------- ZERO DISK STREAM ----------------

async def stream_and_upload(url, name, client, status):
    """
    ZERO DISK:
    yt-dlp -> ffmpeg -> Telegram
    """

    cmd = [
        "bash", "-c",
        f"""
        yt-dlp -f bv*+ba/b -o - "{url}" |
        ffmpeg -i pipe:0 -map 0 -c copy -f segment
        -segment_size {MAX_CHUNK_SIZE}
        -reset_timestamps 1
        -movflags +faststart
        pipe:1
        """
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL
    )

    part = 1
    while True:
        data = await proc.stdout.read(64 * 1024)
        if not data:
            break

        await client.send_video(
            "me",
            data,
            caption=f"{name} [part {part}]",
            supports_streaming=True
        )
        part += 1

    await proc.wait()

# ---------------- HANDLER ----------------

@app.on_message(filters.text & (filters.outgoing | filters.private))
async def handler(client, message: Message):
    url = message.text.strip()
    if not url.startswith("http"):
        return

    status = await message.reply("Analyzing…")

    name = "video.mp4"

    # 🔥 ZERO DISK FOR LARGE FILES
    await status.edit("Streaming (zero disk)…")
    await stream_and_upload(url, name, client, status)

    await status.edit("✅ Done!")

if __name__ == "__main__":
    app.run()
