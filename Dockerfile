FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AUCTION_DB=/app/data/auction.sqlite3 \
    AUCTION_UPLOADS=/app/data/photos

WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir --only-binary=:all: -r requirements.txt \
    && groupadd --gid 10001 auction \
    && useradd --uid 10001 --gid auction --no-create-home --home-dir /app auction \
    && install -d -m 0700 -o auction -g auction /app/data

COPY auction/ ./auction/
COPY static/ ./static/
COPY run.py ./

USER 10001:10001
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/', timeout=3).close()"]

CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "4", "--worker-tmp-dir", "/tmp", "--access-logfile", "-", "--error-logfile", "-", "run:app"]
