#!/bin/bash
# 把状态屏注册成登录自启的 LaunchAgent。用户级配置，不碰系统设置。
# 重复执行 = 重新安装，是安全的。
set -e

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.lx04.status"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/lx04-status.log"

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

sleep 2
if launchctl list | grep -q "$LABEL"; then
  echo "[install-agent] 已装好并启动"
else
  echo "[install-agent] 装好了，但没在 launchctl 列表里，看日志：$LOG" >&2
fi

cat <<TIP

  配置  $PLIST
  日志  $LOG

  改了 bridge/run.py 后重启服务：
    launchctl kickstart -k gui/\$(id -u)/$LABEL

  卸载（不留痕迹）：
    launchctl unload $PLIST && rm $PLIST

  注意：自启只跑状态屏、不播报——播报要小米账号，
  而 LaunchAgent 读不到你 shell 里的 MI_USER / MI_PASS。
  要播报就手动跑 ./scripts/start.sh --speak
TIP
