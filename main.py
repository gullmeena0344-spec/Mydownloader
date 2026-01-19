import os
import asyncio
import shutil
import logging
from pathlib import Path

from pyrogram import Client, filters
from pyrogram.types import Message

from run import GoFile  # IMPORTANT: run.py MUST be lowercase

# ================= CONFIG =================

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")

DOWNLOAD_DIR = Path("output")

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# ================= USERBOT =================

app = Client(
    name="gofile-userbot",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING,
)

# ================= HELPERS =================

async def progress(current, total, message: Message, start):
    percent = (current / total) * 100
    elapsed = asyncio.get_event_loop().time() - start
    speed = current / elapsed if elapsed > 0 else 0
    eta = (total - current) / speed if speed > 0 else 0

    text = (
        f"📥 Uploading video...\n"
        f"{percent:.2f}%\n"
        f"{current / 1024 / 1024:.2f} MB / {total / 1024 / 1024:.2f} MB\n"
        f"Speed: {speed / 1024 / 1024:.2f} MB/s\n"
        f"ETA: {int(eta)} sec"
    )

    try:
        await message.edit(text)
    except:
        pass


def cleanup(path: Path):
    try:
        if path.exists():
            if path.is_file():
                path.unlink()
            else:
                shutil.rmtree(path)
    except Exception as e:
        logger.warning(f"Cleanup failed: {e}")


# ================= COMMAND =================

@app.on_message(filters.me & filters.command("gofile"))
async def gofile_handler(client: Client, message: Message):
    if len(message.command) < 2:
        await message.reply("❌ Usage:\n`/gofile <gofile_link>`")
        return

    url = message.command[1]
    status = await message.reply("🔍 Fetching files...")

    # Ensure clean start
    cleanup(DOWNLOAD_DIR)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    # Download via run.py logic
    try:
        GoFile().execute(
            dir=str(DOWNLOAD_DIR),
            url=url,
            num_threads=1,  # SAFE for justrunmy.app
        )
    except Exception as e:
        await status.edit(f"❌ Download failed:\n`{e}`")
        cleanup(DOWNLOAD_DIR)
        return

    # Collect downloaded files
    files = sorted([p for p in DOWNLOAD_DIR.rglob("*") if p.is_file()])

    if not files:
        await status.edit("❌ No files found.")
        cleanup(DOWNLOAD_DIR)
        return

    await status.edit(f"📦 {len(files)} file(s) found.\nStarting upload...")

    # Upload ONE BY ONE
    for idx, file in enumerate(files, start=1):
        upload_msg = await message.reply(
            f"📤 Uploading ({idx}/{len(files)}):\n`{file.name}`"
        )

        start_time = asyncio.get_event_loop().time()

        try:
            await client.send_video(
                chat_id="me",
                video=str(file),
                supports_streaming=True,
                progress=progress,
                progress_args=(upload_msg, start_time),
            )
        except Exception as e:
            await upload_msg.edit(f"❌ Upload failed:\n`{e}`")
            cleanup(file)
            continue

        await upload_msg.delete()
        cleanup(file)

    cleanup(DOWNLOAD_DIR)
    await status.edit("✅ Done. All videos sent to **Saved Messages**.")

# ================= START =================

app.run()
