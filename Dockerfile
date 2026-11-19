FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg curl unzip ca-certificates \
    && rm -rf /var/lib/apt/lists/*
# Deno: the JavaScript runtime yt-dlp needs to read YouTube's player.
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh -s -- -y \
    && deno --version
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py .
# Fresh yt-dlp on every start: sites change often.
CMD ["sh", "-c", "pip install -U --no-cache-dir 'yt-dlp[default]' >/dev/null 2>&1; python bot.py"]
