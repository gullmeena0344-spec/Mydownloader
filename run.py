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
        existing_size = os.path.getsize(temp_file) if os.path.exists(temp_file) else 0
        range_start = start + existing_size
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
                        f.write(chunk)
                        with self.progress_lock:
                            self.progress_bar.update(len(chunk))
        return i

    def _merge_temp_files(self, temp_dir, dest, num_threads):
        with open(dest, "wb") as outfile:
            for i in range(num_threads):
                temp_file = os.path.join(temp_dir, f"part_{i}")
                with open(temp_file, "rb") as f:
                    outfile.write(f.read())
                os.remove(temp_file)
        shutil.rmtree(temp_dir)

    def download(self, file: File, num_threads=4):
        link = file.link
        dest = file.dest
        temp_dir = dest + "_parts"
        try:
            total_size, is_support_range = self._get_total_size(link)

            if os.path.exists(dest):
                if os.path.getsize(dest) == total_size:
                    return

            if num_threads == 1 or not is_support_range:
                temp_file = dest + ".part"
                downloaded_bytes = os.path.getsize(temp_file) if os.path.exists(temp_file) else 0

                display_name = (
                    os.path.basename(dest)[:10] + "....." + os.path.basename(dest)[-10:]
                    if len(os.path.basename(dest)) > 25
                    else os.path.basename(dest).rjust(25)
                )

                self.progress_bar = tqdm(
                    total=total_size,
                    initial=downloaded_bytes,
                    unit='B',
                    unit_scale=True,
                    desc=f'Downloading {display_name}'
                )

                headers = {
                    "Cookie": f"accountToken={self.token}",
                    "Range": f"bytes={downloaded_bytes}-"
                }

                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with requests.get(link, headers=headers, stream=True) as r:
                    r.raise_for_status()
                    with open(temp_file, "ab") as f:
                        for chunk in r.iter_content(chunk_size=8192):
                            if chunk:
                                f.write(chunk)
                                self.progress_bar.update(len(chunk))

                self.progress_bar.close()
                os.rename(temp_file, dest)

            else:
                os.path.exists(dest + ".part") and os.remove(dest + ".part")

                check_file = os.path.join(temp_dir, "num_threads")
                if os.path.exists(temp_dir):
                    prev_num_threads = None
                    if os.path.exists(check_file):
                        with open(check_file) as f:
                            prev_num_threads = int(f.read())
                    if prev_num_threads is None or prev_num_threads != num_threads:
                        shutil.rmtree(temp_dir)

                if not os.path.exists(temp_dir):
                    os.makedirs(temp_dir, exist_ok=True)
                    with open(check_file, "w") as f:
                        f.write(str(num_threads))

                part_size = math.ceil(total_size / num_threads)

                downloaded_bytes = sum(
                    os.path.getsize(os.path.join(temp_dir, f"part_{i}"))
                    for i in range(num_threads)
                    if os.path.exists(os.path.join(temp_dir, f"part_{i}"))
                )

                display_name = (
                    os.path.basename(dest)[:10] + "....." + os.path.basename(dest)[-10:]
                    if len(os.path.basename(dest)) > 25
                    else os.path.basename(dest).rjust(25)
                )

                self.progress_bar = tqdm(
                    total=total_size,
                    initial=downloaded_bytes,
                    unit='B',
                    unit_scale=True,
                    desc=f'Downloading {display_name}'
                )

                base, _ = os.path.splitext(dest)

                with ThreadPoolExecutor(max_workers=num_threads) as executor:
                    futures = []
                    for i in range(num_threads):
                        start = i * part_size
                        end = min(start + part_size - 1, total_size - 1)

                        temp_file = f"{base}.part{i+1:03d}.mp4"

                        futures.append(
                            executor.submit(
                                self._download_range,
                                link,
                                start,
                                end,
                                temp_file,
                                i
                            )
                        )

                    for future in as_completed(futures):
                        future.result()

                self.progress_bar.close()

        except Exception as e:
            if self.progress_bar:
                self.progress_bar.close()
            logger.error(f"failed to download ({e}): {dest} ({link})")


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
                logger.info(f"updated token: {self.token}")
            else:
                raise Exception("cannot get token")

    def update_wt(self) -> None:
        if self.wt == "":
            alljs = requests.get("https://gofile.io/dist/js/config.js").text
            if 'appdata.wt = "' in alljs:
                self.wt = alljs.split('appdata.wt = "')[1].split('"')[0]
                logger.info(f"updated wt: {self.wt}")
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

        if proxy:
            os.environ['HTTP_PROXY'] = proxy
            os.environ['HTTPS_PROXY'] = proxy
        else:
            os.environ.pop('HTTP_PROXY', None)
            os.environ.pop('HTTPS_PROXY', None)

        files = self.get_files(dir, content_id, url, password, includes, excludes)
        for file in files:
            Downloader(token=self.token).download(file, num_threads=num_threads)

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
                if data["data"].get("passwordStatus", "passwordOk") == "passwordOk":
                    if data["data"]["type"] == "folder":
                        dirname = sanitize_filename(data["data"]["name"])
                        dir = os.path.join(dir, dirname)

                        for cid, child in data["data"]["children"].items():
                            if child["type"] == "folder":
                                files.extend(self.get_files(dir, cid, password, includes, excludes))
                            else:
                                name = child["name"]
                                if self.is_included(name, includes) and not self.is_excluded(name, excludes):
                                    files.append(File(child["link"], os.path.join(dir, sanitize_filename(name))))
                    else:
                        name = data["data"]["name"]
                        if self.is_included(name, includes) and not self.is_excluded(name, excludes):
                            files.append(File(data["data"]["link"], os.path.join(dir, sanitize_filename(name))))
                else:
                    logger.error("invalid password")

        elif url and "gofile.io/d/" in url:
            content_id = url.split("/d/")[-1].split("?")[0].strip("/")
            files = self.get_files(dir, content_id=content_id, password=password, includes=includes, excludes=excludes)
        else:
            logger.error(f"invalid parameters: content_id={content_id}, url={url}")

        return files


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("url", nargs='?', default=None)
    group.add_argument("-f", type=str, dest="file")
    parser.add_argument("-t", type=int, dest="num_threads")
    parser.add_argument("-d", type=str, dest="dir")
    parser.add_argument("-p", type=str, dest="password")
    parser.add_argument("-x", type=str, dest="proxy")
    parser.add_argument("-i", action="append", dest="includes")
    parser.add_argument("-e", action="append", dest="excludes")
    args = parser.parse_args()

    num_threads = args.num_threads or 1
    dir = args.dir or "./output"

    if args.file:
        with open(args.file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    GoFile().execute(
                        dir,
                        url=line,
                        password=args.password,
                        proxy=args.proxy,
                        num_threads=num_threads,
                        includes=args.includes,
                        excludes=args.excludes,
                    )
    else:
        GoFile().execute(
            dir,
            url=args.url,
            password=args.password,
            proxy=args.proxy,
            num_threads=num_threads,
            includes=args.includes,
            excludes=args.excludes,
        )
