import argparse
import fnmatch
import hashlib
import logging
import math
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
import subprocess
import requests
from pathvalidate import sanitize_filename
import shutil
from tqdm import tqdm

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s][%(funcName)20s()][%(levelname)-8s]: %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger("GoFile")

class File:
    def __init__(self, link: str, dest: str):
        self.link = link
        self.dest = dest

    def __str__(self):
        return f"{self.dest} ({self.link})"

class Downloader:
    def __init__(self, token):
        self.token = token
        self.progress_lock = Lock()
        self.progress_bar = None

    def _get_total_size(self, link):
        r = requests.head(link, headers={"Cookie": f"accountToken={self.token}"})
        r.raise_for_status()
        return int(r.headers["Content-Length"]), r.headers.get("Accept-Ranges", "none") == "bytes"

    def _download_range(self, link, start, end, temp_file, i):
        headers = {
            "Cookie": f"accountToken={self.token}",
            "Range": f"bytes={start}-{end}"
        }
        with requests.get(link, headers=headers, stream=True) as r:
            r.raise_for_status()
            with open(temp_file, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        with self.progress_lock:
                            if self.progress_bar:
                                self.progress_bar.update(len(chunk))
        return i

    def _generate_thumbnail(self, video_path):
        """
        Extracts a thumbnail image from the video.
        Returns the path to the jpg file.
        """
        ffmpeg_bin = "./ffmpeg_static"
        if not os.path.exists(ffmpeg_bin):
            ffmpeg_bin = "ffmpeg"

        thumb_path = video_path + ".jpg"
        
        try:
            # Extract frame at 00:00:05
            subprocess.run([
                ffmpeg_bin, "-y", 
                "-ss", "00:00:05", 
                "-i", video_path, 
                "-frames:v", "1", 
                "-q:v", "2", 
                thumb_path
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            
            if os.path.exists(thumb_path):
                return thumb_path
        except Exception as e:
            logger.warning(f"Could not generate external thumbnail: {e}")
        
        return None

    def _embed_thumbnail_internal(self, video_path, thumb_path):
        """
        Embeds the JPG into the MP4 container.
        """
        ffmpeg_bin = "./ffmpeg_static"
        if not os.path.exists(ffmpeg_bin):
            ffmpeg_bin = "ffmpeg"

        temp_output = video_path + ".temp.mp4"

        try:
            cmd = [
                ffmpeg_bin, "-y",
                "-i", video_path,
                "-i", thumb_path,
                "-map", "0",
                "-map", "1",
                "-c", "copy",
                "-disposition:v:1", "attached_pic",
                "-movflags", "+faststart",
                "-ignore_unknown",
                temp_output
            ]

            subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True
            )
            
            # If successful, replace original
            if os.path.exists(temp_output):
                os.remove(video_path)
                os.rename(temp_output, video_path)
                return True
        except Exception as e:
            logger.error(f"Embedding failed (likely disk full): {e}")
            if os.path.exists(temp_output):
                os.remove(temp_output)
        
        return False

    def download(self, file: File, num_threads=1, on_part_ready=None):
        link = file.link
        dest = file.dest

        try:
            total_size, is_support_range = self._get_total_size(link)

            # --- LIMITS ---
            # 1.6 GB Limit: Files smaller than this get the thumbnail EMBEDDED (Best quality).
            # Files larger than this get a SEPARATE .jpg file (Safe for disk).
            embed_limit = int(1.6 * 1024 * 1024 * 1024)
            
            # 2.0 GB Limit: Telegram Standard Upload Limit.
            split_limit = int(2.0 * 1024 * 1024 * 1024)
            
            needs_splitting = total_size > split_limit

            display_name = os.path.basename(dest)
            self.progress_bar = tqdm(
                total=total_size,
                unit='B',
                unit_scale=True,
                desc=f'Downloading {display_name[:25]}'
            )

            os.makedirs(os.path.dirname(dest), exist_ok=True)
            base, ext = os.path.splitext(dest)

            if not needs_splitting:
                # --- SINGLE FILE ---
                self._download_range(link, 0, total_size - 1, dest, 0)
                
                # Check if video format
                if ext.lower() in ['.mp4', '.mkv', '.avi', '.mov', '.m4v']:
                    # 1. Always generate a JPG first
                    thumb_jpg = self._generate_thumbnail(dest)
                    
                    # 2. If small enough, try to embed it
                    if total_size <= embed_limit and thumb_jpg:
                        success = self._embed_thumbnail_internal(dest, thumb_jpg)
                        if success:
                            # If embedded successfully, we don't need the external jpg anymore
                            os.remove(thumb_jpg)
                    
                    # If file was > 1.6GB, we skip embedding. 
                    # We leave the .jpg file there. The uploader bot should pick it up.

                if on_part_ready:
                    on_part_ready(dest, 1, 1, total_size)

            else:
                # --- SPLIT FILES ---
                parts = math.ceil(total_size / split_limit)

                for i in range(parts):
                    start = i * split_limit
                    end = min(start + split_limit - 1, total_size - 1)

                    final_part = f"{base}.part{i+1:03d}{ext}"
                    self._download_range(link, start, end, final_part, i)

                    # Try to generate thumbnail ONLY for Part 1 (which has the header)
                    if i == 0 and ext.lower() in ['.mp4', '.mkv', '.avi', '.mov', '.m4v']:
                        # We just leave the .jpg there for the uploader
                        self._generate_thumbnail(final_part)

                    if on_part_ready:
                        on_part_ready(final_part, i + 1, parts, end - start + 1)

            self.progress_bar.close()

        except Exception as e:
            if self.progress_bar:
                self.progress_bar.close()
            logger.error(f"failed to download ({e}): {dest} ({link})")
            if os.path.exists(dest):
                try: os.remove(dest)
                except: pass
            raise

class GoFileMeta(type):
    _instances = {}

    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            instance = super().__call__(*args, **kwargs)
            cls._instances[cls] = instance
        return cls._instances[cls]

class GoFile(metaclass=GoFileMeta):
    def __init__(self) -> None:
        self.token = ""
        self.wt = ""
        self.lock = Lock()

    def update_token(self) -> None:
        if self.token == "":
            data = requests.post("https://api.gofile.io/accounts").json()
            if data["status"] == "ok":
                self.token = data["data"]["token"]
            else:
                raise Exception("cannot get token")

    def update_wt(self) -> None:
        if self.wt == "":
            alljs = requests.get("https://gofile.io/dist/js/config.js").text
            self.wt = alljs.split('appdata.wt = "')[1].split('"')[0]

    def execute(
        self,
        dir: str,
        content_id: str = None,
        url: str = None,
        password: str = None,
        proxy: str = None,
        num_threads: int = 1,
        includes: list[str] = None,
        excludes: list[str] = None
    ) -> None:

        files = self.get_files(dir, content_id, url, password, includes, excludes)
        for file in files:
            Downloader(token=self.token).download(file, num_threads=1)

    def is_included(self, filename: str, includes: list[str]) -> bool:
        return True if not includes else any(fnmatch.fnmatch(filename, p) for p in includes)

    def is_excluded(self, filename: str, excludes: list[str]) -> bool:
        return False if not excludes else any(fnmatch.fnmatch(filename, p) for p in excludes)

    def get_files(
        self,
        dir: str,
        content_id: str = None,
        url: str = None,
        password: str = None,
        includes: list[str] = None,
        excludes: list[str] = None
    ) -> list[File]:

        includes = includes or []
        excludes = excludes or []
        files = []

        if content_id:
            self.update_token()
            self.update_wt()

            hash_password = hashlib.sha256(password.encode()).hexdigest() if password else ""
            data = requests.get(
                f"https://api.gofile.io/contents/{content_id}?cache=true&password={hash_password}",
                headers={
                    "Authorization": "Bearer " + self.token,
                    "X-Website-Token": self.wt,
                },
            ).json()

            if data["status"] == "ok":
                if data["data"]["type"] == "folder":
                    dirname = sanitize_filename(data["data"]["name"])
                    dir = os.path.join(dir, dirname)
                    for cid, child in data["data"]["children"].items():
                        if child["type"] == "file":
                            name = child["name"]
                            files.append(File(child["link"], os.path.join(dir, sanitize_filename(name))))
                        elif child["type"] == "folder":
                            files.extend(self.get_files(
                                dir, 
                                content_id=child["id"], 
                                password=password, 
                                includes=includes, 
                                excludes=excludes
                            ))
                else:
                    name = data["data"]["name"]
                    files.append(File(data["data"]["link"], os.path.join(dir, sanitize_filename(name))))

        elif url and "gofile.io/d/" in url:
            content_id = url.split("/d/")[-1].split("?")[0].strip("/")
            files = self.get_files(dir, content_id=content_id, password=password)

        return files

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("-d", type=str, dest="dir", default="./output")
    args = parser.parse_args()

    GoFile().execute(dir=args.dir, url=args.url)
