# syntax=docker/dockerfile:1.7
#
# Gootier production image.
#
# Pinned to linux/amd64 to match the ECS Fargate target. Apple Silicon
# Docker defaults to arm64, which would produce a non-runnable image on
# AWS — never drop the --platform flag at FROM or in the build command.

ARG PYTHON_VERSION=3.11

FROM --platform=linux/amd64 python:${PYTHON_VERSION}-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
COPY requirements.txt .
RUN pip wheel --wheel-dir /wheels -r requirements.txt


FROM --platform=linux/amd64 python:${PYTHON_VERSION}-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8000

# ffmpeg powers the /studio video-compose pipeline (concat clips, mix TTS
# and music beds, drawtext for burned-in captions). Pulls in ~150MB; the
# static binary is smaller but the apt build is the well-trodden path.
# fonts-dejavu-core gives us the DejaVuSans-Bold.ttf that drawtext loads
# at /usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf (see
# services/video_composer.DEFAULT_FONT_PATH).  Debian's ffmpeg ships with
# libfreetype so the drawtext filter is available out of the box.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /wheels /wheels
COPY requirements.txt .
RUN pip install --no-cache-dir --find-links=/wheels -r requirements.txt \
 && rm -rf /wheels

COPY . .

# Non-root runtime user
RUN useradd --create-home --shell /bin/bash gootier \
 && chown -R gootier:gootier /app
USER gootier

EXPOSE 8000

# Healthcheck hits the dynamic $PORT (Railway injects it; default 8000 locally)
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request,sys; \
                 sys.exit(0) if urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/health', timeout=3).status == 200 else sys.exit(1)" \
  || exit 1

# Shell-form CMD so ${PORT} expands at container start. Railway sets PORT
# automatically; locally `docker run` without -e PORT falls back to 8000.
CMD ["sh", "-c", "gunicorn -k uvicorn.workers.UvicornWorker -w 2 -t 120 -b 0.0.0.0:${PORT:-8000} main:app"]
