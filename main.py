import os
import re
import math
import asyncio
import shutil
import time
import logging
import subprocess
import requests
from pathlib import Path

from pyrogram import Client, filters, errors
from pyrogram.types import Message

# --- Import Bunkr ---
try:
    from bunkr import Bunkr
except ImportError:
    Bunkr = None
    print("Warning: bunkr.py not found. Bunkr logic will fail.")

# --- Import Run (GoFile) ---
try:
    from run import GoFile, Downloader, File
except ImportError:
    GoFile = None
    Downloader = None
    print("Warning: run.py not found. GoFile logic will fail.")

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
SESSION_STRING = os.getenv("SESSION_STRING", "")

DOWNLOAD_DIR = Path("output")
MAX_CHUNK_SIZE = 1900 * 1024 * 1024
MIN_FREE_SPACE_MB = 500

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("BOT")

app = Client(
    "gofile-userbot",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING
)

saved_messages_chat = None

async def get_saved_messages_chat(client):
    global saved_messages_chat
    if not saved_messages_chat:
        me = await client.get_me()
        saved_messages_chat = me.id
        log.info(f"Saved Messages chat ID: {saved_messages_chat}")
    return saved_messages_chat

def get_free_space():
    return shutil.disk_usage(os.getcwd()).free

def format_bytes(size):
    if not size:
        return "0B"
    power = 2**10
    n = 0
    power_labels = {0: 'B', 1: 'KB', 2: 'MB', 3: 'GB', 4: 'TB'}
    while size > power:
        size /= power
        n += 1
    return f"{size:.2f} {power_labels[n]}"

# --- FUTURISTIC PROGRESS BAR ---
def get_progress_bar(percent, total=15):
    filled = int(total * percent // 100)
    # Futuristic style characters
    return f"❲{'█'*filled}{'▒'*(total-filled)}❳"

async def progress_bar(current, total, status, title):
    try:
        now = time.time()
        if not hasattr(status, "start"):
            status.start = now
            status.last = 0
        if not hasattr(status, "last"):
            status.last = 0
        
        # Update every 3 seconds to keep telegram api happy
        if now - status.last < 3 and current != total:
            return
        status.last = now

        percent = (current * 100 / total) if total else 0
        elapsed = now - status.start
        speed = current / elapsed if elapsed > 0 else 0
        eta = (total - current) / speed if speed > 0 else 0

        # Stylish UI
        await status.edit(
            f"<b>⚡ {title}</b>\n"
            f"<b>{get_progress_bar(percent)} {percent:.1f}%</b>\n"
            f"<b>📂 Sɪᴢᴇ:</b> {format_bytes(current)} / {format_bytes(total)}\n"
            f"<b>🚀 Sᴘᴇᴇᴅ:</b> {format_bytes(speed)}/s\n"
            f"<b>⏳ Eᴛᴀ:</b> {int(eta)}s"
        )
    except:
        pass

# --- FAST FFPROBE ---
def get_duration(file_path):
    try:
        cmd = [
            "ffprobe", 
            "-v", "error", 
            "-select_streams", "v:0", 
            "-show_entries", "format=duration", 
            "-of", "default=noprint_wrappers=1:nokey=1", 
            str(file_path)
        ]
        # Added timeout to prevent hanging
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=5)
        if result.stdout.strip():
            return float(result.stdout.strip())
    except Exception as e:
        log.error(f"Error getting duration: {e}")
    return 0

def faststart_mp4(src):
    if not os.path.exists(src):
        return src

    dst = src + ".fast.mp4"
    try:
        # Added faststart to move atoms to beginning for streaming
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", src, "-c", "copy", "-movflags", "+faststart", dst],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=300,
            text=True
        )

        if result.returncode == 0 and os.path.exists(dst):
            return dst
        else:
            return src
    except Exception as e:
        return src

