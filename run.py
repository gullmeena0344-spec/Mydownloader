import hashlib
import math
import os
import stat
import subprocess
import requests
from threading import Lock
from pathvalidate import sanitize_filename

class File:
    def __init__(self, link, dest):
        self.link = link
        self.dest = dest

class Downloader:
    def __init__(self, token):
        self.token = token
        self.lock = Lock()

    def _ffmpeg(self):
        return "ffmpeg"

    def _generate_thumb(self, video):
        jpg = video + ".jpg"
        if os.path.exists(jpg):
            return jpg
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", video, "-ss", "00:00:03", "-vframes", "1", jpg],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            if os.path.exists(jpg):
                return jpg
        except:
            pass
        return None

    def download(self, file, threads=1, on_part_ready=None):
        r = requests.head(file.link, headers={"Cookie": f"accountToken={self.token}"})
        size = int(r.headers["Content-Length"])
        split = 2 * 1024 * 1024 * 1024

        os.makedirs(os.path.dirname(file.dest), exist_ok=True)
        base, ext = os.path.splitext(file.dest)

        parts = math.ceil(size / split)
        for i in range(parts):
            start = i * split
            end = min(start + split - 1, size - 1)

            part = f"{base}.part{i+1:03d}{ext}" if parts > 1 else file.dest
            with requests.get(
                file.link,
                headers={
                    "Cookie": f"accountToken={self.token}",
                    "Range": f"bytes={start}-{end}"
                },
                stream=True
            ) as rr:
                with open(part, "wb") as f:
                    for c in rr.iter_content(8192):
                        if c:
                            f.write(c)

            if ext.lower() in [".mp4", ".mkv", ".mov", ".m4v"]:
                self._generate_thumb(part)

            if on_part_ready:
                on_part_ready(part, i + 1, parts, end - start + 1)

class GoFile:
    def __init__(self):
        self.token = ""

    def get_files(self, dir, content_id=None):
        if not self.token:
            self.token = requests.post("https://api.gofile.io/accounts").json()["data"]["token"]

        data = requests.get(
            f"https://api.gofile.io/contents/{content_id}",
            headers={"Authorization": f"Bearer {self.token}"}
        ).json()

        files = []
        for c in data["data"]["children"].values():
            if c["type"] == "file":
                files.append(
                    File(
                        c["link"],
                        os.path.join(dir, sanitize_filename(c["name"]))
                    )
                )
        return files
