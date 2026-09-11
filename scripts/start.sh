#!/bin/bash
# 启动状态屏。默认不播报；要播报加 --speak。
set -e
cd "$(dirname "$0")/.."

SPEAK=0
ARGS=()
for a in "$@"; do
  if [ "$a" = "--speak" ]; then SPEAK=1; else ARGS+=("$a"); fi
done

if [ "$SPEAK" = "1" ]; then
  if [ -z "$MI_USER" ] || [ -z "$MI_PASS" ]; then
    echo "[start] --speak 需要小米账号，先跑：source ~/.mibe.env" >&2
    exit 1
  fi
  echo "[start] 状态屏 + 播报"
  # 开了播报就不能回放：回放会把历史事件当新事件，启动时一口气播十几句
  set -- --replay-existing none "${ARGS[@]}"
else
  echo "[start] 只跑状态屏，不播报（要播报加 --speak）"
  # 不回放：run.py 启动时自己读最近几个会话，比重放整个日志快也准
  set -- --no-speaker --replay-existing none "${ARGS[@]}"
fi

exec vendor/mibe/.venv/bin/python bridge/run.py --codex-only "$@"
