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
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger("GoFile")


class File:
    def __init__(self, link: str, dest: str, offset=0, max_size=None):
        self.link = link
        self.dest = dest
        self.offset = offset
        self.max_size = max_size

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

    def download(self, file: File, num_threads=1):
        link = file.link
        dest = file.dest
        offset = file.offset
        max_size = file.max_size

        total_size, support_range = self._get_total_size(link)

        if offset >= total_size:
            return False

        end = total_size - 1
        if max_size:
            end = min(end, offset + max_size - 1)

        os.makedirs(os.path.dirname(dest), exist_ok=True)

        headers = {
            "Cookie": f"accountToken={self.token}",
            "Range": f"bytes={offset}-{end}"
        }

        self.progress_bar = tqdm(
            total=end - offset + 1,
            unit="B",
            unit_scale=True,
            desc=f"Downloading {os.path.basename(dest)}"
        )

        with requests.get(link, headers=headers, stream=True) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        self.progress_bar.update(len(chunk))

        self.progress_bar.close()
        return end + 1 < total_size


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
        if not self.token:
            data = requests.post("https://api.gofile.io/accounts").json()
            if data["status"] == "ok":
                self.token = data["data"]["token"]
            else:
                raise Exception("cannot get token")

    def update_wt(self) -> None:
        if not self.wt:
            js = requests.get("https://gofile.io/dist/js/config.js").text
            self.wt = js.split('appdata.wt = "')[1].split('"')[0]

    def execute(
        self,
        dir: str,
        content_id: str = None,
        url: str = None,
        password: str = None,
        proxy: str = None,
        num_threads: int = 1,
        includes: list[str] = None,
        excludes: list[str] = None,
        offset: int = 0,
        max_size: int = None
    ) -> bool:

        if proxy:
            os.environ['HTTP_PROXY'] = proxy
            os.environ['HTTPS_PROXY'] = proxy
        else:
            os.environ.pop('HTTP_PROXY', None)
            os.environ.pop('HTTPS_PROXY', None)

        files = self.get_files(dir, content_id, url, password, includes, excludes, offset, max_size)
        if not files:
            return False

        more = False
        for file in files:
            more = Downloader(self.token).download(file, num_threads)

        return more

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
        excludes: list[str] = None,
        offset: int = 0,
        max_size: int = None
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

            if data["status"] != "ok":
                return files

            for _, child in data["data"]["children"].items():
                if child["type"] == "file":
                    name = child["name"]
                    if self.is_included(name, includes) and not self.is_excluded(name, excludes):
                        files.append(
                            File(
                                child["link"],
                                os.path.join(dir, sanitize_filename(name)),
                                offset,
                                max_size
                            )
                        )

        elif url and "gofile.io/d/" in url:
            content_id = url.split("/d/")[-1].split("?")[0].strip("/")
            return self.get_files(dir, content_id, password=password, includes=includes, excludes=excludes, offset=offset, max_size=max_size)

        return files


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("-t", type=int, default=1)
    parser.add_argument("-d", type=str, default="./output")
    args = parser.parse_args()

    GoFile().execute(dir=args.d, url=args.url, num_threads=args.t)
