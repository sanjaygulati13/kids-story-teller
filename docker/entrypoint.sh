#!/bin/sh
# Container entrypoint. One image, three roles:
#   api        migrate the database, create the admin + first child (first run only), serve the app
#   worker     write, check and narrate stories
#   scheduler  nightly batch + watchdog (replaces the systemd timers)
# Anything else is run as a command, e.g.  docker compose run --rm api python scripts/import_stories.py ...
set -e

mkdir -p "${TB_DATA_DIR:-/data}"

case "${1:-api}" in
  api)
    python -m alembic upgrade head
    # idempotent; prints a generated admin password ONCE unless TB_ADMIN_PASSWORD is set
    python scripts/seed.py --child "${TB_FIRST_CHILD:-Sam}" --band "${TB_FIRST_CHILD_BAND:-early}"
    exec python -m uvicorn tellybear.main:app --host "${TB_API_HOST:-0.0.0.0}" --port "${TB_API_PORT:-8090}" \
      --proxy-headers --forwarded-allow-ips="${TB_FORWARDED_ALLOW_IPS:-127.0.0.1}"
    ;;
  worker)
    # wait for the api container to finish migrating
    until python -c "import sys; from tellybear.db.base import SessionLocal; from sqlalchemy import text; \
s=SessionLocal(); s.execute(text('SELECT 1 FROM alembic_version')); s.close()" 2>/dev/null; do
      echo "waiting for the database migration..."; sleep 3
    done
    exec python -m tellybear.worker.runner
    ;;
  scheduler)
    until python -c "from tellybear.db.base import SessionLocal; from sqlalchemy import text; \
s=SessionLocal(); s.execute(text('SELECT 1 FROM alembic_version')); s.close()" 2>/dev/null; do
      echo "waiting for the database migration..."; sleep 3
    done
    exec python -m tellybear.scheduler.loop
    ;;
  *)
    exec "$@"
    ;;
esac
