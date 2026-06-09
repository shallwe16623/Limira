FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PORT=8080 \
    LIMIRA_CORS_ALLOW_ORIGINS="http://127.0.0.1:5173,http://localhost:5173"

WORKDIR /app/backend

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl \
        jq \
        gcc \
        build-essential \
        libmariadb-dev \
        netcat-openbsd \
        pandoc \
        zstd && \
    rm -rf /var/lib/apt/lists/*

COPY ./backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir uv && \
    uv pip install --system -r requirements.txt --no-cache-dir

COPY ./backend .

EXPOSE 8080

HEALTHCHECK CMD curl --silent --fail http://localhost:${PORT:-8080}/health | jq -ne 'input.status == true' || exit 1

CMD ["python", "-m", "uvicorn", "limira_native:app", "--host", "0.0.0.0", "--port", "8080"]
