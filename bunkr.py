import cloudscraper
import json
import re
import base64
from math import floor
from urllib.parse import unquote, urlparse, urljoin
from bs4 import BeautifulSoup

class Bunkr:
    def __init__(self):
        self.scraper = cloudscraper.create_scraper(
            browser={'browser': 'chrome', 'platform': 'windows', 'mobile': False}
        )
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': '*/*',
        }
        self.SECRET_KEY_BASE = "SECRET_KEY_"

    # ==========================
    # 1. BUNKR LOGIC
    # ==========================
    def _bunkr_get_api_url(self, main_url):
        parsed = urlparse(main_url)
        return f"{parsed.scheme}://{parsed.netloc}/api/vs", f"{parsed.scheme}://{parsed.netloc}/"

    def _bunkr_decrypt(self, encryption_data):
        try:
            timestamp = encryption_data['timestamp']
            secret_key = f"{self.SECRET_KEY_BASE}{floor(timestamp / 3600)}"
            encrypted_data = base64.b64decode(encryption_data['url'])
            key_bytes = secret_key.encode('utf-8')
            decrypted = []
            for i, byte in enumerate(encrypted_data):
                decrypted.append(chr(byte ^ key_bytes[i % len(key_bytes)]))
            return "".join(decrypted)
        except: return None

    def _scrape_bunkr(self, url):
        print(f"Scraper: Bunkr -> {url}")
        api_url, referer = self._bunkr_get_api_url(url)
        try:
            r = self.scraper.get(url, headers={'Referer': referer}, timeout=15)
            soup = BeautifulSoup(r.text, 'html.parser')
            files_map = {} 
            slug_regex = r'/(?:v|i|f)/([a-zA-Z0-9\-_]+)'

            match_self = re.search(slug_regex, url)
            if match_self and ("Download" in r.text or soup.find('div', {'class': 'lightgallery'})):
                slug = match_self.group(1)
                h1 = soup.find('h1')
                files_map[slug] = h1.text.strip() if h1 else f"bunkr_{slug}.mp4"

            links = soup.find_all('a', href=re.compile(slug_regex))
            for link in links:
                match = re.search(slug_regex, link.get('href'))
                if not match: continue
                slug = match.group(1)
                
                name = None
                if link.text.strip() and "Download" not in link.text: name = link.text.strip()
                if not name and link.find_parent('div'):
                    txt = link.find_parent('div').get_text(strip=True, separator=" ")
                    clean = re.sub(r'\d{1,2}:\d{2}', '', txt).strip()
                    if len(clean) > 2: name = clean
                
                files_map[slug] = name or f"bunkr_{slug}.mp4"

            results = []
            for slug, name in files_map.items():
                name = re.sub(r'[^\w\-. ]', '', name.replace("Watch", "").strip())
                if not name.endswith(('.mp4', '.jpg', '.png', '.mkv')): name += ".mp4"
                
                try:
                    h = self.headers.copy()
                    h['Referer'] = referer
                    api = self.scraper.post(api_url, json={'slug': slug}, headers=h, timeout=10)
                    if api.status_code == 200:
                        direct = self._bunkr_decrypt(api.json())
                        if direct: results.append({'url': direct, 'name': name, 'referer': referer})
                except: pass
            return results
        except Exception as e:
            print(f"Bunkr Error: {e}")
            return []

    # ==========================
    # 2. IMGCHEST LOGIC (FIXED)
    # ==========================
    def _scrape_imgchest(self, url):
        print(f"Scraper: Imgchest -> {url}")
        try:
            r = self.scraper.get(url, headers=self.headers, timeout=15)
            soup = BeautifulSoup(r.text, 'html.parser')
            results = []
            
            # Title
            h1 = soup.find('h1')
            album_name = re.sub(r'[^\w\-. ]', '', h1.text.strip()) if h1 else "imgchest_album"

            # Method: Find 'post-image' containers (Robust)
            containers = soup.find_all('div', class_='post-image')
            
            # Fallback: Find specific image links
            if not containers:
                containers = soup.find_all('a', class_='image-link')

            count = 0
            seen_urls = set()

            for item in containers:
                target_url = None
                
                # Check anchor href first (usually max res)
                if item.name == 'a':
                    target_url = item.get('href')
                elif item.name == 'div':
                    a_tag = item.find('a')
                    if a_tag: target_url = a_tag.get('href')
                    else:
                        img_tag = item.find('img')
                        if img_tag: target_url = img_tag.get('src')

                if not target_url: continue

                # Normalize URL
                if target_url.startswith("//"): target_url = "https:" + target_url
                target_url = target_url.split('?')[0] # Remove size params

                if target_url in seen_urls: continue
                seen_urls.add(target_url)

                # Extension
                ext = target_url.split('.')[-1]
                if len(ext) > 4: ext = "jpg"
                
                count += 1
                name = f"{album_name}_{count:03d}.{ext}"
                
                results.append({
                    'url': target_url,
                    'name': name,
                    'referer': url
                })

            print(f"Imgchest: Found {len(results)} images.")
            return results
        except Exception as e:
            print(f"Imgchest Error: {e}")
            return []

    # ==========================
    # 3. CYBERDROP & EROME
    # ==========================
    def _scrape_cyberdrop(self, url):
        try:
            r = self.scraper.get(url, headers=self.headers, timeout=15)
            soup = BeautifulSoup(r.text, 'html.parser')
            results = []
            links = soup.find_all('a', class_='image') or soup.find_all('a', href=re.compile(r'\.(mp4|jpg|png|jpeg|mkv)$', re.I))
            for link in links:
                href = link.get('href')
                if not href: continue
                full_url = urljoin(url, href)
                name = link.get('title') or link.text.strip() or href.split('/')[-1]
                name = re.sub(r'[^\w\-. ]', '', name)
                if not name: name = f"cyber_{len(results)}.mp4"
                results.append({'url': full_url, 'name': name, 'referer': url})
            return results
        except: return []

    def _scrape_erome(self, url):
        try:
            r = self.scraper.get(url, headers=self.headers, timeout=15)
            soup = BeautifulSoup(r.text, 'html.parser')
            results = []
            title = soup.find('h1')
            base_name = re.sub(r'[^\w\-. ]', '', title.text.strip()) if title else "erome"
            
            # Videos
            for i, v in enumerate(soup.find_all('source')):
                src = v.get('src')
                if src: results.append({'url': src, 'name': f"{base_name}_vid_{i}.mp4", 'referer': url})
            # Images
            for i, img in enumerate(soup.find_all('img', class_='img-front')):
                src = img.get('data-src') or img.get('src')
                if src:
                    ext = src.split('.')[-1]
                    results.append({'url': src, 'name': f"{base_name}_img_{i}.{ext}", 'referer': url})
            return results
        except: return []

    # ==========================
    # ROUTER
    # ==========================
    def get_files(self, url):
        url_lower = url.lower()
        if "bunkr" in url_lower: return self._scrape_bunkr(url)
        elif "imgchest" in url_lower: return self._scrape_imgchest(url)
        elif any(x in url_lower for x in ["cyberdrop", "cyberfile"]): return self._scrape_cyberdrop(url)
        elif "erome" in url_lower: return self._scrape_erome(url)
        return []
