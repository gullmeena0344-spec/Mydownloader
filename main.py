import os
import re
import math
import asyncio
import shutil
import time
import logging
import subprocess
from pathlib import Path
from queue import Queue
from threading import Thread

from pyrogram import Client, filters
from pyrogram.types import Message
from run import GoFile, Downloader, File

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")

DOWNLOAD_DIR = Path("output")
MAX_TG_SIZE = 1990 * 1024 * 1024
MIN_FREE_SPACE_MB = 500

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
        return await message.reply("Disk Full.")

    status = await message.reply("Starting Download...")
    shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    try:
        go = GoFile()
        files = go.get_files(dir=str(DOWNLOAD_DIR), content_id=m.group(1))

        if not files:
            await status.edit("No files found.")
            return

        await status.edit(f"Found {len(files)} file(s). Processing...")

        for idx, file in enumerate(files, 1):
            if get_free_space() < MIN_FREE_SPACE_MB * 1024 * 1024:
                await status.edit("Disk Full. Stopped.")
                break

            file_name = os.path.basename(file.dest)
            await status.edit(f"[{idx}/{len(files)}] Downloading: {file_name[:30]}...")

            upload_queue = asyncio.Queue()
            download_complete = asyncio.Event()

            def on_part_ready(path, part_num, total_parts, size):
                asyncio.run_coroutine_threadsafe(
                    upload_queue.put((path, part_num, total_parts)),
                    asyncio.get_event_loop()
                )

            async def download_task():
                try:
                    await asyncio.to_thread(
                        Downloader(token=go.token).download,
                        file,
                        1,
                        on_part_ready
                    )
                except Exception as e:
                    log.error(f"Download error: {e}")
                finally:
                    download_complete.set()

            async def upload_task():
                uploaded = 0
                while True:
                    try:
                        get_task = asyncio.create_task(upload_queue.get())
                        wait_task = asyncio.create_task(download_complete.wait())
                        done, pending = await asyncio.wait(
                            [get_task, wait_task],
                            return_when=asyncio.FIRST_COMPLETED
                        )

                        if get_task in done:
                            path, part_num, total_parts = await get_task
                            if wait_task in pending:
                                wait_task.cancel()

                            caption = f"{file_name}"
                            if total_parts > 1:
                                caption = f"{file_name} [Part {part_num}/{total_parts}]"

                            await status.edit(f"[{idx}/{len(files)}] Uploading part {part_num}/{total_parts}...")

                            await client.send_video(
                                "me",
                                video=str(path),
                                caption=caption,
                                supports_streaming=True,
                                progress=progress_bar,
                                progress_args=(status, f"Uploading {part_num}/{total_parts}")
                            )

                            os.remove(path)
                            uploaded += 1

                        else:
                            if get_task in pending:
                                get_task.cancel()
                            if upload_queue.empty():
                                break

                    except asyncio.CancelledError:
                        break
                    except Exception as e:
                        log.error(f"Upload error: {e}")

            await asyncio.gather(download_task(), upload_task())

        shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
        await status.edit("Done")

    except Exception as e:
        log.error(f"Error: {e}")
        await status.edit(f"Error: {str(e)[:100]}")
        shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)

app.run()
