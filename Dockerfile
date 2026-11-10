FROM python:3.11-slim-bookworm

# Chromium + Xvfb(xvfb-run, xauth): Linux에서 성남 모니터가 Chrome을 가상 화면에서 실행하는 데 필요
RUN apt-get update \
    && apt-get install -y --no-install-recommends chromium xvfb xauth fonts-nanum \
    && rm -rf /var/lib/apt/lists/*

ENV CHROME_BIN=/usr/bin/chromium \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# Render는 PORT 환경변수를 주입한다.
CMD ["sh", "-c", "python tennis_court_monitor_all.py --port ${PORT:-8000}"]