# --- FAST THUMBNAIL GENERATOR ---
def generate_thumbnail(video_path):
    video_path = str(video_path)
    thumb_path = f"{video_path}.jpg"

    if not os.path.exists(video_path):
        return None

    # Logic: Force grab frame at 3 seconds. If fails, try 1 second.
    # This is much faster than calculating duration percentages.
    timestamps = ["00:00:03", "00:00:01", "00:00:00"]

    for ss in timestamps:
        try:
            cmd = [
                "ffmpeg", "-y",
                "-ss", ss,              # Seek fast
                "-i", video_path,       # Input
                "-vframes", "1",        # Only 1 frame
                "-vf", "scale=320:-1",  # Resize width to 320 (Telegram standard)
                "-q:v", "2",            # High quality JPG
                thumb_path
            ]
            
            subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)

            # Check if generated and valid size > 1KB
            if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 1000:
                log.info(f"Thumbnail generated at {ss}")
                return thumb_path
        except Exception as e:
            continue

    log.error(f"Failed to generate thumbnail for {os.path.basename(video_path)}")
    return None

# --- GOFILE LOGIC ---
async def handle_gofile_logic(client, message, status, url):
    try:
        if not GoFile:
            await status.edit("run.py is missing!")
            return

        go = GoFile()
        m = re.search(r"gofile\.io/d/([\w\-]+)", url)
        if not m:
            await status.edit("Invalid GoFile URL.")
            return

        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        files = go.get_files(dir=str(DOWNLOAD_DIR), content_id=m.group(1))
        if not files:
            await status.edit("No files found in GoFile link.")
            return

        await status.edit(f"Found {len(files)} file(s) on GoFile. Processing...")

        for idx, file in enumerate(files, 1):
            if get_free_space() < MIN_FREE_SPACE_MB * 1024 * 1024:
                await status.edit("Disk Full.")
                break

            file_name = os.path.basename(file.dest)
            await status.edit(f"[{idx}/{len(files)}] Preparing: {file_name}...")

            dest_dir = os.path.dirname(file.dest)
            if dest_dir:
                os.makedirs(dest_dir, exist_ok=True)

            upload_queue = asyncio.Queue()
            download_complete = asyncio.Event()
            loop = asyncio.get_running_loop()

            def on_part_ready(path, part_num, total_parts, size):
                asyncio.run_coroutine_threadsafe(upload_queue.put((path, part_num, total_parts)), loop)

            async def download_task():
                try:
                    await asyncio.to_thread(
                        Downloader(token=go.token).download,
                        file, 1, on_part_ready
                    )
                except Exception as e:
                    log.error(f"Download error: {e}")
                finally:
                    download_complete.set()

            async def upload_task():
                while True:
                    try:
                        get_task = asyncio.create_task(upload_queue.get())
                        wait_task = asyncio.create_task(download_complete.wait())
                        done, pending = await asyncio.wait([get_task, wait_task], return_when=asyncio.FIRST_COMPLETED)

                        if get_task in done:
                            path, part_num, total_parts = await get_task
                            if wait_task in pending:
                                wait_task.cancel()
                            if not os.path.exists(path):
                                log.error(f"Part file not found: {path}")
                                continue

                            caption = f"{file_name} [Part {part_num}/{total_parts}]" if total_parts > 1 else file_name
                            await status.edit(f"[{idx}/{len(files)}] Uploading Part {part_num}/{total_parts}...")

                            fixed_path = await asyncio.to_thread(faststart_mp4, str(path))
                            thumb_path = await asyncio.to_thread(generate_thumbnail, fixed_path)

                            try:
                                chat_id = await get_saved_messages_chat(client)
                                await client.send_video(
                                    chat_id,
                                    video=fixed_path,
                                    caption=caption,
                                    supports_streaming=True,
                                    thumb=thumb_path,
                                    progress=progress_bar,
                                    progress_args=(status, f"UP: {part_num}/{total_parts}")
                                )
                            except Exception as e:
                                log.error(f"Send Error: {e}")

                            if thumb_path and os.path.exists(thumb_path):
                                os.remove(thumb_path)
                            if fixed_path and os.path.exists(fixed_path) and fixed_path != str(path):
                                os.remove(fixed_path)
                            if path and os.path.exists(path):
                                os.remove(path)
                        else:
                            if get_task in pending:
                                get_task.cancel()
                            if upload_queue.empty():
                                break

                    except asyncio.CancelledError:
                        break
                    except Exception as e:
                        log.error(f"Upload loop error: {e}")

            await asyncio.gather(download_task(), upload_task())

        await status.edit("GoFile Download Complete!")
    except Exception as e:
        log.exception(e)
        await status.edit(f"GoFile Error: {str(e)}")

