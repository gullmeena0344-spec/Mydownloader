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
        if not hasattr(status, "start"):
            status.start = now
            status.last = 0
        if not hasattr(status, "last"):
            status.last = 0
        if now - status.last < 3:
            return
        status.last = now

        percent = (current * 100 / total) if total else 0
        elapsed = now - status.start
        speed = current / elapsed if elapsed > 0 else 0
        eta = (total - current) / speed if speed > 0 else 0

        await status.edit(
            f"<b>{title}</b>\n"
            f"<code>{get_progress_bar(percent)} {percent:.1f}%</code>\n"
            f"<b>Size:</b> {format_bytes(current)} / {format_bytes(total)}\n"
            f"<b>ETA:</b> {int(eta)}s"
        )
    except:
        pass

def get_duration(file_path):
    try:
        cmd = [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "format=duration", "-of",
            "default=noprint_wrappers=1:nokey=1", str(file_path)
        ]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
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
        subprocess.run(
            ["ffmpeg", "-y", "-i", src, "-c", "copy", "-movflags", "+faststart", dst],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True
        )
        return dst if os.path.exists(dst) else src
    except:
        return src

def generate_thumbnail(video_path):
    video_path = str(video_path)
    thumb_path = f"{video_path}.jpg"
    if not os.path.exists(video_path):
        return None

    duration = get_duration(video_path)

    timestamps = []
    if duration > 0:
        timestamps.append(f"{duration * 0.10:.2f}")
        timestamps.append(f"{duration * 0.50:.2f}")
        timestamps.append("2")
    else:
        timestamps = ["00:00:02", "00:00:00"]

    for ss in timestamps:
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(video_path), "-ss", str(ss), "-vframes", "1", thumb_path],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 1024:
                return thumb_path
        except:
            continue

    return None

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
        if not line:
            break

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

                    msg = (
                        f"<b>Downloading: {filename}</b>\n"
                        f"<code>{get_progress_bar(percent)} {percent}%</code>\n"
                        f"<b>Size:</b> {current_val:.2f}{unit} / {total_val}{unit}\n"
                        f"<b>Speed:</b> {speed} | <b>ETA:</b> {eta}"
                    )
                    await status.edit(msg)
                    last_update = now
        except:
            pass

    await process.wait()
    return process.returncode == 0 and out_path.exists()

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
            except:
                pass
        elif "/u/" in url:
            fid = url.split("/u/")[1].split("/")[0]
            try:
                r = requests.get(f"https://pixeldrain.com/api/file/{fid}/info").json()
                items.append({"url": f"https://pixeldrain.com/api/file/{fid}", "name": r.get('name', f'{fid}.mp4'), "size": r.get('size', 0)})
            except:
                pass
    else:
        items.append({"url": url, "name": "video.mp4", "size": 0})
    return items

async def handle_generic_logic(client, message, status, url):
    file_list = await resolve_generic_url(url)
    if not file_list:
        await status.edit("No files found.")
        return

    total = len(file_list)
    for idx, item in enumerate(file_list, 1):
        name = re.sub(r'[^\w\-. ]', '', item["name"])
        if not name:
            name = "video.mp4"
        path = DOWNLOAD_DIR / name

        path.parent.mkdir(parents=True, exist_ok=True)

        await status.edit(f"[{idx}/{total}] Downloading: {name}...")

        ok = await download_direct_any(item["url"], path, status)

        if not ok or not path.exists():
            await status.edit("Download failed.")
            continue

        size = os.path.getsize(path)

        if size <= MAX_CHUNK_SIZE:
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
                    progress_args=(status, "Uploading")
                )
            except Exception as e:
                log.error(f"Upload error: {e}")
            if thumb and os.path.exists(thumb):
                os.remove(thumb)
            if path.exists():
                os.remove(path)
            continue

        await status.edit(f"[{idx}/{total}] File > 1.9GB. Splitting...")

        duration = await asyncio.to_thread(get_duration, str(path))
        base_str = str(path.with_suffix(""))

        if duration > 0:
            SAFE_TARGET = 1850 * 1024 * 1024
            segment_time = int((SAFE_TARGET / size) * duration)
            if segment_time < 30:
                segment_time = 30

            cmd = [
                "ffmpeg", "-i", str(path), "-c", "copy", "-map", "0",
                "-f", "segment",
                "-segment_time", str(segment_time),
                "-reset_timestamps", "1",
                f"{base_str}.part%03d.mp4"
            ]
        else:
            await status.edit("Metadata error. Using binary split.")
            cmd = [
                "split", "-b", "1900M", "--numeric-suffixes=0",
                "--additional-suffix=.mp4", str(path), f"{base_str}.part"
            ]

        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            log.error(f"Split error: {stderr.decode()}")
            await status.edit("Error splitting video.")
            if path.exists():
                os.remove(path)
            continue

        if path.exists():
            os.remove(path)

        parts = sorted(path.parent.glob(f"{path.stem}.part*.mp4"))

        if not parts:
            await status.edit("Splitting produced no output files.")
            continue

        for i, part in enumerate(parts, 1):
            if not part.exists():
                log.error(f"Part file missing: {part}")
                continue

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

            if thumb and os.path.exists(thumb):
                os.remove(thumb)
            if part.exists():
                os.remove(part)

    await status.edit("Done!")


@app.on_message(filters.text & (filters.outgoing | filters.private))
async def handler(client, message: Message):
    text = message.text.strip()
    if not text.startswith("http"):
        return

    status = await message.reply("Analysing link...")

    shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    try:
        if "gofile.io" in text:
            await handle_gofile_logic(client, message, status, text)
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
        print("Set them in the Secrets tab.")
    else:
        app.run()
