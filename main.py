import time
import asyncio
from pyrogram import Client, filters
from yt_dlp import YoutubeDL

# Bot Credentials
API_ID = 123456
API_HASH = "your_hash"
BOT_TOKEN = "your_token"

app = Client("my_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

async def progress(current, total, message, start_time):
    now = time.time()
    if (now - start_time) < 5: return # Update every 5s to avoid flood
    percent = current * 100 / total
    try:
        await message.edit(f"🚀 Uploading: {percent:.1f}%")
    except: pass

@app.on_message(filters.regex(r'^http'))
async def handle_link(client, message):
    status = await message.reply("⚡ Processing...")
    url = message.text

    # Use aria2 for fast metadata extraction via yt-dlp
    ydl_opts = {
        'quiet': True,
        'external_downloader': 'aria2c',
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best'
    }

    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            # Direct link from Pixeldrain/Gofile/YouTube
            direct_url = info.get('url') 
            thumb = info.get('thumbnail')
            duration = info.get('duration', 0)

        # FIXED: Sending directly from URL (No local storage used)
        # FIXED: Thumb passed to solve 'blue thumbnail' issue
        await client.send_video(
            chat_id=message.chat.id,
            video=direct_url,
            caption=f"✅ {info.get('title')}",
            thumb=thumb,
            duration=duration,
            supports_streaming=True,
            progress=progress,
            progress_args=(status, time.time())
        )
        await status.delete()
    except Exception as e:
        await status.edit(f"❌ Error: {str(e)}")

app.run()
