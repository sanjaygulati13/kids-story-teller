# Tellybear — one image for the API, the worker and the scheduler (see docker-compose.yml).
#
#   docker build -t tellybear .
#   docker run --rm -p 8090:8090 -v tellybear-data:/data \
#     -e TB_LLM_URL=http://your-gateway:8080/v1 -e TB_ADMIN_EMAIL=you@example.com tellybear
#
# Everything the app writes (database, audio, voices, model caches) lives in /data.

# ---- 1. build the parent app (static files) ---------------------------------------------------
FROM node:20.18-slim AS web
WORKDIR /web
COPY web_parent/package.json web_parent/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web_parent/ ./
RUN npx vite build

# ---- 2. runtime -------------------------------------------------------------------------------
FROM python:3.12-slim

# ffmpeg: audio transcoding · espeak-ng: optional IPA for name pronunciations · tzdata: local
# time for the nightly batch (set TZ)
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg espeak-ng tzdata curl \
 && rm -rf /var/lib/apt/lists/*

ARG WITH_EMBED=1
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TB_DATA_DIR=/data \
    TB_API_HOST=0.0.0.0 \
    TB_API_PORT=8090 \
    TB_GPU_HANDOFF=false \
    HF_HOME=/data/cache/huggingface \
    FASTEMBED_CACHE_PATH=/data/cache/fastembed

WORKDIR /app
COPY pyproject.toml README.md ./
COPY tellybear/ tellybear/
RUN pip install --root-user-action=ignore -e . \
 && if [ "$WITH_EMBED" = "1" ]; then pip install --root-user-action=ignore -e ".[embed]"; fi

COPY alembic.ini ./
COPY migrations/ migrations/
COPY prompts/ prompts/
COPY scripts/ scripts/
COPY examples/ examples/
COPY web_child/ web_child/
COPY --from=web /web/dist web_parent/dist
COPY docker/entrypoint.sh /usr/local/bin/tellybear-entrypoint

# readable by the runtime user whatever the permissions were in the build context
RUN chmod -R a+rX /app \
 && useradd --create-home --uid 1000 tellybear \
 && mkdir -p /data && chown tellybear:tellybear /data \
 && chmod +x /usr/local/bin/tellybear-entrypoint
USER tellybear
VOLUME /data
EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD curl -fsS -o /dev/null "http://127.0.0.1:${TB_API_PORT}/parent/" || exit 1

ENTRYPOINT ["tellybear-entrypoint"]
CMD ["api"]
