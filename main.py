import os
import re
import math
import asyncio
import shutil
import time
import logging
import subprocess
import requests
import json
from pathlib import Path
from urllib.parse import unquote, urlparse

from pyrogram import Client, filters, errors
from pyrogram.types import Message

# --- Config ---
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")

DOWNLOAD_DIR = Path("output")

# Split limit: 1.5GB (Safe for 4GB VPS/Disk)
# Lowered from 1.9GB to ensure you don't run out of space if OS takes 2GB+
MAX_CHUNK_SIZE = 1500 * 1024 * 1024 

# Standard Browser Header to avoid being blocked
GENERIC_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
    "Referer": "https://google.com/",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"
}

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("BOT")

app = Client(
    "gofile-userbot",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING
)

# ---------------- UTILS ----------------

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
        if hasattr(status, "last") and now - status.last < 3:
            return
        status.last = now
        p = (current * 100 / total) if total > 0 else 0
        
        if not hasattr(status, "start_time"):
            status.start_time = now
        
        diff = now - status.start_time
        speed = current / diff if diff > 0 else 0
        eta = (total - current) / speed if speed > 0 else 0
        eta_str = time.strftime("%H:%M:%S", time.gmtime(eta))
        
        await status.edit(
            f"<b>{title}</b>\n"
            f"<code>{get_progress_bar(p)} {p:.1f}%</code>\n"
            f"<b>Size:</b> {format_bytes(current)} / {format_bytes(total)}\n"
            f"<b>Speed:</b> {format_bytes(speed)}/s\n"
            f"<b>ETA:</b> {eta_str}"
        )
    except:
        pass

# ---------------- CORE LOGIC ----------------

def get_extension_from_url(url):
    path = urlparse(url).path
    ext = os.path.splitext(path)[1]
    return ext if ext else ".mp4"

def get_real_filename(url, default_name, headers=None):
    try:
        r = requests.head(url, allow_redirects=True, timeout=5, headers=headers)
        if "Content-Disposition" in r.headers:
            fname = re.findall("filename=(.+)", r.headers["Content-Disposition"])
            if fname:
                clean_name = fname[0].strip().replace('"', '').replace("'", "")
                return unquote(clean_name)
        path_name = os.path.basename(urlparse(r.url).path)
        if path_name and "." in path_name:
            return unquote(path_name)
    except: pass
    return default_name

async def download_byte_range(url, start, end, filename, headers=None):
    out_path = DOWNLOAD_DIR / filename
    # Add -k to ignore SSL errors which happen on some bunkr mirrors
    cmd = ["curl", "-k", "-L", "-s", "-r", f"{start}-{end}", "-o", str(out_path)]

    final_headers = GENERIC_HEADERS.copy()
    if headers:
        final_headers.update(headers)

    for key, value in final_headers.items():
        cmd.extend(["-H", f"{key}: {value}"])

    cmd.append(url)

    process = await asyncio.create_subprocess_exec(*cmd)
    await process.wait()
    return out_path if out_path.exists() else None

def generate_thumbnail(video_path):
    thumb_path = f"{video_path}.jpg"
    timestamps = ["00:00:15", "00:00:02"]
    for ss in timestamps:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(video_path), "-ss", ss, "-vframes", "1", thumb_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        if os.path.exists(thumb_path):
            return thumb_path
    return None

def generate_thumbnail_for_chunk(video_path):
    """Extracts thumbnail for video chunks (large videos)"""
    thumb_path = f"{video_path}.jpg"
    # Try 15s, 2s, then 0s
    timestamps = ["00:00:15", "00:00:02", "00:00:00"]
    for ss in timestamps:
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", str(video_path), "-ss", ss, "-vframes", "1",
             "-vf", "scale=320:-1", thumb_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        if result.returncode == 0 and os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
            return thumb_path
    return None

