FROM python:3.11-slim

# ---------- System deps ----------
RUN apt-get update && apt-get install -y \
    ffmpeg \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ---------- Working dir ----------
WORKDIR /app

# ---------- Python deps ----------
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ---------- Bot source ----------
COPY . .

# ---------- Runtime ----------
ENV PYTHONUNBUFFERED=1

CMD ["python", "main.py"]
