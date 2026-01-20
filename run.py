import argparse
import fnmatch
import hashlib
import logging
import math
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

import requests
from pathvalidate import sanitize_filename
import shutil
from tqdm import tqdm


# ---------------- CONFIG ----------------

MAX_TMP_BYTES = int(3.5 * 1024 * 1024 * 1024)  # 3.5GB hard safety cap

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s][%(funcName)20s()][%(levelname)-8s]: %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger("GoFile")


# ---------------- HELPERS ----------------

def disk_used_bytes(base="."):
    total = 0
    for root, _, files in os.walk(base):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except:
                pass
    return total


# ---------------- CLASSES ----------------

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

    def _check_disk_limit(self):
        if disk_used_bytes() >= MAX_TMP_BYTES:
            raise RuntimeError("Disk limit reached")

    def _get_total_size(self, link):
        r = requests.head(link, headers={"Cookie": f"accountToken={self.token}"})
        r.raise_for_status()
        return int(r.headers["Content-Length"]), r.headers.get("Accept-Ranges") == "bytes"

    def _download_range(self, link, start, end, temp_file, i):
        existing = os.path.getsize(temp_file) if os.path.exists(temp_file) else 0
        range_start = start + existing
        if range_start > end:
            return i

        headers = {
            "Cookie": f"accountToken={self.token}",
            "Range": f"bytes={range_start}-{end}"
        }

        with requests.get(link, headers=headers, stream=True) as r:
            r.raise_for_status()
            with open(temp_file, "ab") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        self._check_disk_limit()
                        f.write(chunk)
                        with self.progress_lock:
                            self.progress_bar.update(len(chunk))
        return i

    def _merge_temp_files(self, temp_dir, dest, num_threads):
        with open(dest, "wb") as out:
            for i in range(num_threads):
                part = os.path.join(temp_dir, f"part_{i}")
                with open(part, "rb") as pf:
                    shutil.copyfileobj(pf, out)
                os.remove(part)
        shutil.rmtree(temp_dir)

    def download(self, file: File, num_threads=1):
        link = file.link
        dest = file.dest
        temp_dir = dest + "_parts"

        try:
            total_size, support_range = self._get_total_size(link)

            if os.path.exists(dest) and os.path.getsize(dest) == total_size:
                return

            os.makedirs(os.path.dirname(dest), exist_ok=True)

            # ---------- SINGLE THREAD ----------
            if num_threads == 1 or not support_range:
                temp_file = dest + ".part"
                downloaded = os.path.getsize(temp_file) if os.path.exists(temp_file) else 0

                self.progress_bar = tqdm(
                    total=total_size,
                    initial=downloaded,
                    unit="B",
                    unit_scale=True,
                    desc=f"Downloading {os.path.basename(dest)}"
                )

                headers = {
                    "Cookie": f"accountToken={self.token}",
                    "Range": f"bytes={downloaded}-"
                }

                with requests.get(link, headers=headers, stream=True) as r:
                    r.raise_for_status()
                    with open(temp_file, "ab") as f:
                        for chunk in r.iter_content(chunk_size=8192):
                            if chunk:
                                self._check_disk_limit()
                                f.write(chunk)
                                self.progress_bar.update(len(chunk))

                self.progress_bar.close()
                os.rename(temp_file, dest)

            # ---------- MULTI THREAD ----------
            else:
                if os.path.exists(dest + ".part"):
                    os.remove(dest + ".part")

                if not os.path.exists(temp_dir):
                    os.makedirs(temp_dir, exist_ok=True)

                part_size = math.ceil(total_size / num_threads)
                downloaded = sum(
                    os.path.getsize(os.path.join(temp_dir, f"part_{i}"))
                    for i in range(num_threads)
                    if os.path.exists(os.path.join(temp_dir, f"part_{i}"))
                )

                self.progress_bar = tqdm(
                    total=total_size,
                    initial=downloaded,
                    unit="B",
                    unit_scale=True,
                    desc=f"Downloading {os.path.basename(dest)}"
                )

                with ThreadPoolExecutor(max_workers=num_threads) as exe:
                    futures = []
                    for i in range(num_threads):
                        start = i * part_size
                        end = min(start + part_size - 1, total_size - 1)
                        futures.append(
                            exe.submit(
                                self._download_range,
                                link, start, end,
                                os.path.join(temp_dir, f"part_{i}"),
                                i
                            )
                        )
                    for f in as_completed(futures):
                        f.result()

                self.progress_bar.close()
                self._merge_temp_files(temp_dir, dest, num_threads)

        except RuntimeError:
            if self.progress_bar:
                self.progress_bar.close()
            logger.warning("Disk limit reached — download paused safely")
            raise

        except Exception as e:
            if self.progress_bar:
                self.progress_bar.close()
            logger.error(f"Download failed: {e}")


# ---------------- GOFILE CORE ----------------

class GoFileMeta(type):
    _instances = {}

    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            cls._instances[cls] = super().__call__(*args, **kwargs)
        return cls._instances[cls]


class GoFile(metaclass=GoFileMeta):
    def __init__(self):
        self.token = ""
        self.wt = ""
        self.lock = Lock()

    def update_token(self):
        if not self.token:
            data = requests.post("https://api.gofile.io/accounts").json()
            if data["status"] == "ok":
                self.token = data["data"]["token"]
            else:
                raise Exception("Token fetch failed")

    def update_wt(self):
        if not self.wt:
            js = requests.get("https://gofile.io/dist/js/config.js").text
            self.wt = js.split('appdata.wt = "')[1].split('"')[0]

    def execute(self, dir, content_id=None, url=None, password=None,
                proxy=None, num_threads=1, includes=None, excludes=None):

        files = self.get_files(dir, content_id, url, password, includes, excludes)
        for f in files:
            Downloader(self.token).download(f, num_threads)

    def is_included(self, name, includes):
        return True if not includes else any(fnmatch.fnmatch(name, p) for p in includes)

    def is_excluded(self, name, excludes):
        return False if not excludes else any(fnmatch.fnmatch(name, p) for p in excludes)

    def get_files(self, dir, content_id=None, url=None, password=None,
                  includes=None, excludes=None):

        includes = includes or []
        excludes = excludes or []
        files = []

        if content_id:
            self.update_token()
            self.update_wt()

            pwd = hashlib.sha256(password.encode()).hexdigest() if password else ""
            data = requests.get(
                f"https://api.gofile.io/contents/{content_id}?cache=true&password={pwd}",
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "X-Website-Token": self.wt
                }
            ).json()

            if data["status"] != "ok":
                return files

            for _, item in data["data"]["children"].items():
                if item["type"] == "file":
                    if self.is_included(item["name"], includes) and not self.is_excluded(item["name"], excludes):
                        files.append(
                            File(item["link"], os.path.join(dir, sanitize_filename(item["name"])))
                        )
        return files


# ---------------- CLI ----------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("-t", type=int, default=1)
    parser.add_argument("-d", type=str, default="./output")
    args = parser.parse_args()

    GoFile().execute(dir=args.d, url=args.url, num_threads=args.t)
