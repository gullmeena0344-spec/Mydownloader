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

from pyrogram import Client, filters
from pyrogram.types import Message
# Assuming run.py contains these classes
from run import GoFile, Downloader, File

# --- Config ---
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")

DOWNLOAD_DIR = Path("output")
MAX_TG_SIZE = 1990 * 1024 * 1024
MIN_FREE_SPACE_MB = 500
MAX_PART_SIZE = 1900 * 1024 * 1024

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("BOT")

app = Client(
    "gofile-userbot",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING
)

# ---------------- utils ----------------

def get_free_space():
    return shutil.disk_usage(os.getcwd()).free

def format_bytes(size):
    for u in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024:
            return f"{size:.2f} {u}"
        size /= 1024

def get_progress_bar(percent, total=15):
    filled = int(total * percent // 100)
    return f"▰{'▰'*filled}{'▱'*(total-filled-1)}▱"

async def progress_bar(current, total, status, title):
    try:
        now = time.time()
        if hasattr(status, "last") and now - status.last < 1.5:
            return
        status.last = now
        p = (current * 100 / total) if total > 0 else 0
        
        # Calculate speed and ETA
        if not hasattr(status, "start_time"):
            status.start_time = now
            status.last_current = current
        
        diff = now - status.start_time
        speed = current / diff if diff > 0 else 0
        eta = (total - current) / speed if speed > 0 else 0
        
        eta_str = time.strftime("%M:%S", time.gmtime(eta)) if eta < 3600 else "Wait..."
        
        await status.edit(
            f"<b>{title}</b>\n"
            f"<code>{get_progress_bar(p)} {p:.1f}%</code>\n"
            f"<b>Size:</b> {format_bytes(current)} / {format_bytes(total)}\n"
            f"<b>Speed:</b> {format_bytes(speed)}/s\n"
            f"<b>ETA:</b> {eta_str}"
        )
    except:
        pass

FFMPEG_PATH = "./ffmpeg_static"

def faststart_mp4(src):
    """
    Safely runs ffmpeg by temporarily renaming the file to ASCII
    if it contains emojis or special characters.
    """
    if not os.path.exists(src):
        return src

    dir_name = os.path.dirname(src)
    # Define temporary safe paths
    temp_input = os.path.join(dir_name, f"temp_fs_in_{int(time.time())}.mp4")
    temp_output = os.path.join(dir_name, f"temp_fs_out_{int(time.time())}.mp4")
    final_dst = src + ".fast.mp4"

    renamed_input = False

    try:
        # 1. Rename complex filename to simple ASCII
        os.rename(src, temp_input)
        renamed_input = True

        # 2. Run FFmpeg on the safe filename
        subprocess.run(
            [FFMPEG_PATH, "-y", "-i", temp_input, "-c", "copy", "-movflags", "+faststart", temp_output],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )

        # 3. If success, move output to final destination and restore input name
        if os.path.exists(temp_output):
            if os.path.exists(final_dst): os.remove(final_dst)
            os.rename(temp_output, final_dst)
            
            os.rename(temp_input, src) # Restore original
            return final_dst

    except Exception as e:
        log.error(f"Faststart Error: {e}")

    # Fallback: Restore original name if something failed
    if renamed_input and os.path.exists(temp_input):
        os.rename(temp_input, src)
    
    return src

def make_thumb(src):
    """
    Safely generates thumbnail by temporarily renaming the file to ASCII.
    """
    if not os.path.exists(src):
        return None

    dir_name = os.path.dirname(src)
    temp_input = os.path.join(dir_name, f"temp_th_in_{int(time.time())}.mp4")
    thumb_output = src + ".jpg"
    
    renamed_input = False

    try:
        # 1. Rename to safe ASCII
        os.rename(src, temp_input)
        renamed_input = True

        # 2. Generate Thumb
        subprocess.run(
            [FFMPEG_PATH, "-y", "-i", temp_input, "-ss", "00:00:01", "-vframes", "1", thumb_output],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )

        # 3. Restore original filename
        os.rename(temp_input, src)
        
        if os.path.exists(thumb_output):
            return thumb_output

    except Exception as e:
        log.error(f"Thumb Error: {e}")

    # Fallback: Restore original name
    if renamed_input and os.path.exists(temp_input):
        os.rename(temp_input, src)
        
    return None

def normalize_path(p):
    return str(p.dest) if hasattr(p, "dest") else str(p)

# ---------------- SMART DOWNLOAD ----------------

def clean_filename(name):
    # Removes emojis and special chars, keeping only A-Z, 0-9, . - _
    return re.sub(r'[^\w\-. ]', '', name)

async def smart_download(url):
    # 1. Pixeldrain List (Album)
    m = re.search(r"pixeldrain\.com/l/(\w+)", url)
    if m:
        try:
            r = requests.get(f"https://pixeldrain.com/api/list/{m.group(1)}").json()
            files = []
            for idx, f in enumerate(r.get("files", []), 1):
                # Clean filename STRICTLY
                safe_name = clean_filename(f['name'])
                out = DOWNLOAD_DIR / f"{idx}_{safe_name}"
                
                api_url = f"https://pixeldrain.com/api/file/{f['id']}"
                subprocess.run(
                    ["aria2c", "-x", "4", "-k", "1M", "-o", str(out), api_url],
                    check=True
                )
                files.append(out)
            return files
        except Exception as e:
            log.error(f"Pixeldrain List Error: {e}")
            return []

    # 2. Pixeldrain Single File
    m = re.search(r"pixeldrain\.com/u/(\w+)", url)
    if m:
        file_id = m.group(1)
        api_url = f"https://pixeldrain.com/api/file/{file_id}"
        
        try:
            info = requests.get(f"https://pixeldrain.com/api/file/{file_id}/info").json()
            # Clean filename STRICTLY
            filename = clean_filename(info.get("name", f"{file_id}.mp4"))
        except:
            filename = f"{file_id}.mp4"

        out = DOWNLOAD_DIR / filename
        
        subprocess.run(
            ["aria2c", "-x", "4", "-k", "1M", "-o", str(filename), "-d", str(DOWNLOAD_DIR), api_url],
            check=True
        )
        return [out]

    # 3. Direct Links
    if re.search(r"\.(mp4|mov|mkv|webm|avi|flv)(\?|$)", url) or \
       any(domain in url for domain in ["cdn3.mfcamhub.com", "dl1.turbocdn.st", "saint2.su"]):
        try:
            filename = "video.mp4"
            if "fn=" in url:
                filename = re.search(r"fn=([^&]+)", url).group(1)
            elif "file=" in url:
                filename = os.path.basename(re.search(r"file=([^&]+)", url).group(1))
            elif ".mp4" in url:
                m = re.search(r"/([^/?#]+\.mp4)", url)
                if m: filename = m.group(1)

            # Clean filename STRICTLY
            filename = clean_filename(filename)
            out = DOWNLOAD_DIR / filename
            
            subprocess.run(
                ["aria2c", "-x", "8", "-s", "8", "-k", "1M", "-o", filename, "-d", str(DOWNLOAD_DIR), 
                 "--header", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                 url],
                check=True
            )
            return [out]
        except subprocess.CalledProcessError:
            pass

    # 4. Generic yt-dlp
    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--restrict-filenames", # Forces ASCII filenames to avoid emoji errors
        "--merge-output-format", "mp4",
        "-o", str(DOWNLOAD_DIR / "%(title)s.%(ext)s"),
        url
    ]
    subprocess.run(cmd, check=True)
    return list(DOWNLOAD_DIR.glob("*"))

