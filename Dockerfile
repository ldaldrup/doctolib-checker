FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATABASE_PATH=/data/checker.sqlite3

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --system --uid 10001 --user-group --create-home checker \
    && mkdir -p /data \
    && chown checker:checker /data

COPY app ./app
USER 10001:10001
EXPOSE 8000
CMD ["python", "-m", "app.api.main"]
