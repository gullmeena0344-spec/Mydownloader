# ---- Base image ----
FROM python:3.11-slim

# ---- Environment ----
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# ---- Install system deps ----
# We install ffmpeg (for thumbs) and aria2 (for downloading)
RUN apt-get update && apt-get install -y \
    ffmpeg \
    gcc \
    curl \
    aria2 \
    && rm -rf /var/lib/apt/lists/*

# ---- Set workdir ----
WORKDIR /app

# ---- Security: Create a non-root user ----
# Cloud platforms often fail if you run as root. 
# We create a user named 'userbot' with ID 1000.
RUN useradd -m -u 1000 userbot

# ---- Copy requirements ----
COPY requirements.txt .

# ---- Install Python deps ----
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# ---- Copy source code ----
COPY . .

# ---- Permissions Fix ----
# Give the 'userbot' user permission to write to this folder
# This ensures it can create 'output/' and '*.session' files
RUN chown -R userbot:userbot /app

# ---- Switch to non-root user ----
USER userbot

# ---- Start userbot ----
CMD ["python", "main.py"]
