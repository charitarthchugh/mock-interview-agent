#!/usr/bin/env bash
# Build the virtualenv that scripts/diarise.py runs in.
#
# NeMo is kept out of the project environment on purpose: nemo_toolkit pins
# torch, and letting it resolve alongside the app would decide the app's torch
# version for it. Nothing in app/ imports any of this, so it lives off to one
# side and the app's lockfile stays untouched.
set -euo pipefail

DEST="${DEST:-.diarise-venv}"

uv venv "$DEST" --python 3.12
VIRTUAL_ENV="$PWD/$DEST" uv pip install "nemo_toolkit[asr]"

echo
echo "built $DEST"
echo "run:  $DEST/bin/python scripts/diarise.py corpus/audio corpus/transcripts"
