#!/usr/bin/env python3

import argparse
import os
import requests
from concurrent.futures import ThreadPoolExecutor

API = "https://api.gofile.io"

def get_token():
    return requests.get(f"{API}/getAccountToken").json()["data"]["token"]

def get_content(url):
    cid = url.rstrip("/").split("/")[-1]
    return requests.get(f"{API}/getContent", params={"contentId": cid}).json()

def download_file(url, path):
    r = requests.get(url, stream=True)
    with open(path, "wb") as f:
        for chunk in r.iter_content(8192):
            if chunk:
                f.write(chunk)

def main():
    p = argparse.ArgumentParser()
    p.add_argument("url")
    p.add_argument("-d", "--dir", default="downloads")
    p.add_argument("-t", "--threads", type=int, default=4)
    args = p.parse_args()

    os.makedirs(args.dir, exist_ok=True)

    data = get_content(args.url)["data"]["contents"]
    files = [v for v in data.values() if v["type"] == "file"]

    with ThreadPoolExecutor(max_workers=args.threads) as ex:
        for f in files:
            path = os.path.join(args.dir, f["name"])
            ex.submit(download_file, f["link"], path)

if __name__ == "__main__":
    main()
