import os
import re
import asyncio
import shutil
from pathlib import Path
from pyrogram import Client, filters
from pyrogram.types import Message
from run import GoFile, RangeDownloader

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")

DOWNLOAD_DIR = Path("output")
CHUNK_SIZE = 2 * 1024 * 1024 * 1024  # 2GB

app = Client(
    "gofile-userbot",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING
)

GOFILE_RE = re.compile(r"https?://gofile\.io/d/\w+", re.I)

@app.on_message(filters.text)
async def handler(client: Client, message: Message):
    m = GOFILE_RE.search(message.text or "")
    if not m:
        return

    url = m.group(0)
    status = await message.reply("⬇️ Fetching file list...")

    shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
    DOWNLOAD_DIR.mkdir(exist_ok=True)

    gf = GoFile()
    files = gf.get_files(dir=str(DOWNLOAD_DIR), url=url)

    if not files:
        return await status.edit("❌ No files found")

    await status.edit(f"📦 {len(files)} file(s) found")

    for file in files:
        await status.edit(f"⬇️ Processing `{os.path.basename(file.dest)}`")

        gf.update_token()
        rd = RangeDownloader(gf.token)

        size = rd.get_info(file.link)
        sent = 0
        part = 1

        while sent < size:
            start = sent
            end = min(start + CHUNK_SIZE - 1, size - 1)

            part_path = DOWNLOAD_DIR / f"{Path(file.dest).stem}.part{part}.mp4"

            downloaded = 0
            def progress(x):
                nonlocal downloaded
                downloaded += x

            await status.edit(
                f"⬇️ Downloading part {part}\n"
                f"{start // (1024**2)}MB → {end // (1024**2)}MB"
            )

            await asyncio.to_thread(
                rd.download_range,
                file.link,
                start,
                end,
                part_path,
                progress
            )

            await status.edit(f"⬆️ Uploading part {part}")

            await client.send_video(
                "me",
                video=str(part_path),
                supports_streaming=True
            )

            os.remove(part_path)
            sent += CHUNK_SIZE
            part += 1

    shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
    await status.edit("✅ Done")

app.run()