async def download_direct_any(url, out_path, status):
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "yt-dlp",
        "-f", "bv*+ba/b",
        "--merge-output-format", "mp4",
        "--no-playlist",
        "--newline",
        "--no-check-certificate", 
        "-o", str(out_path),
        url
    ]

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )

    pattern = re.compile(r'\[download\]\s+(\d+\.?\d*)%\s+of\s+(?:~)?(\d+\.?\d+)(\w+)\s+at\s+([^\s]+)\s+ETA\s+([^\s]+)')
    last_update = 0
    filename = out_path.name

    while True:
        line = await process.stdout.readline()
        if not line: break

        try:
            line_decoded = line.decode().strip()
            match = pattern.search(line_decoded)
            if match:
                now = time.time()
                if now - last_update > 3:
                    percent = float(match.group(1))
                    total_val = float(match.group(2))
                    unit = match.group(3)
                    speed = match.group(4)
                    eta = match.group(5)
                    current_val = total_val * (percent / 100)

                    await status.edit(
                        f"<b>⬇️ Dᴏᴡɴʟᴏᴀᴅɪɴɢ: {filename}</b>\n"
                        f"<b>{get_progress_bar(percent)} {percent}%</b>\n"
                        f"<b>📦 Sɪᴢᴇ:</b> {current_val:.2f}{unit} / {total_val}{unit}\n"
                        f"<b>🚀 Sᴘᴇᴇᴅ:</b> {speed} | <b>⏳ ETA:</b> {eta}"
                    )
                    last_update = now
        except:
            pass

    await process.wait()
    return process.returncode == 0 and out_path.exists()

# --- BUNKR LOGIC HELPERS ---

async def resolve_bunkr_url(url):
    """Uses bunkr.py to scrape the album/file and return a list of direct links."""
    if not Bunkr: return []
    try:
        b = Bunkr()
        # Run the scraping in a thread to not block the bot
        items = await asyncio.to_thread(b.get_files, url)
        resolved = []
        for item in items:
            resolved.append({
                "url": item["url"],
                "name": item.get("name", "bunkr_video.mp4"),
                "size": 0
            })
        return resolved
    except Exception as e:
        log.error(f"Bunkr Resolve Error: {e}")
        return []

async def resolve_generic_url(url):
    items = []
    if "pixeldrain.com" in url:
        if "/l/" in url:
            lid = url.split("/l/")[1].split("/")[0]
            try:
                r = requests.get(f"https://pixeldrain.com/api/list/{lid}").json()
                if r.get("success"):
                    for f in r.get("files", []):
                        items.append({"url": f"https://pixeldrain.com/api/file/{f['id']}", "name": f['name'], "size": f['size']})
            except: pass
        elif "/u/" in url:
            fid = url.split("/u/")[1].split("/")[0]
            try:
                r = requests.get(f"https://pixeldrain.com/api/file/{fid}/info").json()
                items.append({"url": f"https://pixeldrain.com/api/file/{fid}", "name": r.get('name', f'{fid}.mp4'), "size": r.get('size', 0)})
            except: pass
    else:
        items.append({"url": url, "name": "video.mp4", "size": 0})
    return items

