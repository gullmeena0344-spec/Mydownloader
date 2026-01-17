FROM python:3.10

WORKDIR /app

# Required build deps for tgcrypto
RUN apt-get update && apt-get install -y \
    ffmpeg \
    aria2 \
    gcc \
    g++ \
    make \
    python3-dev \
    libffi-dev \
    libssl-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "main.py"]
