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

    def _ensure_dir(self, filepath):
        dir_path = os.path.dirname(filepath)
        if dir_path:
            os.makedirs(dir_path, exist_ok=True)

    def _download_range(self, link, start, end, temp_file, i):
        self._ensure_dir(temp_file)
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

    def _make_streamable(self, src, dst):
        self._ensure_dir(dst)
        ffmpeg_bin = "./ffmpeg_static"
        if not os.path.exists(ffmpeg_bin):
            ffmpeg_bin = "ffmpeg"

        cmd = [
            ffmpeg_bin,
            "-y",
            "-i", src,
            "-map", "0",
            "-c", "copy",
            "-ignore_unknown",
        ]

        if dst.lower().endswith(('.mp4', '.mov', '.m4v')):
            cmd.extend(["-movflags", "+faststart"])

        cmd.append(dst)

        subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True
        )

    def download(self, file: File, num_threads=1, on_part_ready=None):
        link = file.link
        dest = file.dest

        try:
            total_size, is_support_range = self._get_total_size(link)

            ffmpeg_limit = int(1.9 * 1024 * 1024 * 1024)
            part_size = int(2.5 * 1024 * 1024 * 1024)
            needs_splitting = total_size > part_size

            display_name = os.path.basename(dest)
            self.progress_bar = tqdm(
                total=total_size,
                unit='B',
                unit_scale=True,
                desc=f'Downloading {display_name[:25]}'
            )

            self._ensure_dir(dest)
            base, ext = os.path.splitext(dest)

            if not needs_splitting:
                if total_size <= ffmpeg_limit and ext.lower() in ['.mp4', '.mkv', '.avi', '.mov', '.webm', '.flv', '.m4v']:
                    raw_file = f"{base}.raw{ext}"
                    final_file = dest

                    self._download_range(link, 0, total_size - 1, raw_file, 0)

                    try:
                        self._make_streamable(raw_file, final_file)
                        if os.path.exists(raw_file):
                            os.remove(raw_file)
                    except Exception as e:
                        logger.error(f"FFmpeg failed or not found, keeping raw file: {e}")
                        if os.path.exists(raw_file):
                            os.rename(raw_file, final_file)
                else:
                    self._download_range(link, 0, total_size - 1, dest, 0)

                if on_part_ready:
                    on_part_ready(dest, 1, 1, total_size)

            else:
                parts = math.ceil(total_size / part_size)

                for i in range(parts):
                    start = i * part_size
                    end = min(start + part_size - 1, total_size - 1)

                    final_part = f"{base}.part{i+1:03d}{ext}"

                    self._download_range(link, start, end, final_part, i)

                    if on_part_ready:
                        on_part_ready(final_part, i + 1, parts, end - start + 1)

            self.progress_bar.close()

        except Exception as e:
            if self.progress_bar:
                self.progress_bar.close()
            logger.error(f"failed to download ({e}): {dest} ({link})")
            if os.path.exists(dest):
                try:
                    os.remove(dest)
                except:
                    pass
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
                    os.makedirs(dir, exist_ok=True)
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
                    os.makedirs(dir, exist_ok=True)
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
