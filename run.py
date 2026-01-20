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


logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s][%(funcName)20s()][%(levelname)-8s]: %(message)s",
    handlers=[
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("GoFile")

MAX_CHUNK_SIZE = 2 * 1024 * 1024 * 1024  # 2 GB


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

    def _download_range(self, link, start, end, out_file):
        headers = {
            "Cookie": f"accountToken={self.token}",
            "Range": f"bytes={start}-{end}"
        }
        with requests.get(link, headers=headers, stream=True) as r:
            r.raise_for_status()
            with open(out_file, "ab") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        with self.progress_lock:
                            self.progress_bar.update(len(chunk))

    def download(self, file: File, num_threads=1):
        link = file.link
        dest = file.dest

        total_size, is_support_range = self._get_total_size(link)
        os.makedirs(os.path.dirname(dest), exist_ok=True)

        parts = math.ceil(total_size / MAX_CHUNK_SIZE)

        display_name = os.path.basename(dest)
        self.progress_bar = tqdm(
            total=total_size,
            unit='B',
            unit_scale=True,
            desc=f'Downloading {display_name}'
        )

        try:
            for i in range(parts):
                start = i * MAX_CHUNK_SIZE
                end = min(start + MAX_CHUNK_SIZE - 1, total_size - 1)
                part_path = f"{dest}.part{i+1:03d}"

                if os.path.exists(part_path) and os.path.getsize(part_path) == (end - start + 1):
                    self.progress_bar.update(end - start + 1)
                    continue

                self._download_range(link, start, end, part_path)

        finally:
            self.progress_bar.close()


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
            if 'appdata.wt = "' in alljs:
                self.wt = alljs.split('appdata.wt = "')[1].split('"')[0]
            else:
                raise Exception("cannot get wt")

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
                for child in data["data"]["children"].values():
                    if child["type"] == "file":
                        name = sanitize_filename(child["name"])
                        files.append(File(child["link"], os.path.join(dir, name)))

        elif url and "gofile.io/d/" in url:
            content_id = url.split("/d/")[-1].split("?")[0].strip("/")
            files = self.get_files(dir, content_id=content_id, password=password)

        return files
