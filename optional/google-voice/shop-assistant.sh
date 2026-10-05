#!/bin/sh
# Opens the control panel. With arguments, runs a command instead
# (for example: ./shop-assistant.sh doctor). Run ./install.sh once first.
ROOT="$(cd "$(dirname "$0")" && pwd)"
PY="$ROOT/runtime/python/bin/python3"
if [ ! -x "$PY" ]; then
  echo "Shop Assistant is not installed yet. Run ./install.sh first."
  exit 1
fi
cd "$ROOT/app" || exit 1
export SHOPASSISTANT_HOME="$ROOT"
if [ $# -eq 0 ]; then
  nohup "$PY" -m shopassistant panel >/dev/null 2>&1 &
  exit 0
fi
exec "$PY" -m shopassistant "$@"
