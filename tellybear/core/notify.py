"""ntfy notifier.

One-way status pushes to an ntfy server (ntfy.sh or self-hosted). Leave TB_NTFY_URL or
TB_NTFY_TOPIC empty to disable notifications. Two message types in v1:
  - "ready to review" (with an approve deep-link action button)
  - "ready to play"
Plus generation-delayed alerts on dependency failure. Best-effort: a notify failure must
never break the pipeline, so errors are logged and swallowed.
"""
from __future__ import annotations

import httpx

from tellybear.core.config import Settings, get_settings
from tellybear.core.logging import get_logger

log = get_logger("ntfy")


def notify(title: str, message: str, *, click: str | None = None,
           actions: list[dict] | None = None, priority: str | None = None,
           settings: Settings | None = None) -> bool:
    """Publish a notification to TB_NTFY_TOPIC. Returns True on success (False if disabled).

    Uses ntfy's JSON publishing (POST to the server root) rather than headers, so titles with
    non-ASCII child names ("Zoë", Devanagari, ...) work — HTTP headers are ASCII-only."""
    s = settings or get_settings()
    if not (s.TB_NTFY_URL and s.TB_NTFY_TOPIC):
        return False
    payload: dict = {"topic": s.TB_NTFY_TOPIC, "title": title, "message": message}
    if click:
        payload["click"] = click
    if priority:
        payload["priority"] = {"min": 1, "low": 2, "default": 3, "high": 4, "max": 5, "urgent": 5}.get(priority, 3)
    if actions:
        payload["actions"] = [{"action": a.get("action", "view"), "label": a["label"], "url": a["url"]}
                              for a in actions]
    try:
        with httpx.Client(timeout=10.0) as c:
            r = c.post(s.TB_NTFY_URL.rstrip("/"), json=payload)
            r.raise_for_status()
        return True
    except Exception as e:  # best-effort; never break the pipeline on a notify failure
        log.warning("ntfy notify failed (%s): %s", title, e)
        return False


def notify_ready_to_review(child_name: str, story_id: str, target_date: str | None,
                           settings: Settings | None = None) -> bool:
    s = settings or get_settings()
    base = (s.TB_PARENT_APP_URL or "").rstrip("/")
    deep = f"{base}?review={story_id}" if base else None
    when = f" ({target_date})" if target_date else ""
    actions = [{"action": "view", "label": "Review", "url": deep}] if deep else None
    return notify(
        f"Tomorrow's story for {child_name} is ready to review",
        f"A new story for {child_name}{when} needs your approval.",
        click=deep, actions=actions, settings=s,
    )


def notify_ready_to_play(child_name: str, title: str, slug: str | None = None,
                         settings: Settings | None = None) -> bool:
    s = settings or get_settings()
    # tap-through to the child's player. TB_PARENT_APP_URL ends in /parent; the player lives at the
    # ORIGIN's /play/{slug}, so strip the /parent suffix to get the origin.
    base = (s.TB_PARENT_APP_URL or "").rstrip("/")
    origin = base[:-len("/parent")] if base.endswith("/parent") else base
    deep = f"{origin}/play/{slug}" if (origin and slug) else None
    actions = [{"action": "view", "label": "Play", "url": deep}] if deep else None
    return notify(f"{child_name}'s story is ready to play", f"“{title}” is now in the catalog.",
                  click=deep, actions=actions, settings=s)


def notify_delayed(child_name: str, reason: str, settings: Settings | None = None) -> bool:
    return notify(f"Story for {child_name} delayed", reason, priority="high", settings=settings)
