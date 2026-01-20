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

def get_free_space():
    return shutil.disk_usage(os.getcwd()).free

def format_bytes(size):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024:
            return f"{size:.2f} {unit}"
        size /= 1024

async def progress_bar(current, total, status_msg, action_name):
    try:
        now = time.time()
        if hasattr(status_msg, "last_update") and (now - status_msg.last_update) < 4:
            return
        status_msg.last_update = now
        perc = current * 100 / total
        await status_msg.edit(
            f"{action_name}...\n"
            f"Progress: {perc:.1f}%\n"
            f"{format_bytes(current)} / {format_bytes(total)}"
        )
    except:
        pass

@app.on_message(filters.text & (filters.outgoing | filters.private))
async def handler(client, message: Message):
    m = re.search(r"gofile\.io/d/([\w\-]+)", message.text)
    if not m:
        return

    if get_free_space() < MIN_FREE_SPACE_MB * 1024 * 1024:
        return await message.reply("❌ Disk Full.")

    status = await message.reply("⬇️ Starting Download...")
    shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    # ---- ONLY CHANGE STARTS HERE ----
    go = GoFile()
    seen = set()

    def download():
        go.execute(
            dir=str(DOWNLOAD_DIR),
            content_id=m.group(1),
            num_threads=1
        )

    dl_task = asyncio.to_thread(download)

    async def watch_and_upload():
        while True:
            await asyncio.sleep(3)
            for f in DOWNLOAD_DIR.rglob("*"):
                if not f.is_file():
                    continue
                if f in seen:
                    continue
                if f.stat().st_size < 5 * 1024 * 1024:
                    continue

                seen.add(f)

                await client.send_document(
                    "me",
                    document=str(f),
                    caption=f.name,
                    progress=progress_bar,
                    progress_args=(status, "⬆️ Uploading")
                )

                os.remove(f)

    try:
        await asyncio.gather(dl_task, watch_and_upload())
    except:
        pass

    shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
    await status.edit("✅ Done")

app.run()
