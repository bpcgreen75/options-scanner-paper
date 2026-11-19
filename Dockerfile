FROM python:3.14-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/*
COPY paper_receiver.py paper_preview.py ./
RUN mkdir /app/storage
CMD ["python", "-B", "paper_receiver.py", "--database", "/app/storage/signals.sqlite3", "serve", "--hosted"]