# ---------------- handler ----------------

@app.on_message(filters.text & (filters.outgoing | filters.private))
async def handler(client, message: Message):
    text = message.text.strip()
    if not text.startswith("http"): return

    if get_free_space() < MIN_FREE_SPACE_MB * 1024 * 1024:
        return await message.reply("Disk Full.")

    status = await message.reply("Starting...")
    shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    try:
        # --- GOFILE HANDLER ---
        m = re.search(r"gofile\.io/d/([\w\-]+)", text)
        if m:
            go = GoFile()
            files = go.get_files(dir=str(DOWNLOAD_DIR), content_id=m.group(1))

            for idx, f in enumerate(files, 1):
                file_name = os.path.basename(f.dest)
                real_path = normalize_path(f)
                
                upload_queue = asyncio.Queue()
                download_done = asyncio.Event()
                loop = asyncio.get_running_loop()

                def on_part(path, part, total, size):
                    asyncio.run_coroutine_threadsafe(upload_queue.put((path, part, total)), loop)

                async def download_task():
                    try:
                        def on_part_with_progress(path, part, total_parts, size):
                            on_part(path, part, total_parts, size)

                        await asyncio.to_thread(Downloader(token=go.token).download, f, 1, on_part_with_progress)
                    finally:
                        download_done.set()

                async def upload_task():
                    while True:
                        get_task = asyncio.create_task(upload_queue.get())
                        wait_task = asyncio.create_task(download_done.wait())
                        done_set, pending = await asyncio.wait([get_task, wait_task], return_when=asyncio.FIRST_COMPLETED)

                        if get_task in done_set:
                            path, part_num, total_parts = await get_task
                            if wait_task in pending: wait_task.cancel()
                            
                            await asyncio.sleep(0.5)
                            
                            if not os.path.exists(path): continue
                            
                            current_file_size = os.path.getsize(path)
                            parts = max(1, math.ceil(current_file_size / MAX_PART_SIZE))

                            if parts > 1:
                                for i in range(parts):
                                    start = i * MAX_PART_SIZE
                                    end = min(start + MAX_PART_SIZE, current_file_size)
                                    chunk_path = f"{path}.part{i+1}"
                                    
                                    with open(path, "rb") as src, open(chunk_path, "wb") as dst:
                                        src.seek(start)
                                        dst.write(src.read(end - start))

                                    fixed = faststart_mp4(chunk_path)
                                    thumb = make_thumb(fixed)
                                    caption = f"{file_name} [Part {i+1}/{parts}]"

                                    try:
                                        await client.send_video(
                                            "me",
                                            fixed,
                                            caption=caption,
                                            supports_streaming=True,
                                            thumb=thumb,
                                            progress=progress_bar,
                                            progress_args=(status,f"[{idx}/{len(files)}] Uploading {i+1}/{parts}")
                                        )
                                    except Exception as e:
                                        log.error(f"Upload failed: {e}")

                                    if os.path.exists(chunk_path): os.remove(chunk_path)
                                    if fixed != chunk_path and os.path.exists(fixed): os.remove(fixed)
                                    if thumb: os.remove(thumb)
                            else:
                                fixed = faststart_mp4(path)
                                thumb = make_thumb(fixed)
                                try:
                                    await client.send_video(
                                        "me",
                                        fixed,
                                        caption=file_name,
                                        supports_streaming=True,
                                        thumb=thumb,
                                        progress=progress_bar,
                                        progress_args=(status,f"[{idx}/{len(files)}] Uploading")
                                    )
                                except Exception as e:
                                    log.error(f"Upload failed: {e}")

                                if os.path.exists(path): os.remove(path)
                                if fixed != path and os.path.exists(fixed): os.remove(fixed)
                                if thumb: os.remove(thumb)

                        else:
                            if get_task in pending: get_task.cancel()
                            if upload_queue.empty(): break

                await asyncio.gather(download_task(), upload_task())

        # --- SMART DOWNLOAD HANDLER ---
        else:
            await status.edit("Downloading...")
            files = await smart_download(text)

            if not files:
                await status.edit("No files found or download failed.")
                return

            for idx, f in enumerate(files, 1):
                f_path = str(f)
                if not os.path.exists(f_path): continue
                
                file_name = os.path.basename(f_path)
                fixed = faststart_mp4(f_path)
                
                file_size = os.path.getsize(fixed)
                parts = max(1, math.ceil(file_size / MAX_PART_SIZE))
                
                thumb = make_thumb(fixed)

                if parts > 1:
                    for i in range(parts):
                        start = i * MAX_PART_SIZE
                        end = min(start + MAX_PART_SIZE, file_size)
                        chunk_path = f"{fixed}.part{i+1}"
                        
                        with open(fixed, "rb") as src, open(chunk_path, "wb") as dst:
                            src.seek(start)
                            dst.write(src.read(end - start))

                        fixed_chunk = faststart_mp4(chunk_path)
                        thumb_chunk = make_thumb(fixed_chunk)
                        caption = f"{file_name} [Part {i+1}/{parts}]"

                        await client.send_video(
                            "me",
                            fixed_chunk,
                            caption=caption,
                            supports_streaming=True,
                            thumb=thumb_chunk,
                            progress=progress_bar,
                            progress_args=(status,f"Uploading {i+1}/{parts}")
                        )
                        
                        if os.path.exists(chunk_path): os.remove(chunk_path)
                        if fixed_chunk != chunk_path and os.path.exists(fixed_chunk): os.remove(fixed_chunk)
                        if thumb_chunk: os.remove(thumb_chunk)
                else:
                    await client.send_video(
                        "me",
                        fixed,
                        caption=file_name,
                        supports_streaming=True,
                        thumb=thumb,
                        progress=progress_bar,
                        progress_args=(status,"Uploading")
                    )

                if os.path.exists(f_path): os.remove(f_path)
                if fixed != f_path and os.path.exists(fixed): os.remove(fixed)
                if thumb: os.remove(thumb)

        await status.edit("All done!")

    except Exception as e:
        log.exception(e)
        await status.edit(f"Error: {str(e)[:100]}")

    shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)

if __name__ == "__main__":
    app.run()
