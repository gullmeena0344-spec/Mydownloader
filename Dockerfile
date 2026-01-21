# ---- Base image ----
FROM python:3.11-slim

# ---- Environment ----
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# ---- Install system deps ----
RUN apt-get update && apt-get install -y \
    ffmpeg \
    gcc \
    curl \
    aria2 \
    && rm -rf /var/lib/apt/lists/*

# ---- Set workdir ----
WORKDIR /app

# ---- Copy requirements ----
COPY requirements.txt .

# ---- Install Python deps ----
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# ---- Copy source code ----
COPY . .

# ---- Create output dir ----
RUN mkdir -p output

# ---- Start userbot ----
CMD ["python", "main.py"]
