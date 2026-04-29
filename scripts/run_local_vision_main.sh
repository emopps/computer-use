#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

MODEL_PATH="${MODEL_PATH:-/data/usershare/models/Qwen3-VL-2B-Instruct-GGUF/Qwen3VL-2B-Instruct-Q4_K_M.gguf}"
MMPROJ_PATH="${MMPROJ_PATH:-/data/usershare/models/Qwen3-VL-2B-Instruct-GGUF/mmproj-Qwen3VL-2B-Instruct-F16.gguf}"
LLAMA_SERVER_CMD="${LLAMA_SERVER_CMD:-llama-server}"
LLAMA_SERVER_HOST="${LLAMA_SERVER_HOST:-127.0.0.1}"
LLAMA_SERVER_PORT="${LLAMA_SERVER_PORT:-8080}"
LLAMA_SERVER_ALIAS="${LLAMA_SERVER_ALIAS:-qwen3-vl-2b-instruct}"
LLAMA_SERVER_CTX="${LLAMA_SERVER_CTX:-4096}"
LLAMA_SERVER_NGL="${LLAMA_SERVER_NGL:-0}"

cd "$ROOT_DIR"

python3 scripts/run_local_vision_main.py \
  --server-command "$LLAMA_SERVER_CMD" \
  --model "$MODEL_PATH" \
  --mmproj "$MMPROJ_PATH" \
  --alias "$LLAMA_SERVER_ALIAS" \
  --host "$LLAMA_SERVER_HOST" \
  --port "$LLAMA_SERVER_PORT" \
  --ctx-size "$LLAMA_SERVER_CTX" \
  --gpu-layers "$LLAMA_SERVER_NGL" \
  "$@"
