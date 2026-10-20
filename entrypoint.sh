#!/bin/sh
# Restart the bot after unexpected exits. Telegram polling conflicts are different:
# exit 23 means another instance owns the token, so stop rather than fight it.
set -u
delay=2
child=""

shutdown() {
  if [ -n "$child" ]; then
    kill -TERM "$child" 2>/dev/null || true
    wait "$child" 2>/dev/null || true
  fi
  exit 0
}
trap shutdown TERM INT

while true; do
  started="$(date +%s)"
  echo "[pear-entrypoint] Starting Pear CM..."
  python -u iris.py &
  child=$!
  wait "$child"
  code=$?
  child=""

  if [ "$code" -eq 23 ]; then
    echo "[pear-entrypoint] Telegram polling conflict: another instance is using this token. Retrying in 60s."
    sleep 60 &
    child=$!
    wait "$child" || true
    child=""
    delay=2
    continue
  fi

  ended="$(date +%s)"
  uptime=$((ended - started))
  if [ "$uptime" -ge 300 ]; then
    delay=2
  fi
  echo "[pear-entrypoint] Bot exited with code $code after ${uptime}s; restarting in ${delay}s."
  sleep "$delay" &
  child=$!
  wait "$child" || true
  child=""

  if [ "$delay" -lt 60 ]; then
    delay=$((delay * 2))
    if [ "$delay" -gt 60 ]; then delay=60; fi
  fi
done
