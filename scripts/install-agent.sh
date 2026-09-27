#!/bin/bash
# 把状态屏注册成登录自启的 LaunchAgent。用户级配置，不碰系统设置。
# 重复执行 = 重新安装，是安全的。
set -e

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.gerry.lx04-status"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/lx04-status.log"
BRIDGE_LABEL="com.gerry.lx04-adb-bridge"
BRIDGE_PLIST="$HOME/Library/LaunchAgents/$BRIDGE_LABEL.plist"
BRIDGE_LOG="$HOME/Library/Logs/lx04-adb-bridge.log"

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"

# 这里用不带引号的 EOF：$ROOT / $LOG 要在生成时就展开成真实路径
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>

    <!-- start.sh 自己会 cd 到项目根，不依赖调用方的工作目录 -->
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$ROOT/scripts/start.sh</string>
    </array>

    <key>RunAtLoad</key>
    <true/>
    <!-- 挂了就拉起来：音箱那头没有重试逻辑，服务一断就是白屏 -->
    <key>KeepAlive</key>
    <true/>

    <key>StandardOutPath</key>
    <string>$LOG</string>
    <key>StandardErrorPath</key>
    <string>$LOG</string>
</dict>
</plist>
EOF

plutil -lint "$PLIST" >/dev/null
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"

cat > "$BRIDGE_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$BRIDGE_LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>$ROOT/scripts/adb-bridge.py</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$BRIDGE_LOG</string>
    <key>StandardErrorPath</key>
    <string>$BRIDGE_LOG</string>
</dict>
</plist>
EOF

plutil -lint "$BRIDGE_PLIST" >/dev/null
launchctl unload "$BRIDGE_PLIST" 2>/dev/null || true
launchctl load "$BRIDGE_PLIST"
/usr/bin/python3 "$ROOT/scripts/adb-bridge.py" --open || true

sleep 2
if launchctl list | grep -q "$LABEL" && launchctl list | grep -q "$BRIDGE_LABEL"; then
  echo "[install-agent] 状态屏和 ADB 本机代理已装好并启动"
else
  echo "[install-agent] 有服务没在 launchctl 列表里，查看：$LOG / $BRIDGE_LOG" >&2
fi

cat <<TIP

  配置  $PLIST
  日志  $LOG
  本机代理  $BRIDGE_PLIST
  代理日志  $BRIDGE_LOG

  改了 bridge/run.py 后重启服务：
    launchctl kickstart -k gui/\$(id -u)/$LABEL

  卸载（不留痕迹）：
    launchctl unload $PLIST && rm $PLIST
    launchctl unload $BRIDGE_PLIST && rm $BRIDGE_PLIST

  注意：自启只跑状态屏、不播报——播报要小米账号，
  而 LaunchAgent 读不到你 shell 里的 MI_USER / MI_PASS。
  要播报就手动跑 ./scripts/start.sh --speak
  ADB 本机代理会让音箱固定访问 http://127.0.0.1:8477/。
TIP
