#!/bin/sh
# 安裝或移除開機自動啟動。LaunchAgent 的路徑依這個資料夾和目前使用者產生，任何位置都能用。
#   ./install.sh             安裝並啟動
#   ./install.sh uninstall   停止並移除
set -e
cd "$(dirname "$0")"
DIR=$(pwd)
LABEL=com.hyjay.voicetype
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

launchctl bootout "$DOMAIN" "$PLIST" 2>/dev/null || true
if [ "$1" = "uninstall" ]; then
  rm -f "$PLIST"
  echo "已移除開機自動啟動"
  exit 0
fi

command -v uv >/dev/null || { echo "需要先安裝 uv：https://docs.astral.sh/uv/"; exit 1; }
uv sync
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$DIR/.venv/bin/python</string>
    <string>$DIR/voicetype.py</string>
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>EnvironmentVariables</key><dict><key>VOICETYPE_AUTOSTART</key><string>1</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>$HOME/Library/Logs/voicetype.log</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/voicetype.log</string>
</dict>
</plist>
PLIST
launchctl bootstrap "$DOMAIN" "$PLIST"
echo "已啟動。管理介面會自動打開；權限要給這個執行檔（輔助使用、輸入監控）："
echo "  $(realpath .venv/bin/python)"
echo "日誌：tail -f ~/Library/Logs/voicetype.log"
