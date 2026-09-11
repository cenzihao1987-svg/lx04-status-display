#!/bin/bash
# 跑着的时候开关播报：./scripts/speak.sh on|off|toggle
set -e

case "${1:-toggle}" in
  on)     Q="?on=1" ;;
  off)    Q="?on=0" ;;
  toggle) Q="" ;;
  *) echo "用法: $0 on|off|toggle" >&2; exit 1 ;;
esac

curl -fsS -X POST "http://127.0.0.1:8477/api/speak$Q" \
  || { echo "[speak] 失败：状态屏没在跑，或这次启动没加 --speak" >&2; exit 1; }
echo
