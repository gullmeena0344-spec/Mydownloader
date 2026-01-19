#!/usr/bin/env python3
import os
import sys
import time
import json
import math
import queue
import shutil
import logging
import threading
from dataclasses import dataclass
from typing import List, Tuple

import requests
from tqdm import tqdm

# ================= CONFIG =================

GOFILE_API = "https://api.gofile.io"
CHUNK_SIZE = 2 * 1024 * 1024 * 1024  # 2GB
USER_AGENT = "Mozilla/5.0 (Downloader)"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger(__name__)

# ================= DATA =================

@dataclass
class File:
    name: str
    link: str
    size: int
    dest: str

# ================= GOFILE =================

class GoFile:
    def __init__(self):
        self.token = None

    def update_token(self):
        r = requests.get(f"{GOFILE_API}/accounts/getAccountId")
        r.raise_for_status()
        self.token = r.json()["data"]["token"]
        return self.token

    def get_content(self, url: str):
        content_id = url.rstrip("/").split("/")[-1]
        r = requests.get(
            f"{GOFILE_API}/contents/{content_id}",
            params={"token": self.token}
        )
        r.raise_for_status()
        return r.json()["data"]

    def get_files(self, url: str, output="downloads") -> List[File]:
        data = self.get_content(url)
        files = []

        def walk(node, base):
            for item in node.values():
                if item["type"] == "file":
                    dest = os.path.join(output, item["name"])
                    files.append(
                        File(
                            name=item["name"],
                            link=item["link"],
                            size=int(item["size"]),
                            dest=dest
                        )
                    )
                elif item["type"] == "folder":
                    walk(item["children"], base)

        walk(data["children"], "")
        return files

# ================= DOWNLOADER =================

class Downloader:
    def __init__(self, token: str):
        self.token = token

    def _get_total_size(self, link: str) -> Tuple[int, bool]:
        r = requests.head(
            link,
            headers={"Cookie": f"accountToken={self.token}"},
            allow_redirects=True
        )
        size = int(r.headers.get("Content-Length", 0))
        support_range = "bytes" in r.headers.get("Accept-Ranges", "").lower()
        return size, support_range

    # -------- OLD FULL DOWNLOAD (UNCHANGED) --------
    def download(self, file: File):
        os.makedirs(os.path.dirname(file.dest), exist_ok=True)

        headers = {"Cookie": f"accountToken={self.token}"}
        with requests.get(file.link, headers=headers, stream=True) as r:
            r.raise_for_status()
            with open(file.dest, "wb") as f, tqdm(
                total=file.size,
                unit="B",
                unit_scale=True,
                desc=file.name
            ) as bar:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)
                        bar.update(len(chunk))
        return file.dest

    # -------- NEW CHUNKED RANGE DOWNLOAD --------
    def download_in_chunks(self, file: File, chunk_size=CHUNK_SIZE):
        """
        Generator: downloads file in ranged chunks.
        NEVER creates full file on disk.
        """

        total_size, ranged = self._get_total_size(file.link)
        if not ranged:
            raise Exception("Server does not support ranged downloads")

        downloaded = 0
        part = 1

        os.makedirs(os.path.dirname(file.dest), exist_ok=True)

        while downloaded < total_size:
            start = downloaded
            end = min(start + chunk_size - 1, total_size - 1)

            part_path = f"{file.dest}.part{part}"

            headers = {
                "Cookie": f"accountToken={self.token}",
                "Range": f"bytes={start}-{end}",
                "User-Agent": USER_AGENT
            }

            logger.info(
                f"Chunk {part}: {start // (1024**2)}MB → {end // (1024**2)}MB"
            )

            with requests.get(file.link, headers=headers, stream=True) as r:
                r.raise_for_status()
                with open(part_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            f.write(chunk)

            yield part_path  # upload this → then delete

            downloaded += (end - start + 1)
            part += 1

# ================= CLI =================

def main():
    if len(sys.argv) < 2:
        print("Usage: python run.py <gofile_url>")
        sys.exit(1)

    url = sys.argv[1]

    gf = GoFile()
    gf.update_token()

    files = gf.get_files(url)

    dl = Downloader(gf.token)

    for f in files:
        logger.info(f"Downloading: {f.name}")
        dl.download(f)

if __name__ == "__main__":
    main()
