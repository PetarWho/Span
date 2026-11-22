#!/bin/sh
cd "$(dirname "$0")"
PY=python3
[ -x "$HOME/.span-venv/bin/python3" ] && PY="$HOME/.span-venv/bin/python3"
exec "$PY" span.py "$@"