async def upload_media_safe(client, chat_id, path, caption, thumb, status, progress_text):
    if not os.path.exists(path): return
    
    ext = os.path.splitext(path)[1].lower()
    is_video = ext in ['.mp4', '.mkv', '.avi', '.mov', '.webm', '.flv', '.m4v']
    is_image = ext in ['.jpg', '.jpeg', '.png', '.webp']

    try:
        if is_video:
            await client.send_video(
                chat_id,
                path,
                caption=caption,
                thumb=thumb,
                supports_streaming=True,
                progress=progress_bar,
                progress_args=(status, progress_text)
            )
        elif is_image:
            await client.send_photo(
                chat_id,
                path,
                caption=caption,
                progress=progress_bar,
                progress_args=(status, progress_text)
            )
        else:
            await client.send_document(
                chat_id,
                path,
                caption=caption,
                thumb=thumb,
                progress=progress_bar,
                progress_args=(status, progress_text)
            )

    except errors.FloodWait as e:
        log.warning(f"FloodWait: {e.value}s")
        await asyncio.sleep(e.value + 5)
        await upload_media_safe(client, chat_id, path, caption, thumb, status, progress_text)
    except Exception as e:
        log.error(f"Upload Error: {e}")

# ---------------- SCRAPERS & RESOLVERS ----------------

def extract_xhchicks(url):
    try:
        r = requests.get(url, headers=GENERIC_HEADERS, timeout=10)
        src = re.search(r'<source\s+src=["\']([^"\']+)["\']', r.text)
        if src: return src.group(1)
        src2 = re.search(r'video_url\s*:\s*["\']([^"\']+)["\']', r.text)
        if src2: return src2.group(1)
        return None
    except: return None

def extract_cyberfile(url):
    files = []
    try:
        r = requests.get(url, headers=GENERIC_HEADERS, timeout=10)
        regex = r'href=["\'](https?://[^"\']+\.(?:mp4|mkv|zip|rar|7z|avi|mov|iso|jpg|png|jpeg))["\']'
        matches = re.findall(regex, r.text, re.IGNORECASE)
        for m in matches:
            if m not in [f['url'] for f in files]:
                name = unquote(m.split("/")[-1])
                files.append({"url": m, "name": name, "size": 0, "headers": GENERIC_HEADERS})
    except: pass
    return files

def get_gofile_token():
    try:
        r = requests.post("https://api.gofile.io/accounts", headers=GENERIC_HEADERS).json()
        if r.get("status") == "ok":
            return r["data"]["token"]
    except: pass
    return None

def extract_gofile_folder(content_id, token):
    files = []
    try:
        url = f"https://api.gofile.io/contents/{content_id}?wt={token}"
        r = requests.get(url, headers=GENERIC_HEADERS).json()
        
        if r.get("status") == "ok":
            contents = r["data"].get("children", {})
            for child_id, child_data in contents.items():
                if child_data["type"] == "folder":
                    files.extend(extract_gofile_folder(child_data["id"], token))
                else:
                    files.append({
                        "url": child_data["link"],
                        "name": child_data["name"],
                        "size": child_data["size"],
                        "headers": {"Cookie": f"wt={token}"}
                    })
    except Exception as e:
        log.error(f"GoFile Error: {e}")
    return files

def extract_bunkr_album(url):
    links = []
    try:
        r = requests.get(url, headers=GENERIC_HEADERS, timeout=15)
        domain = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
        
        regex = r'href=["\']((?:https?://[^/]+)?/(?:v|i|f|d)/[^"\' >]+)["\']'
        matches = re.findall(regex, r.text)
        
        for m in matches:
            full_url = m if m.startswith("http") else f"{domain}{m}"
            if full_url not in [x['url'] for x in links]:
                name_guess = full_url.split("/")[-1]
                links.append({
                    "url": full_url,
                    "name": name_guess, 
                    "size": 0,
                    "type": "bunkr_view", 
                    "headers": GENERIC_HEADERS
                })
    except Exception as e:
        log.error(f"Bunkr Album Error: {e}")
    return links

