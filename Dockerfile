FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    sed -i "s/'code': code_response\['device_code'\]/'device_code': code_response['device_code']/" \
    /usr/local/lib/python3.12/site-packages/yt_dlp_plugins/extractor/youtubeoauth.py

COPY bot.py .

CMD ["python", "-u", "bot.py"]
