"""FastAPI app assembly.

Mounts the parent and child routers, the child static player, the parent PWA build (if
present), serves generated audio, and exposes GET /healthz (DB + llama-swap + Kokoro).
"""
from __future__ import annotations

from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from tellybear.api_auth.routes import router as auth_router
from tellybear.api_child.routes import router as child_router
from tellybear.api_parent.routes import router as parent_router
from tellybear.core.config import get_settings
from tellybear.core.logging import get_logger
from tellybear.db.base import SessionLocal
from sqlalchemy import text

log = get_logger("main")
settings = get_settings()
settings.ensure_dirs()

# interactive API docs are off unless TB_API_DOCS=true (don't advertise every endpoint publicly)
_docs = settings.TB_API_DOCS
app = FastAPI(title="Tellybear", version="1.0.0",
              docs_url="/docs" if _docs else None, redoc_url=None,
              openapi_url="/openapi.json" if _docs else None)

app.include_router(auth_router, prefix="/api/auth", tags=["auth"])
app.include_router(parent_router, prefix="/api/parent", tags=["parent"])
app.include_router(child_router, prefix="/api/child", tags=["child"])

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _check_db() -> tuple[bool, str]:
    try:
        db = SessionLocal()
        try:
            db.execute(text("SELECT 1"))
            return True, "ok"
        finally:
            db.close()
    except Exception as e:
        return False, str(e)


def _llm_headers() -> dict | None:
    key = settings.TB_LLM_API_KEY
    return {"Authorization": f"Bearer {key}"} if key else None


def _check_http(url: str, headers: dict | None = None) -> tuple[bool, str]:
    try:
        with httpx.Client(timeout=5.0) as c:
            r = c.get(url, headers=headers)
            r.raise_for_status()
        return True, "ok"
    except Exception as e:
        return False, str(e)


@app.get("/")
def root():
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/parent/")


@app.get("/healthz")
def healthz() -> JSONResponse:
    db_ok, db_msg = _check_db()
    llm_ok, llm_msg = _check_http(f"{settings.TB_LLM_URL.rstrip('/')}/models", _llm_headers())
    from tellybear.core.settings_store import tts_url
    kokoro = tts_url("kokoro", settings)
    kokoro_ok, kokoro_msg = _check_http(f"{kokoro}/health") if kokoro else (False, "not configured")
    # public endpoint: booleans only — details (internal URLs, error text) go to the server log
    for name, ok, msg in (("db", db_ok, db_msg), ("llm", llm_ok, llm_msg), ("kokoro", kokoro_ok, kokoro_msg)):
        if not ok:
            log.warning("healthz: %s unhealthy: %s", name, msg)
    deps = {"db": {"ok": db_ok}, "llm": {"ok": llm_ok}, "kokoro": {"ok": kokoro_ok}}
    # Kokoro is the fallback TTS: its absence degrades, but doesn't fail, overall health
    overall = db_ok and llm_ok
    return JSONResponse(
        status_code=200 if overall else 503,
        content={"status": "ok" if overall else "degraded", "dependencies": deps},
    )


# --- static surfaces (mounted last so /api and /healthz win) ----------------
def _mount_static() -> None:
    audio_dir = settings.audio_dir
    audio_dir.mkdir(parents=True, exist_ok=True)

    # Child player: one static page served for /play AND /play/{slug}. StaticFiles
    # serves /play/ (index) but 404s on /play/{slug}, so add explicit routes returning the file.
    child_index = _REPO_ROOT / "web_child" / "index.html"
    if child_index.is_file():
        from fastapi.responses import FileResponse

        # offline resilience: a small service worker scoped to /play/ caches the shell +
        # last catalog so a Wi-Fi blip mid-bedtime doesn't blank the page. MUST be registered
        # BEFORE /play/{slug} or that path-param route swallows /play/sw.js (slug="sw.js").
        child_sw = _REPO_ROOT / "web_child" / "sw.js"
        if child_sw.is_file():
            @app.get("/play/sw.js")
            def _child_sw():
                return FileResponse(str(child_sw), media_type="application/javascript")

        @app.get("/play")
        @app.get("/play/")
        @app.get("/play/{slug}")
        def _child_player(slug: str | None = None):  # slug resolved client-side via the API
            return FileResponse(str(child_index))

    parent_dist = _REPO_ROOT / "web_parent" / "dist"
    if parent_dist.is_dir():
        app.mount("/parent", StaticFiles(directory=str(parent_dist), html=True), name="parent")


_mount_static()
