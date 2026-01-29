import os
import re
import asyncio
import shutil
import time
import logging
import subprocess
from pathlib import Path

from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.errors import FloodWait

# --- IMPORT MODULES ---
try:
    from run import GoFile, Downloader
except ImportError:
    GoFile = None
    print("âŒ Warning: run.py not found.")

try:
    from extras import StreamDownloader
except ImportError:
    StreamDownloader = None
    print("âŒ Warning: extras.py not found.")

# --- CONFIG ---
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
SESSION_STRING = os.getenv("SESSION_STRING", "")

DOWNLOAD_DIR = Path("output")
MAX_CHUNK_SIZE = 1900 * 1024 * 1024  # 1.9 GB

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("UserBot")

app = Client("gofile-userbot", api_id=API_ID, api_hash=API_HASH, session_string=SESSION_STRING)

# Queue
task_queue = asyncio.Queue()
is_processing = False

# --- UI STYLE ---
class Style:
    FILLED = "â–“"
    EMPTY = "â–‘"
    
    @staticmethod
    def human_size(size):
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if size < 1024.0: return f"{size:.2f}{unit}"
            size /= 1024.0
        return f"{size:.2f}PB"

    @staticmethod
    def time_fmt(seconds):
        m, s = divmod(int(seconds), 60)
        h, m = divmod(m, 60)
        return f"{h}h {m}m {s}s" if h else f"{m}m {s}s"

    @staticmethod
    def progress_bar(current, total):
        pct = current * 100 / total
        filled = int(pct // 10)
        return Style.FILLED * filled + Style.EMPTY * (10 - filled)

async def update_ui(current, total, msg, action, start_time):
    now = time.time()
    diff = now - start_time
    if round(diff % 4.00) == 0 or current == total:
        speed = current / diff if diff > 0 else 0
        eta = (total - current) / speed if speed > 0 else 0
        pct = (current * 100) / total
        
        text = f"""
<b>{action}...</b>
<code>{msg.document_name}</code>

<b>{Style.progress_bar(current, total)}</b> <i>{pct:.2f}%</i>

ðŸš€ <b>Speed:</b> {Style.human_size(speed)}/s
ðŸ’¾ <b>Done:</b> {Style.human_size(current)}
â³ <b>ETA:</b> {Style.time_fmt(eta)}
"""
        try: await msg.edit(text)
        except: pass

# --- HELPERS ---
def generate_thumbnail(path):
    thumb = f"{path}.jpg"
    try:
        subprocess.run(["ffmpeg", "-y", "-i", str(path), "-ss", "00:00:01", "-vframes", "1", "-vf", "scale=320:-1", thumb], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if os.path.exists(thumb): return thumb
    except: pass
    return None

async def upload_file(client, status, path, info=""):
    name = os.path.basename(path)
    status.document_name = name
    thumb = generate_thumbnail(path)
    start = time.time()
    
    async def progress(current, total):
        await update_ui(current, total, status, f"ðŸ“¤ Uploading {info}", start)

    try:
        await client.send_video(
            "me", path, caption=f"<code>{name}</code>\n{info}", 
            thumb=thumb, supports_streaming=True, progress=progress
        )
    except FloodWait as e:
        await asyncio.sleep(e.value)
        await upload_file(client, status, path, info)
    except Exception as e:
        log.error(f"Upload fail: {e}")
    finally:
        if thumb and os.path.exists(thumb): os.remove(thumb)

async def check_and_upload(client, status, index_str=""):
    files = list(DOWNLOAD_DIR.glob("*"))
    if not files: return

    total_files = len(files)
    for idx, path in enumerate(files, 1):
        path_str = str(path)
        size = os.path.getsize(path_str)
        curr_info = f"{index_str} [{idx}/{total_files}]"

        if size > MAX_CHUNK_SIZE:
            await status.edit(f"<b>âœ‚ï¸ Splitting {path.name}...</b>")
            cmd = ["split", "-b", "1900M", "--numeric-suffixes=1", "--additional-suffix=.mp4", path_str, f"{path_str}.part"]
            await asyncio.create_subprocess_exec(*cmd)
            os.remove(path_str)
            
            parts = sorted(list(path.parent.glob("*.part*.mp4")))
            for i, part in enumerate(parts, 1):
                await upload_file(client, status, str(part), f"{curr_info} Part {i}/{len(parts)}")
                os.remove(part)
        else:
            await upload_file(client, status, path_str, curr_info)
            os.remove(path_str)

# --- HANDLERS ---
async def handle_gofile(client, status, url):
    go = GoFile()
    cid = url.split("/d/")[-1].split("?")[0]
    files = await asyncio.to_thread(go.get_files, str(DOWNLOAD_DIR), cid)
    
    for i, f in enumerate(files, 1):
        status.document_name = os.path.basename(f.dest)
        await status.edit(f"<b>â¬‡ï¸ GoFile DL [{i}/{len(files)}]</b>")
        await asyncio.to_thread(Downloader(go.token).download, f)
        if os.path.exists(f.dest):
            await check_and_upload(client, status)

async def handle_extras(client, status, url):
    dl = StreamDownloader(str(DOWNLOAD_DIR))
    start = time.time()
    
    async def callback(text):
        if "Bunkr File" in text:
            try: await status.edit(f"<b>ðŸ“¦ {text}</b>")
            except: pass
            return

        match = re.search(r'\[download\]\s+(\d+\.?\d*)%', text)
        if match:
            pct = float(match.group(1))
            if (time.time() - start) > 4:
                bar = Style.progress_bar(pct, 100)
                try: await status.edit(f"<b>â¬‡ï¸ Downloading...</b>\n{bar} {pct}%")
                except: pass

    if "bunkr" in url:
        await dl.process_bunkr(url, status, callback)
    else:
        await dl.download_generic(url, progress_callback=callback)
    
    await check_and_upload(client, status)

async def handle_generic(client, status, url):
    await status.edit("<b>ðŸ¦† yt-dlp Downloading...</b>")
    cmd = ["yt-dlp", "-f", "bv*+ba/b", "-o", str(DOWNLOAD_DIR/"%(title)s.%(ext)s"), url]
    proc = await asyncio.create_subprocess_exec(*cmd)
    await proc.wait()
    await check_and_upload(client, status)

# --- WORKER ---
async def worker():
    global is_processing
    is_processing = True
    while not task_queue.empty():
        client, msg, url = await task_queue.get()
        status = await msg.reply("<b>â³ Queued...</b>")
        
        shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        status.document_name = "Unknown"

        try:
            if "gofile.io" in url:
                await handle_gofile(client, status, url)
            elif any(x in url for x in ["streamtape", "pkembed", "myvidplay", "bunkr"]):
                await handle_extras(client, status, url)
            else:
                await handle_generic(client, status, url)
            await status.delete()
        except Exception as e:
            await status.edit(f"âŒ Error: {e}")
        
        shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
        task_queue.task_done()
    is_processing = False

@app.on_message(filters.text & filters.private & filters.outgoing)
async def on_msg(client, message):
    text = message.text.strip()
    if text.startswith("http"):
        await task_queue.put((client, message, text))
        if not is_processing: asyncio.create_task(worker())
        else: await message.reply(f"<b>âœ… Added to Queue. Pos: {task_queue.qsize()}</b>")

if __name__ == "__main__":
    if not API_ID or not SESSION_STRING:
        print("âŒ Set API_ID, API_HASH, and SESSION_STRING env vars!")
    else:
        print("ðŸ¤– Bot Started.")
        app.run()
