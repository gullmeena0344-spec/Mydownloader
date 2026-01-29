import logging
import asyncio
import re
import aiohttp
from bs4 import BeautifulSoup
from pathlib import Path
import yt_dlp

log = logging.getLogger("Extras")

class BunkrScraper:
    def __init__(self):
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36',
        }

    async def get_soup(self, url):
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, headers=self.headers, timeout=20) as resp:
                    if resp.status == 200:
                        return BeautifulSoup(await resp.text(), 'html.parser')
            except Exception as e:
                log.error(f"Soup Error: {e}")
        return None

    async def get_real_link(self, media_page_url):
        soup = await self.get_soup(media_page_url)
        if not soup: return None
        
        # 1. Check Video
        vid = soup.find('video')
        if vid:
            src = vid.get('src')
            if not src:
                src_tag = vid.find('source')
                if src_tag: src = src_tag.get('src')
            if src: return src

        # 2. Check Image
        img = soup.find('img', {'class': 'is-gif'}) or soup.find('img', id='lightGallery')
        if img: return img.get('src')

        # 3. Check Download Button
        btn = soup.find('a', string=re.compile(r'Download', re.I))
        if btn: return btn.get('href')
        
        return None

    async def scrape_album(self, album_url):
        log.info(f"Scraping Bunkr: {album_url}")
        soup = await self.get_soup(album_url)
        if not soup: return []

        links = []
        # Bunkr structure usually involves grids of <a> tags
        for a in soup.find_all('a', href=True):
            href = a['href']
            # Valid content pages usually have these paths
            if any(x in href for x in ['/v/', '/i/', '/f/']):
                if not href.startswith('http'):
                    # Reconstruct domain logic
                    parts = album_url.split('/')
                    domain = f"{parts[0]}//{parts[2]}"
                    href = f"{domain}{href}" if href.startswith('/') else f"{domain}/{href}"
                links.append(href)
        
        return list(set(links))

class StreamDownloader:
    def __init__(self, output_dir):
        self.output_dir = Path(output_dir)

    async def download_generic(self, url, progress_callback=None):
        # CAPTURE MAIN LOOP SAFELY
        loop = asyncio.get_running_loop()

        def progress_hook(d):
            if d['status'] == 'downloading' and progress_callback:
                total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
                downloaded = d.get('downloaded_bytes', 0)
                if total > 0:
                    percent = (downloaded / total) * 100
                    try:
                        # USE CAPTURED LOOP
                        asyncio.run_coroutine_threadsafe(
                            progress_callback(f"[download] {percent:.1f}%"), 
                            loop
                        )
                    except: pass

        ydl_opts = {
            'outtmpl': str(self.output_dir / '%(title)s.%(ext)s'),
            'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
            'merge_output_format': 'mp4',
            'noplaylist': True,
            'ignoreerrors': True,
            'no_warnings': True,
            'postprocessors': [{'key': 'FFmpegVideoConvertor', 'preferedformat': 'mp4'}],
            'progress_hooks': [progress_hook],
            'http_headers': {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'},
            'nocheckcertificate': True,
        }
        
        # Run blocking download in thread
        await asyncio.to_thread(self._run_ydl, ydl_opts, url)

    def _run_ydl(self, opts, url):
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])

    async def process_bunkr(self, url, status_msg, update_callback):
        scraper = BunkrScraper()
        media_pages = await scraper.scrape_album(url)
        
        if not media_pages:
            raise Exception("No media found (Album might be empty or Bot blocked).")

        total = len(media_pages)
        await status_msg.edit(f"<b>ðŸ“¦ Bunkr Album: {total} files found.</b>")
        
        for i, page in enumerate(media_pages, 1):
            try:
                real_url = await scraper.get_real_link(page)
                if real_url:
                    await update_callback(f"Bunkr File {i}/{total}")
                    await self.download_generic(real_url)
            except Exception as e:
                log.error(f"Bunkr item fail: {e}")
