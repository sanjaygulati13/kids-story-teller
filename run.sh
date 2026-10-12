#!/usr/bin/env bash
# Tellybear — one-shot setup + verification.
# Usage:
#   ./run.sh            # setup + all checks (unit tests, judge eval, full live E2E)
#   ./run.sh --serve    # do the above, then start API + worker in the foreground
#   ./run.sh --quick    # setup + unit tests only (skip the slow live LLM/TTS checks)
#
# Uses `python3` (no python3.12 required). Run from the repo root.

set -euo pipefail
cd "$(dirname "$0")"

# ----- pretty output -----------------------------------------------------------
BOLD=$'\033[1m'; GRN=$'\033[32m'; RED=$'\033[31m'; YEL=$'\033[33m'; RST=$'\033[0m'
step(){ echo; echo "${BOLD}==> $*${RST}"; }
ok(){ echo "${GRN}✓ $*${RST}"; }
warn(){ echo "${YEL}! $*${RST}"; }
die(){ echo "${RED}✗ $*${RST}"; exit 1; }

SERVE=0; QUICK=0
for a in "$@"; do
  case "$a" in
    --serve) SERVE=1 ;;
    --quick) QUICK=1 ;;
    *) die "unknown option: $a (use --serve or --quick)" ;;
  esac
done

# ----- prerequisites -----------------------------------------------------------
step "Checking prerequisites"
command -v python3 >/dev/null || die "python3 not found"
PYV=$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')
echo "python3 = $PYV"
python3 -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3,12) else 1)' \
  || die "Python >= 3.12 required (found $PYV)"
command -v node >/dev/null && echo "node = $(node -v)" || warn "node not found — the parent PWA build will be skipped"
ok "prerequisites"

# ----- python env --------------------------------------------------------------
step "Python venv + dependencies"
[ -d .venv ] || python3 -m venv .venv
PY=.venv/bin/python
"$PY" -m pip install -q --upgrade pip
"$PY" -m pip install -q -e ".[dev]"
ok "dependencies installed"

# ----- .env --------------------------------------------------------------------
step "Configuration (.env)"
if [ ! -f .env ]; then
  cp .env.example .env
  warn ".env created from .env.example — set TB_LLM_URL and TB_ADMIN_EMAIL, then re-run"; exit 1
else
  ok ".env present"
fi
"$PY" -c "from tellybear.core.config import get_settings; s=get_settings(); print('  data_dir =', s.data_dir); print('  LLM gateway =', s.TB_LLM_URL); print('  kokoro =', s.TB_KOKORO_URL)" \
  || die "config failed to load (a required var is missing — see message above)"
ok "config valid"

# ----- database ----------------------------------------------------------------
step "Database: migrate + seed"
"$PY" -m alembic upgrade head
"$PY" scripts/seed.py
ok "schema migrated + seeded"

# ----- parent PWA --------------------------------------------------------------
if command -v node >/dev/null; then
  step "Parent PWA build (web_parent)"
  ( cd web_parent
    if [ -f package-lock.json ]; then npm ci; else npm install; fi
    npm run build )
  ok "PWA built -> web_parent/dist"
else
  warn "skipping PWA build (node not installed)"
fi

# ----- unit tests --------------------------------------------------------------
step "Unit tests"
"$PY" -m pytest tests/test_parser.py tests/test_banned.py tests/test_pron.py tests/test_played.py -q
ok "unit tests passed"

if [ "$QUICK" = "1" ]; then
  echo; ok "${BOLD}Quick checks done.${RST} Re-run without --quick for the live judge eval + E2E."
  exit 0
fi

# ----- health (live deps) ------------------------------------------------------
step "Health check (LLM gateway + Kokoro)"
if ! "$PY" - <<'PYEOF'
import sys, httpx
from tellybear.core.config import get_settings
s = get_settings()
hdr = {"Authorization": f"Bearer {s.TB_LLM_API_KEY}"} if s.TB_LLM_API_KEY else None
checks = [("LLM gateway", f"{s.TB_LLM_URL.rstrip('/')}/models", hdr)]
if s.TB_KOKORO_URL:
    checks.append(("kokoro", f"{s.TB_KOKORO_URL.rstrip('/')}/health", None))
bad = []
for name, url, h in checks:
    try:
        httpx.get(url, timeout=5, headers=h).raise_for_status(); print(f"  {name}: ok")
    except Exception as e:
        print(f"  {name}: UNREACHABLE ({e})"); bad.append(name)
sys.exit(1 if bad else 0)
PYEOF
then
  warn "a live dependency is unreachable — skipping judge eval + E2E."
  warn "fix TB_LLM_URL / TB_KOKORO_URL in .env (or start those services) and re-run."
  exit 0
fi
ok "all dependencies reachable"

# ----- judge red-team eval (live) ---------------------------------------------
step "Safety judge red-team eval (live) — must let NO unsafe story pass"
"$PY" scripts/run_judge_eval.py
ok "judge eval passed (no unsafe story slipped through)"

# ----- full E2E (live) ---------------------------------------------------------
step "Full E2E (live: generate → approve → wav+mp3 → played)"
"$PY" tests/e2e_live.py
ok "E2E passed"

echo; echo "${GRN}${BOLD}ALL CHECKS PASSED.${RST}"

# ----- optionally serve --------------------------------------------------------
if [ "$SERVE" = "1" ]; then
  step "Starting worker (background) + API (foreground)"
  "$PY" -m tellybear.worker.runner &
  WPID=$!
  trap 'kill $WPID 2>/dev/null' EXIT
  HOST=$("$PY" -c "from tellybear.core.config import get_settings as g; print(g().TB_API_HOST)")
  PORT=$("$PY" -c "from tellybear.core.config import get_settings as g; print(g().TB_API_PORT)")
  echo "  Parent app : http://localhost:${PORT}/parent/"
  echo "  Child player: open it from the Kids tab (Copy link)"
  echo "  Health     : http://localhost:${PORT}/healthz"
  echo "  (Ctrl-C to stop both)"
  exec "$PY" -m uvicorn tellybear.main:app --host "$HOST" --port "$PORT"
else
  echo
  echo "To run the app:"
  echo "  ${BOLD}./run.sh --serve${RST}      (starts API + worker)"
  echo "or manually:"
  echo "  .venv/bin/uvicorn tellybear.main:app --host 0.0.0.0 --port 8090"
  echo "  .venv/bin/python -m tellybear.worker.runner   # second terminal"
fi
