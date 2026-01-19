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
                            if self.progress_bar:
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

            if os.path.exists(dest) and os.path.getsize(dest) == total_size:
                return

            # ================= SINGLE THREAD =================

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

            # ================= MULTI THREAD (PATCHED) =================

            else:
                if os.path.exists(dest + ".part"):
                    os.remove(dest + ".part")

                check_file = os.path.join(temp_dir, "num_threads")
                if os.path.exists(temp_dir):
                    prev_num_threads = None
                    if os.path.exists(check_file):
                        with open(check_file) as f:
                            prev_num_threads = int(f.read())
                    if prev_num_threads != num_threads:
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

                self.progress_bar = tqdm(
                    total=total_size,
                    initial=downloaded_bytes,
                    unit='B',
                    unit_scale=True,
                    desc=f'Downloading {os.path.basename(dest)[:25]}'
                )

                with ThreadPoolExecutor(max_workers=num_threads) as executor:
                    futures = []
                    for i in range(num_threads):
                        start = part_size * i
                        end = min(start + part_size - 1, total_size - 1)
                        temp_file = os.path.join(temp_dir, f"part_{i}")

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

                    for _ in as_completed(futures):
                        pass

                self.progress_bar.close()
                self._merge_temp_files(temp_dir, dest, num_threads)

        except Exception as e:
            logger.error(f"Download failed: {e}")
            raise