def resolve_bunkr_direct(view_url):
    try:
        r = requests.get(view_url, headers=GENERIC_HEADERS, timeout=10)
        # Video
        src = re.search(r'<source\s+src=["\']([^"\']+)["\']', r.text)
        if src: return src.group(1)
        # Image
        img = re.search(r'<img\s+[^>]*src=["\']([^"\']+\.(?:jpg|jpeg|png|gif))["\']', r.text, re.IGNORECASE)
        if img: return img.group(1)
        # Download Button
        dl = re.search(r'href=["\']([^"\']+)["\']\s*class=["\'][^"\']*download[^"\']*["\']', r.text, re.IGNORECASE)
        if dl: return dl.group(1)
        
        return None
    except: return None

async def resolve_url(url, token=None, bearer_token=None):
    items = []
    
    headers_to_use = GENERIC_HEADERS.copy()
    if bearer_token: headers_to_use["Authorization"] = f"Bearer {bearer_token}"
    elif token: headers_to_use["Authorization"] = f"Token {token}"

    if "cyberfile.me" in url:
        log.info("Detected Cyberfile")
        found = extract_cyberfile(url)
        if found: items.extend(found)
        else: items.append({"url": url, "name": "cyberfile_file.zip", "size": 0, "headers": headers_to_use})

    elif "xhchicks.life" in url:
        log.info("Detected XHChicks")
        direct = extract_xhchicks(url)
        name = get_real_filename(direct if direct else url, "video.mp4", headers=headers_to_use)
        items.append({"url": direct if direct else url, "name": name, "size": 0, "headers": headers_to_use})

    elif "gofile.io/d/" in url:
        log.info("Detected GoFile")
        content_id = url.split("/d/")[-1]
        guest_token = get_gofile_token()
        if guest_token:
            found = extract_gofile_folder(content_id, guest_token)
            items.extend(found)
        else:
            log.error("Failed to get GoFile token")

    elif "pixeldrain.com/l/" in url:
        log.info("Detected PixelDrain List")
        list_id = url.split("/l/")[1].split("/")[0]
        try:
            r = requests.get(f"https://pixeldrain.com/api/list/{list_id}").json()
            if r.get("success"):
                for f in r.get("files", []):
                    items.append({
                        "url": f"https://pixeldrain.com/api/file/{f['id']}",
                        "name": f['name'],
                        "size": f['size'],
                        "headers": {}
                    })
        except: pass

    elif "pixeldrain.com/u/" in url:
        file_id = url.split("/u/")[1].split("/")[0]
        items.append({
            "url": f"https://pixeldrain.com/api/file/{file_id}",
            "name": f"{file_id}.mp4", 
            "size": 0,
            "headers": {}
        })

    elif any(x in url for x in ["bunkr.si", "bunkr.is", "bunk.cr", "bunkr.ru"]):
        log.info("Detected Bunkr")
        if "/a/" in url:
            found = extract_bunkr_album(url)
            items.extend(found)
        else:
            items.append({
                "url": url,
                "name": url.split("/")[-1],
                "size": 0,
                "type": "bunkr_view",
                "headers": headers_to_use
            })

    if not items:
        try:
            r = requests.head(url, allow_redirects=True, headers=headers_to_use, timeout=5)
            size = int(r.headers.get("content-length", 0))
            name = get_real_filename(url, f"download{get_extension_from_url(url)}", headers=headers_to_use)
            items.append({"url": r.url, "name": name, "size": size, "headers": headers_to_use})
        except:
            items.append({"url": url, "name": "download.mp4", "size": 0, "headers": headers_to_use})

    return items

# ---------------- MAIN HANDLER ----------------

