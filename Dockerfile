# Dockerfile for the AI Tracker backend.
FROM swr.cn-north-4.myhuaweicloud.com/ddn-k8s/docker.io/python:3.11-slim

WORKDIR /app

COPY backend/requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock

COPY backend/ ./backend/
COPY frontend/ ./frontend/

RUN mkdir -p data

ENV DB_PATH=data/tracker.db
ENV HOST=0.0.0.0
ENV PORT=8000
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
