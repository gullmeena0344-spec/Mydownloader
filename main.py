import os
import re
import math
import asyncio
import shutil
import time
import logging
import subprocess
from pathlib import Path

from pyrogram import Client, filters
from pyrogram.types import Message
from run import GoFile

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")

DOWNLOAD_DIR = Path("output")
MAX_TG_SIZE = 1990 * 1024 * 1024
MIN_FREE_SPACE_MB = 300

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("BOT")

app = Client(
    "gofile-userbot",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING
)

def format_bytes(size):
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024:
            return f"{size:.2f} {unit}"
        size /= 1024

async def progress_bar(current, total, status_msg, action):
    try:
        now = time.time()
        if hasattr(status_msg, "last_update") and now - status_msg.last_update < 4:
            return
        status_msg.last_update = now
        await status_msg.edit(
            f"{action}\n"
            f"{current * 100 / total:.1f}%\n"
            f"{format_bytes(current)} / {format_bytes(total)}"
        )
    except:
        pass

@app.on_message(filters.text & (filters.outgoing | filters.private))
async def handler(client, message: Message):
    m = re.search(r"gofile\.io/d/([\w\-]+)", message.text)
    if not m:
        return

    if shutil.disk_usage(os.getcwd()).free < MIN_FREE_SPACE_MB * 1024 * 1024:
        return await message.reply("❌ Disk full")

    status = await message.reply("⬇️ Starting chunked download")
    gofile_id = m.group(1)
    offset = 0
    part_no = 1

    downloader = GoFile()

    while True:
        shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

        downloaded = await asyncio.to_thread(
            downloader.execute,
            dir=str(DOWNLOAD_DIR),
            content_id=gofile_id,
            num_threads=1,
            offset=offset,
            max_size=MAX_TG_SIZE
        )

        if not downloaded:
            break

        files = sorted(DOWNLOAD_DIR.rglob("*"))
        for f in files:
            if not f.is_file():
                continue

            clean = re.sub(r"[^a-zA-Z0-9.]", "_", f.name)
            new_path = f.parent / clean
            os.rename(f, new_path)

            parts = [str(new_path)]
            for p in parts:
                await client.send_document(
                    "me",
                    document=p,
                    caption=f"Part {part_no}",
                    progress=progress_bar,
                    progress_args=(status, f"⬆️ Uploading part {part_no}")
                )
                os.remove(p)

            part_no += 1

        offset += MAX_TG_SIZE
        shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)

    await status.edit("✅ Done")

app.run()
