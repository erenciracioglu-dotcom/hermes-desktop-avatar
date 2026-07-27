#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHON="$SCRIPT_DIR/.venv/bin/python"

if [ ! -x "$PYTHON" ]; then
    printf 'venv missing: %s\n' "$PYTHON" >&2
    printf 'Create it with: python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt\n' >&2
    exit 1
fi

cd "$SCRIPT_DIR"
export PYTHONPATH="$SCRIPT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONUNBUFFERED=1
exec "$PYTHON" -u -m avatar
