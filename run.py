import os
import math
import logging
import shutil
import requests
from threading import Lock
from tqdm import tqdm
from pathvalidate import sanitize_filename

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s][%(levelname)-8s]: %(message)s",
)
logger = logging.getLogger("GoFile")

class File:
    def __init__(self, link: str, dest: str):
        self.link = link
        self.dest = dest

class GoFile:
    def __init__(self):
        self.token = os.getenv("GOFILE_TOKEN")
        self.lock = Lock()

    def _get_size(self, link):
        r = requests.head(link, headers={"Cookie": f"accountToken={self.token}"})
        r.raise_for_status()
        return int(r.headers.get("Content-Length", 0))

    def execute(self, dir, url, num_threads=1):
        os.makedirs(dir, exist_ok=True)

        api = requests.get(f"https://api.gofile.io/getContent?contentId={url.split('/')[-1]}").json()
        files = api["data"]["contents"].values()

        for f in files:
            if f["type"] != "file":
                continue

            name = sanitize_filename(f["name"])
            link = f["link"]
            dest = os.path.join(dir, name)

            total = self._get_size(link)
            bar = tqdm(total=total, unit="B", unit_scale=True, desc=name)

            headers = {"Cookie": f"accountToken={self.token}"}

            with requests.get(link, headers=headers, stream=True) as r:
                r.raise_for_status()
                with open(dest, "wb") as out:
                    for chunk in r.iter_content(8192):
                        if chunk:
                            out.write(chunk)
                            bar.update(len(chunk))

            bar.close()
