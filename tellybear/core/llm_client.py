"""LLM client.

One client, two backends:
  - llama-swap (OpenAI-compatible /chat/completions) for local models.
  - Anthropic Messages API for the optional Opus model, when TB_ENABLE_OPUS=true and the
    requested model id equals TB_OPUS_MODEL.

Optionally (TB_LLM_QWEN_PREFIX) Qwen's default system line is prepended for model ids containing
"qwen"; some Qwen chat templates behave better with it.

Implemented with the sync httpx client: the worker, parser-retry and safety judge are all
synchronous; async FastAPI routes call these via asyncio.to_thread. The client is a class so
it can be injected/mocked in tests.
"""
from __future__ import annotations

import time

import httpx

from tellybear.core.config import Settings, get_settings
from tellybear.core.logging import get_logger

log = get_logger("llm")

QWEN_PREFIX = "You are Qwen, created by Alibaba Cloud. You are a helpful assistant."

# connect 10s, read 300s
_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0)


class LLMError(RuntimeError):
    pass


class _ClientError(LLMError):
    """Non-retryable (4xx / malformed response)."""


class LLMClient:
    def __init__(self, settings: Settings | None = None):
        self.s = settings or get_settings()

    # -- public API ---------------------------------------------------------
    def generate(self, messages: list[dict], model: str, json_mode: bool = True,
                 temperature: float | None = None) -> str:
        """messages: OpenAI-style [{role, content}]. Returns the assistant text content.
        temperature: per-call override (story generation runs hotter for variety; the judge low)."""
        temp = 0.7 if temperature is None else temperature
        if self.s.TB_ENABLE_OPUS and self.s.TB_OPUS_MODEL and model == self.s.TB_OPUS_MODEL:
            return self._anthropic(messages, model, json_mode, temp)
        return self._llamaswap(messages, model, json_mode, temp)

    # -- backends -----------------------------------------------------------
    def _apply_qwen_prefix(self, messages: list[dict], model: str) -> list[dict]:
        if not self.s.TB_LLM_QWEN_PREFIX or "qwen" not in model.lower():
            return messages
        out = [dict(m) for m in messages]
        # prepend the required prefix to the first system message, or insert one
        for m in out:
            if m.get("role") == "system":
                m["content"] = f"{QWEN_PREFIX}\n\n{m['content']}"
                return out
        return [{"role": "system", "content": QWEN_PREFIX}] + out

    def _llamaswap(self, messages: list[dict], model: str, json_mode: bool, temperature: float = 0.7) -> str:
        body: dict = {
            "model": model,
            "messages": self._apply_qwen_prefix(messages, model),
            "temperature": temperature,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        url = f"{self.s.TB_LLM_URL.rstrip('/')}/chat/completions"
        headers = {"Authorization": f"Bearer {self.s.TB_LLM_API_KEY}"} if self.s.TB_LLM_API_KEY else None
        data = self._post_with_retry(url, body, headers)
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"unexpected LLM response shape: {str(data)[:300]}") from e
        if not isinstance(content, str) or not content.strip():
            raise LLMError("LLM returned empty content (a reasoning model with thinking enabled?)")
        return content

    def _anthropic(self, messages: list[dict], model: str, json_mode: bool, temperature: float = 0.7) -> str:
        system_parts = [m["content"] for m in messages if m.get("role") == "system"]
        convo = [
            {"role": m["role"], "content": m["content"]}
            for m in messages
            if m.get("role") in ("user", "assistant")
        ]
        body = {
            "model": model,
            "max_tokens": 4096,
            "system": "\n\n".join(system_parts),
            "messages": convo,
            "temperature": temperature,
        }
        headers = {
            "x-api-key": self.s.TB_ANTHROPIC_API_KEY or "",
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        data = self._post_with_retry("https://api.anthropic.com/v1/messages", body, headers)
        try:
            return "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text")
        except (KeyError, TypeError) as e:
            raise LLMError(f"unexpected anthropic response shape: {data}") from e

    # -- transport with patient retry on 5xx/timeout ----------------
    # llama-swap returns 502 while it cold-loads/swaps a model on the shared GPU (can take
    # 30-90s after a TTS↔LLM hand-off), so retry several times with backoff, not 1x1s.
    _BACKOFF = (2.0, 4.0, 8.0, 16.0, 30.0)

    def _post_with_retry(self, url: str, body: dict, headers: dict | None = None) -> dict:
        last_exc: Exception | None = None
        attempts = len(self._BACKOFF) + 1
        for attempt in range(attempts):
            try:
                with httpx.Client(timeout=_TIMEOUT) as client:
                    r = client.post(url, json=body, headers=headers)
                    if r.status_code >= 500:
                        raise LLMError(f"{url} -> {r.status_code}: {r.text[:200]}")
                    if r.status_code >= 400:  # client error: retrying won't help — fail now, as LLMError
                        raise _ClientError(f"{url} -> {r.status_code}: {r.text[:300]}")
                    try:
                        return r.json()
                    except ValueError as e:
                        raise _ClientError(f"{url} returned non-JSON: {r.text[:200]}") from e
            except _ClientError:
                raise
            except (httpx.TimeoutException, httpx.TransportError, LLMError) as e:
                last_exc = e
                log.warning("LLM call failed (attempt %d/%d): %s", attempt + 1, attempts, e)
                if attempt < len(self._BACKOFF):
                    time.sleep(self._BACKOFF[attempt])
        raise LLMError(f"LLM call failed after {attempts} attempts: {last_exc}")


_default_client: LLMClient | None = None


def get_llm_client() -> LLMClient:
    global _default_client
    if _default_client is None:
        _default_client = LLMClient()
    return _default_client
