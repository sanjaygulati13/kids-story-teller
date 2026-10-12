"""Thin wrapper around scripts/gpu_handoff.sh.

The worker shells out to the script for the VRAM dance rather than reimplementing it. All calls
are best-effort: a missing script / rocm-smi (e.g. on a dev box) logs a warning and returns
False so the caller can continue (synthesis still works when only one model needs VRAM).

The hand-off assumes llama-swap + an AMD GPU (rocm-smi) + Chatterbox run as a systemd --user
service. Set TB_GPU_HANDOFF=false on any other setup (NVIDIA, CPU-only, Docker, a remote
OpenAI-compatible endpoint): every public function here then becomes a no-op that reports success.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from tellybear.core.config import Settings, get_settings
from tellybear.core import settings_store
from tellybear.core.logging import get_logger

log = get_logger("gpu")

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "gpu_handoff.sh"


def _env(s: Settings) -> dict:
    import os
    base_llm = s.TB_LLM_URL.rstrip("/")
    if base_llm.endswith("/v1"):
        base_llm = base_llm[: -len("/v1")]
    return {**os.environ, "LLAMASWAP": base_llm, "CHATTERBOX": settings_store.tts_url("chatterbox", s)}


def _run(args: list[str], s: Settings, timeout: float = 240.0, env_overrides: dict | None = None) -> bool:
    if not _SCRIPT.is_file():
        log.warning("gpu_handoff.sh not found at %s; skipping %s", _SCRIPT, args)
        return False
    try:
        env = _env(s)
        if env_overrides:
            env.update(env_overrides)
        r = subprocess.run(["bash", str(_SCRIPT), *args], env=env,
                           capture_output=True, text=True, timeout=timeout)
        out = (r.stdout or "").strip()
        if out:
            log.info("gpu %s: %s", " ".join(args), out)
        if r.returncode != 0 and r.stderr:
            log.warning("gpu %s stderr: %s", " ".join(args), r.stderr.strip())
        return r.returncode == 0
    except Exception as e:
        log.warning("gpu hand-off %s failed: %s", args, e)
        return False


def _managed(s: Settings) -> bool:
    return bool(s.TB_GPU_HANDOFF)


def unload_llm(s: Settings | None = None) -> bool:
    s = s or get_settings()
    return _run(["unload-llm"], s) if _managed(s) else True


def unload_tts(s: Settings | None = None) -> bool:
    s = s or get_settings()
    return _run(["unload-tts"], s) if _managed(s) else True


def load(model: str, s: Settings | None = None) -> bool:
    s = s or get_settings()
    return _run(["load", model], s) if _managed(s) else True


def wait_free(min_mb: int = 9000, s: Settings | None = None, poll_timeout: int | None = None) -> bool:
    """Poll rocm-smi (via gpu_handoff.sh wait-free) until >= min_mb VRAM is free.

    `poll_timeout` overrides the script's internal poll window (its own TIMEOUT env, default
    180s) for a specific call — used by reliability call-sites that want a short poll before
    deciding to escalate (SIGKILL) rather than waiting the full default window."""
    s = s or get_settings()
    if not _managed(s):
        return True
    overrides = {"TIMEOUT": str(poll_timeout)} if poll_timeout else None
    proc_timeout = (poll_timeout + 20) if poll_timeout else 240.0
    return _run(["wait-free", str(min_mb)], s, timeout=proc_timeout, env_overrides=overrides)


def _chatterbox_url(s: Settings) -> str:
    return settings_store.tts_url("chatterbox", s)


def _model_info(s: Settings) -> dict | None:
    try:
        import httpx
        with httpx.Client(timeout=5.0) as c:
            r = c.get(f"{_chatterbox_url(s)}/api/model-info")
            return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def _chatterbox_loaded(s: Settings) -> bool:
    info = _model_info(s)
    return bool(info and info.get("loaded"))


def _repo_to_type(repo_id: str | None) -> str | None:
    """Map a config repo_id to the model 'type' the server reports in /api/model-info."""
    rid = (repo_id or "").lower()
    if not rid:
        return None
    if "multilingual" in rid:
        return "multilingual"
    if "turbo" in rid:
        return "turbo"
    return "original"


def _loaded_as(s: Settings, want_type: str | None) -> bool:
    """Loaded AND (if a type is requested) of the requested type (turbo|multilingual|original)."""
    info = _model_info(s)
    if not (info and info.get("loaded")):
        return False
    return want_type is None or info.get("type") == want_type


def set_tts_model(repo_id: str, s: Settings | None = None) -> bool:
    """Persist the desired Chatterbox model into the server's config.yaml (takes effect on the next
    /restart_server, which reload_model() honors). Best-effort."""
    s = s or get_settings()
    if not _managed(s):
        return True
    try:
        import httpx
        with httpx.Client(timeout=15.0) as c:
            r = c.post(f"{_chatterbox_url(s)}/save_settings", json={"model": {"repo_id": repo_id}})
        if r.status_code == 200:
            log.info("chatterbox model set to %s (pending restart)", repo_id)
            return True
        log.warning("save_settings(model=%s) -> HTTP %s", repo_id, r.status_code)
    except Exception as e:
        log.warning("save_settings(model=%s) failed: %s", repo_id, e)
    return False


def _chatterbox_up(s: Settings) -> bool:
    return _model_info(s) is not None


def _systemctl(action: str, s: Settings) -> bool:
    """`systemctl --user <action> <chatterbox service>`. The worker runs under the user systemd
    manager, so this controls the sibling Chatterbox service. Best-effort."""
    import os
    svc = s.TB_CHATTERBOX_SERVICE
    env = {**os.environ}
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    try:
        r = subprocess.run(["systemctl", "--user", action, svc], env=env,
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            log.warning("systemctl --user %s %s failed: %s", action, svc, (r.stderr or "").strip())
        return r.returncode == 0
    except Exception as e:
        log.warning("systemctl --user %s %s error: %s", action, svc, e)
        return False


def _kill_tts_process(s: Settings) -> bool:
    """Escalation of last resort: SIGKILL the Chatterbox unit directly. Used when a graceful
    `systemctl stop` reports success but VRAM doesn't actually clear within the poll window (the
    process can wedge mid-shutdown, e.g. stuck in a CUDA call) — this is the concrete failure mode
    behind the historical "Chatterbox won't free VRAM -> generation stalls" incidents."""
    import os
    svc = s.TB_CHATTERBOX_SERVICE
    env = {**os.environ}
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    try:
        r = subprocess.run(["systemctl", "--user", "kill", "-s", "SIGKILL", svc], env=env,
                           capture_output=True, text=True, timeout=30)
        log.warning("SIGKILL escalation for %s: rc=%s %s", svc, r.returncode,
                    (r.stderr or "").strip())
        return r.returncode == 0
    except Exception as e:
        log.warning("SIGKILL escalation for %s failed: %s", svc, e)
        return False


def stop_tts_service(s: Settings | None = None) -> bool:
    """Fully stop Chatterbox (frees ALL its VRAM incl. the ~1 GB Torch CUDA context that
    /api/unload leaves behind) so an LLM can load. Falls back to /api/unload.

    RELIABILITY: a graceful `systemctl stop` occasionally reports success without the
    VRAM actually clearing (the process wedges mid-shutdown) — poll for up to 30s and, if it hasn't
    cleared, escalate to SIGKILL and poll again, so a caller that immediately loads an LLM after
    this returns never races a still-resident Chatterbox context (root cause of the OOM/502 +
    stalled-queue failure mode this exists to close out)."""
    s = s or get_settings()
    if not _managed(s):
        return True
    if not _chatterbox_up(s):
        return True  # already down
    if _systemctl("stop", s):
        if wait_free(9000, s, poll_timeout=30):
            return True
        log.warning("VRAM not free 30s after stopping %s; escalating to SIGKILL",
                    s.TB_CHATTERBOX_SERVICE)
        _kill_tts_process(s)
        if not wait_free(9000, s, poll_timeout=30):
            log.warning("VRAM STILL not free after SIGKILL escalation for %s; proceeding anyway "
                        "(a subsequent LLM load may OOM/502)", s.TB_CHATTERBOX_SERVICE)
        return True
    log.warning("could not stop %s; falling back to /api/unload (leaves ~1GB resident)",
                s.TB_CHATTERBOX_SERVICE)
    unload_tts(s)
    wait_free(9000, s, poll_timeout=30)
    return False


def start_tts_service(s: Settings | None = None, timeout: float = 60.0) -> bool:
    """Start Chatterbox and wait until its HTTP is reachable (model loaded separately)."""
    import time as _t
    s = s or get_settings()
    if not _managed(s):
        return True
    if not _chatterbox_up(s):
        _systemctl("start", s)
    waited = 0.0
    while waited < timeout:
        if _chatterbox_up(s):
            return True
        _t.sleep(2.0)
        waited += 2.0
    log.warning("chatterbox service not reachable after %ss", timeout)
    return False


def ensure_tts_loaded(s: Settings | None = None, attempts: int = 3, per_attempt: float = 75.0,
                      repo_id: str | None = None) -> bool:
    """Load the Chatterbox model (only /restart_server triggers a load — no load API, no auto-load
    on service start). A full-card LLM leaves its VRAM release lagging, so the first Chatterbox
    load can OOM; before EACH attempt we re-unload the LLM, wait for free VRAM, settle, then
    restart-and-poll. Retries until the model is resident.

    CO-RESIDENCY: when TB_GPU_CORESIDENT=1 the pre-attempt LLM unload is
    SKIPPED — that unload was defeating co-residency on every cold Chatterbox load / model-type
    swap (the LLM got dropped here even though prepare_for_tts itself was gated). The active gen/
    judge model is sized to fit alongside Chatterbox, so it can stay resident through the restart.

    If `repo_id` is given, the resident model must also be of that type — otherwise its repo_id is
    written to the server config and a restart swaps it in. This lets a clone story load the
    multilingual model and a predefined-voice story load turbo, on demand."""
    import time as _t

    import httpx
    s = s or get_settings()
    if not _managed(s):
        return True
    want_type = _repo_to_type(repo_id)
    if _loaded_as(s, want_type):
        return True
    if repo_id:
        set_tts_model(repo_id, s)  # write config so the upcoming restart loads the right model
    for attempt in range(1, attempts + 1):
        if not s.TB_GPU_CORESIDENT:
            # make sure no LLM is hogging VRAM (release can lag the unload by seconds)
            unload_llm(s)
            wait_free(6000, s)
        _t.sleep(3.0)
        try:
            with httpx.Client(timeout=30.0) as c:
                c.post(f"{_chatterbox_url(s)}/restart_server")
        except Exception as e:
            log.warning("restart_server attempt %d failed: %s", attempt, e)
        waited = 0.0
        while waited < per_attempt:
            if _loaded_as(s, want_type):
                log.info("chatterbox model loaded%s (attempt %d)",
                         f" [{want_type}]" if want_type else "", attempt)
                return True
            _t.sleep(3.0)
            waited += 3.0
        log.warning("chatterbox not loaded after attempt %d/%d; retrying", attempt, attempts)
    log.warning("chatterbox model failed to load after %d attempts", attempts)
    return False


def prepare_for_llm(s: Settings | None = None) -> None:
    """Free the GPU for an LLM load: STOP Chatterbox entirely (a model needing the whole card;
    /api/unload alone leaves ~1 GB resident and a full-card model then OOMs).

    CO-RESIDENCY: when TB_GPU_CORESIDENT=1 the active gen/judge model is
    sized to fit alongside Chatterbox (verified VRAM headroom — see config.py), so this is a no-op
    and Chatterbox stays resident. Leave OFF (default) for a model too big to co-reside."""
    s = s or get_settings()
    if not _managed(s):
        return
    if s.TB_GPU_CORESIDENT:
        log.info("TB_GPU_CORESIDENT=1: skipping Chatterbox stop (co-residency mode)")
        return
    stop_tts_service(s)


def prepare_for_tts(s: Settings | None = None, repo_id: str | None = None) -> None:
    """Hand the GPU from the LLM to TTS: free the LLM, (re)start Chatterbox, load model.
    `repo_id` selects which Chatterbox model must be resident (turbo vs multilingual); a mismatch
    triggers a config write + restart so the right model is swapped in for this story.

    CO-RESIDENCY: when TB_GPU_CORESIDENT=1 the LLM is NOT unloaded first (it
    co-resides with Chatterbox) — only the Chatterbox start/load steps run, still needed so a cold
    box (Chatterbox never started) gets it resident."""
    s = s or get_settings()
    if not _managed(s):
        return
    if not s.TB_GPU_CORESIDENT:
        unload_llm(s)
        wait_free(5000, s)
    start_tts_service(s)
    ensure_tts_loaded(s, repo_id=repo_id)
