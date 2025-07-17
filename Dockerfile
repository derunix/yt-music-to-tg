FROM python:3.11-slim

RUN apt update && apt install -y ffmpeg && \
    pip install aiogram dotenv redis yt-dlp && \
    mkdir /bot

WORKDIR /bot
COPY bot.py .
COPY cookies.txt .

CMD ["python", "bot.py"]