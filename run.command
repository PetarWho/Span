#!/bin/sh
# Double-click in Finder to start Span on macOS.
# (First time: right-click > Open, because the file was downloaded.)
cd "$(dirname "$0")"
PY=python3
[ -x "$HOME/.span-venv/bin/python3" ] && PY="$HOME/.span-venv/bin/python3"
exec "$PY" span.py "$@"
