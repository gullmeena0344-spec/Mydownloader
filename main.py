import os, time, asyncio, math
from pyrogram import Client, filters
from yt_dlp import YoutubeDL

# CONFIG
API_ID = 12345
API_HASH = "your_hash"
BOT_TOKEN = "your_token"

app = Client("my_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# PROGRESS BAR HELPER
async def progress(current, total, message, start_time):
    now = time.time()
    diff = now - start_time
    if diff < 3: return # Update every 3 seconds to avoid flood
    
    percentage = current * 100 / total
    speed = current / diff
    elapsed_time = round(diff) * 1000
    eta = round((total - current) / speed) * 1000
    
    bar = "█" * int(percentage / 10) + "░" * (10 - int(percentage / 10))
    
    try:
        await message.edit(
            f"**Uploading...**\n"
            f"[{bar}] {percentage:.2f}%\n"
            f"🚀 Speed: {humanbytes(speed)}/s\n"
            f"⏳ ETA: {time_formatter(eta)}"
        )
    except: pass

def humanbytes(size):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024: return f"{size:.2f} {unit}"
        size /= 1024

def time_formatter(milliseconds: int) -> str:
    seconds, milliseconds = divmod(int(milliseconds), 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

@app.on_message(filters.regex(r'^http'))
async def handle_url(client, message):
    url = message.text
    status = await message.reply("🔄 Extracting metadata...")
    
    # yt-dlp options to use aria2c
    ydl_opts = {
        'quiet': True,
        'external_downloader': 'aria2c',
        'external_downloader_args': ['-x', '16', '-s', '16', '-k', '1M'],
        'format': 'bestvideo+bestaudio/best',
    }

    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            video_url = info.get('url') # Direct stream link
            thumb = info.get('thumbnail')
            duration = info.get('duration', 0)
            width = info.get('width', 0)
            height = info.get('height', 0)

        # FIXED: Sending directly from URL to avoid disk usage
        # FIXED: Thumb is passed to prevent 'blue/black' preview
        start_time = time.time()
        await client.send_video(
            chat_id=message.chat.id,
            video=video_url,
            caption=f"✅ **{info.get('title', 'Video')}**",
            duration=duration,
            width=width,
            height=height,
            thumb=thumb,
            supports_streaming=True,
            progress=progress,
            progress_args=(status, start_time)
        )
        await status.delete()

    except Exception as e:
        await status.edit(f"❌ Error: {str(e)}")

app.run()
