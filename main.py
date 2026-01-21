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

def get_progress_bar(percent, total=20):
    filled = int(total * percent // 100)
    bar = "█" * filled + "░" * (total - filled)
    return f"[{bar}] {percent:.1f}%"

async def progress_bar(current, total, status_msg, action_name):
    try:
        now = time.time()
        if hasattr(status_msg, "last_update") and (now - status_msg.last_update) < 2:
            return
        status_msg.last_update = now
        perc = current * 100 / total
        bar = get_progress_bar(perc)
        await status_msg.edit(
            f"{action_name}\n{bar}\n"
            f"{format_bytes(current)} / {format_bytes(total)}"
        )
    except Exception as e:
        log.debug(f"Progress update error: {e}")

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
                    await status.edit(f"Download failed: {str(e)[:50]}")
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

                            if not os.path.exists(path):
                                log.error(f"File not found: {path}")
                                continue

                            caption = f"{file_name}"
                            if total_parts > 1:
                                caption = f"{file_name} [Part {part_num}/{total_parts}]"

                            await status.edit(f"[{idx}/{len(files)}] Uploading part {part_num}/{total_parts}...")

                            try:
                                result = await client.send_video(
                                    "me",
                                    video=str(path),
                                    caption=caption,
                                    supports_streaming=True,
                                    progress=progress_bar,
                                    progress_args=(status, f"[{idx}/{len(files)}] Uploading {part_num}/{total_parts}")
                                )

                                if result:
                                    log.info(f"Video sent successfully: {file_name} Part {part_num}/{total_parts}")
                                    uploaded += 1
                                else:
                                    log.error(f"Failed to send video: {path}")
                                    await status.edit(f"Failed to send: {file_name} part {part_num}")

                            except Exception as send_err:
                                log.error(f"Send error: {send_err}")
                                await status.edit(f"Send failed: {str(send_err)[:50]}")

                            try:
                                os.remove(path)
                            except:
                                pass

                        else:
                            if get_task in pending:
                                get_task.cancel()
                            if upload_queue.empty():
                                break

                    except asyncio.CancelledError:
                        break
                    except Exception as e:
                        log.error(f"Upload task error: {e}")

            await asyncio.gather(download_task(), upload_task())

        shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
        await status.edit("All done!")

    except Exception as e:
        log.error(f"Handler error: {e}")
        await status.edit(f"Error: {str(e)[:100]}")
        shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)

app.run()