async def handle_generic_logic(client, message, status, url, file_list=None):
    if file_list is None:
        file_list = await resolve_generic_url(url)
        
    if not file_list:
        await status.edit("No files found.")
        return

    total = len(file_list)
    for idx, item in enumerate(file_list, 1):
        name = re.sub(r'[^\w\-. ]', '', item["name"])
        if not name: name = "video.mp4"
        path = DOWNLOAD_DIR / name
        path.parent.mkdir(parents=True, exist_ok=True)

        await status.edit(f"<b>⬇️ [{idx}/{total}] Dᴏᴡɴʟᴏᴀᴅɪɴɢ: {name}...</b>")
        ok = await download_direct_any(item["url"], path, status)

        if not ok or not path.exists():
            await status.edit("Download failed.")
            continue

        size = os.path.getsize(path)

        if size <= MAX_CHUNK_SIZE:
            # Generate thumbnail using the FAST method
            thumb = await asyncio.to_thread(generate_thumbnail, str(path))
            try:
                chat_id = await get_saved_messages_chat(client)
                await client.send_video(
                    chat_id,
                    str(path),
                    caption=name,
                    thumb=thumb,
                    supports_streaming=True,
                    progress=progress_bar,
                    progress_args=(status, "Uᴘʟᴏᴀᴅɪɴɢ")
                )
            except Exception as e:
                log.error(f"Upload error: {e}")
            
            if thumb and os.path.exists(thumb): os.remove(thumb)
            if path.exists(): os.remove(path)
            continue

        await status.edit(f"[{idx}/{total}] File > 1.9GB. Splitting...")

        duration = await asyncio.to_thread(get_duration, str(path))
        base_str = str(path.with_suffix(""))

        if duration > 0:
            SAFE_TARGET = 1850 * 1024 * 1024
            segment_time = int((SAFE_TARGET / size) * duration)
            if segment_time < 30: segment_time = 30

            cmd = [
                "ffmpeg", "-i", str(path), "-c", "copy", "-map", "0",
                "-f", "segment", "-segment_time", str(segment_time),
                "-reset_timestamps", "1", f"{base_str}.part%03d.mp4"
            ]
        else:
            await status.edit("Metadata error. Using binary split.")
            cmd = [
                "split", "-b", "1900M", "--numeric-suffixes=0",
                "--additional-suffix=.mp4", str(path), f"{base_str}.part"
            ]

        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        await proc.wait()

        if path.exists(): os.remove(path)

        parts = sorted(path.parent.glob(f"{path.stem}.part*.mp4"))
        if not parts:
            await status.edit("Splitting produced no output files.")
            continue

        for i, part in enumerate(parts, 1):
            part_name = f"{name} [Part {i}/{len(parts)}]"
            await status.edit(f"[{idx}/{total}] Uploading Part {i}/{len(parts)}...")

            thumb = await asyncio.to_thread(generate_thumbnail, str(part))
            try:
                chat_id = await get_saved_messages_chat(client)
                await client.send_video(
                    chat_id,
                    str(part),
                    caption=part_name,
                    thumb=thumb,
                    supports_streaming=True,
                    progress=progress_bar,
                    progress_args=(status, f"UP: {i}/{len(parts)}")
                )
            except Exception as e:
                log.error(f"Upload error part {i}: {e}")
                await asyncio.sleep(5)

            if thumb and os.path.exists(thumb): os.remove(thumb)
            if part.exists(): os.remove(part)

    await status.edit("<b>✅ Tᴀsᴋ Cᴏᴍᴘʟᴇᴛᴇᴅ!</b>")

@app.on_message(filters.text & (filters.outgoing | filters.private))
async def handler(client, message: Message):
    text = message.text.strip()
    if not text.startswith("http"): return

    status = await message.reply("<b>🔍 Aɴᴀʟʏsɪɴɢ Lɪɴᴋ...</b>")

    shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    try:
        if "gofile.io" in text:
            await handle_gofile_logic(client, message, status, text)
        
        elif "bunkr" in text:
            if not Bunkr:
                await status.edit("Bunkr module not available.")
            else:
                await status.edit("<b>🔄 Sᴄʀᴀᴘɪɴɢ Bᴜɴᴋʀ...</b>")
                files = await resolve_bunkr_url(text)
                if files:
                    await handle_generic_logic(client, message, status, text, file_list=files)
                else:
                    await status.edit("No files found on Bunkr.")

        else:
            await handle_generic_logic(client, message, status, text)
            
    except Exception as e:
        log.error(e)
        await status.edit(f"Error: {e}")
    finally:
        shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)

if __name__ == "__main__":
    if not API_ID or not API_HASH or not SESSION_STRING:
        print("Error: API_ID, API_HASH, and SESSION_STRING environment variables are required.")
    else:
        app.run()