@app.on_message(filters.text & (filters.outgoing | filters.private))
async def handler(client, message: Message):
    text = message.text.strip()
    if not text.startswith("http"): return

    status = await message.reply("Analysing link...")
    shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    url_parts = text.split()
    main_url = url_parts[0]
    token = url_parts[1] if len(url_parts) > 1 and not url_parts[1].startswith("bearer:") else None
    bearer_token = url_parts[1][7:] if len(url_parts) > 1 and url_parts[1].startswith("bearer:") else None

    try:
        file_list = await resolve_url(main_url, token=token, bearer_token=bearer_token)
        
        if not file_list:
            await status.edit("❌ No files found.")
            return

        total_files = len(file_list)
        await status.edit(f"Found {total_files} file(s). Starting...")

        for index, item in enumerate(file_list, 1):
            url = item["url"]
            name = re.sub(r'[^\w\-. ]', '', item["name"])
            size = item["size"]
            headers = item.get("headers", {})

            # Resolve Bunkr direct links
            if item.get("type") == "bunkr_view":
                await status.edit(f"<b>File {index}/{total_files}</b>\nResolving Bunkr link...")
                direct_url = await asyncio.to_thread(resolve_bunkr_direct, url)
                if direct_url:
                    url = direct_url
                    ext = get_extension_from_url(url)
                    if ext and ext not in name: name += ext
                else:
                    log.error(f"Failed to resolve bunkr: {url}")
                    continue 

            await status.edit(f"<b>File {index}/{total_files}</b>\nName: {name}\nSize: {format_bytes(size)}")

            # --- CASE A: Small File (Single Download) ---
            if 0 < size < MAX_CHUNK_SIZE:
                f_path = await download_byte_range(url, 0, size, name, headers=headers)
                if f_path:
                    is_video = f_path.suffix.lower() in ['.mp4', '.mkv', '.avi', '.mov', '.webm']
                    thumb = await asyncio.to_thread(generate_thumbnail, f_path) if is_video else None
                    await upload_media_safe(client, "me", f_path, name, thumb, status, "Uploading")
                    os.remove(f_path)
                    if thumb and os.path.exists(thumb): os.remove(thumb)

            # --- CASE B: Unknown Size (Risk) -> Download Whole ---
            elif size == 0:
                out_path = DOWNLOAD_DIR / name
                cmd = ["curl", "-k", "-L", "-s", "-o", str(out_path)]
                for k, v in headers.items(): cmd.extend(["-H", f"{k}: {v}"])
                cmd.append(url)
                proc = await asyncio.create_subprocess_exec(*cmd)
                await proc.wait()
                
                if out_path.exists() and out_path.stat().st_size > 0:
                    is_video = out_path.suffix.lower() in ['.mp4', '.mkv', '.avi', '.mov', '.webm']
                    thumb = await asyncio.to_thread(generate_thumbnail, out_path) if is_video else None
                    await upload_media_safe(client, "me", out_path, name, thumb, status, "Uploading")
                    os.remove(out_path)
                    if thumb and os.path.exists(thumb): os.remove(thumb)

            # --- CASE C: Large File -> Split ---
            else:
                parts = math.ceil(size / MAX_CHUNK_SIZE)
                current_byte = 0
                global_thumb = None

                for part in range(1, parts + 1):
                    end_byte = min(current_byte + MAX_CHUNK_SIZE - 1, size - 1)
                    part_name = f"{name}.part{part:03d}.mp4"

                    await status.edit(f"<b>File {index}/{total_files}: {name}</b>\nProcessing Part {part}/{parts}")

                    chunk_path = await download_byte_range(url, current_byte, end_byte, part_name, headers=headers)
                    if not chunk_path: break

                    if part == 1:
                        extracted = await asyncio.to_thread(generate_thumbnail_for_chunk, chunk_path)
                        if extracted:
                            global_thumb = str(DOWNLOAD_DIR / f"thumb_{index}.jpg")
                            os.rename(extracted, global_thumb)

                    caption = f"{name}\nPart {part}/{parts}"
                    thumb_to_use = global_thumb if global_thumb and os.path.exists(global_thumb) else None
                    
                    await upload_media_safe(client, "me", chunk_path, caption, thumb_to_use, status, f"Up Part {part}")

                    os.remove(chunk_path)
                    current_byte = end_byte + 1

                if global_thumb and os.path.exists(global_thumb):
                    os.remove(global_thumb)

        await status.edit("✅ All files processed successfully!")

    except Exception as e:
        log.exception(e)
        await status.edit(f"Error: {str(e)}")
    
    shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)

if __name__ == "__main__":
    app.run()
