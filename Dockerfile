# Use a lightweight Python base
FROM python:3.10-slim

# Install aria2 and ffmpeg (minimal version) and clean up immediately to save space
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg aria2 && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependencies one-by-one to avoid memory spikes
RUN pip install --no-cache-dir pyrogram
RUN pip install --no-cache-dir tgcrypto
RUN pip install --no-cache-dir yt-dlp

COPY . .

CMD ["python", "main.py"]
