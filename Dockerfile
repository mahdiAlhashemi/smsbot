FROM python:3.12-slim

WORKDIR /app

# Install deps first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Database lives in /app/data so it can be a mounted volume (persists restarts).
ENV DB_URL=sqlite+aiosqlite:///data/smsbot.db
RUN mkdir -p /app/data

CMD ["python", "bot.py"]
