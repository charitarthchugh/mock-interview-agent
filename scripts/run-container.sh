#!/usr/bin/env bash
# Run the interview agent in podman with the GPU attached.
#
# --network=host because the app talks to vLLM on the host's port 8001 and is
# itself fronted by `tailscale serve` on the host. Bridged networking would
# mean threading both directions through host.containers.internal for no gain
# on a single-user box.
set -euo pipefail

IMAGE="${IMAGE:-interview-agent:latest}"
HF_CACHE="${HF_CACHE:-$HOME/.cache/huggingface}"
SESSIONS="${SESSIONS:-$PWD/sessions}"

mkdir -p "$HF_CACHE" "$SESSIONS"

exec podman run --rm -it \
    --name interview-agent \
    --device nvidia.com/gpu=all \
    --network=host \
    -v "$HF_CACHE:/models/huggingface:z" \
    -v "$SESSIONS:/app/sessions:z" \
    -e OMNI_HOST="${OMNI_HOST:-http://localhost:8001}" \
    -e TTS_DEVICE="${TTS_DEVICE:-cuda}" \
    "$IMAGE" "$@"
