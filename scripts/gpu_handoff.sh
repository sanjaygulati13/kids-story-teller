#!/usr/bin/env bash
# GPU hand-off helper. On a single 16 GB GPU only one big model fits at a time,
# so the batch worker frees/warms models between phases via this script.
#
# Subcommands (idempotent):
#   unload-llm           POST {LLAMASWAP}/api/models/unload  (free all LLMs)
#   unload-tts           POST {CHATTERBOX}/api/unload        (drop the TTS model; server stays up)
#   wait-free [MIN_MB]   block (≤ TIMEOUT) until ≥ MIN_MB VRAM free on card0 (default 9000)
#   load <model-id>      fire a 1-token completion so llama-swap loads/swaps to it, then wait
#   status               print running models + free VRAM
#
# Env: LLAMASWAP (http://localhost:8080), CHATTERBOX (http://localhost:8890), TIMEOUT (180).
# rocm-smi key names verified on an RX 9070 XT: `--showmeminfo vram --json` ->
#   {"card0": {"VRAM Total Memory (B)": "...", "VRAM Total Used Memory (B)": "..."}}
set -uo pipefail

LLAMASWAP="${LLAMASWAP:-http://localhost:8080}"
CHATTERBOX="${CHATTERBOX:-http://localhost:8890}"
TIMEOUT="${TIMEOUT:-180}"

_free_mb() {
  # free MB on card0 = (Total - Used) / 1MiB; prints an integer or nothing on failure
  rocm-smi --showmeminfo vram --json 2>/dev/null | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
    c = d.get("card0", {})
    tot = int(c.get("VRAM Total Memory (B)", 0)); used = int(c.get("VRAM Total Used Memory (B)", 0))
    print(max(0, (tot - used)) // (1024*1024))
except Exception:
    pass
'
}

cmd_unload_llm() {
  curl -s -m 20 -X POST "$LLAMASWAP/api/models/unload" -o /dev/null -w "unload-llm: HTTP %{http_code}\n" \
    || curl -s -m 20 "$LLAMASWAP/unload" -o /dev/null -w "unload-llm(GET): HTTP %{http_code}\n"
}

cmd_unload_tts() {
  curl -s -m 30 -X POST "$CHATTERBOX/api/unload" -o /dev/null -w "unload-tts: HTTP %{http_code}\n"
}

cmd_wait_free() {
  local min="${1:-9000}" waited=0
  while [ "$waited" -lt "$TIMEOUT" ]; do
    local free; free="$(_free_mb)"
    if [ -n "$free" ] && [ "$free" -ge "$min" ]; then
      echo "wait-free: ${free} MB free (>= ${min})"; return 0
    fi
    sleep 3; waited=$((waited + 3))
  done
  echo "wait-free: TIMEOUT after ${TIMEOUT}s (free=$(_free_mb) MB, wanted ${min}); a swap may be needed" >&2
  return 1
}

cmd_load() {
  local model="${1:?load needs a model id}"
  curl -s -m 120 -X POST "$LLAMASWAP/v1/chat/completions" \
    -H 'content-type: application/json' \
    -d "{\"model\":\"$model\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"max_tokens\":1}" \
    -o /dev/null -w "load $model: warm HTTP %{http_code}\n"
  local waited=0
  while [ "$waited" -lt "$TIMEOUT" ]; do
    # case-insensitive: /running reports the model PATH (e.g. Qwen3.6-27B...gguf) not the id
    if curl -s -m 8 "$LLAMASWAP/running" | grep -qi "$model"; then
      echo "load $model: running"; return 0
    fi
    sleep 2; waited=$((waited + 2))
  done
  echo "load $model: not confirmed running after ${TIMEOUT}s" >&2; return 1
}

cmd_status() {
  echo "running: $(curl -s -m 8 "$LLAMASWAP/running" 2>/dev/null | head -c 400)"
  echo "free VRAM (card0): $(_free_mb) MB"
}

case "${1:-}" in
  unload-llm) cmd_unload_llm ;;
  unload-tts) cmd_unload_tts ;;
  wait-free)  shift; cmd_wait_free "$@" ;;
  load)       shift; cmd_load "$@" ;;
  status)     cmd_status ;;
  *) echo "usage: $0 {unload-llm|unload-tts|wait-free [MIN_MB]|load <model>|status}" >&2; exit 2 ;;
esac
