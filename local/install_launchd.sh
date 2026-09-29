#!/usr/bin/env bash
# open_checkout.py を、毎日 11:58 に自動で起動するよう macOS（launchd）に登録する。
# 11:59 に基準を取り、12:00〜12:05 のあいだ確認して、見つかったらブラウザで開く。
#
# 使い方（リポジトリの直下で）:
#   bash local/install_launchd.sh            # 登録（登録し直しも同じ）
#   bash local/install_launchd.sh uninstall  # 解除
set -euo pipefail

LABEL="com.mellojoy.open-checkout"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
LOG="$HOME/Library/Logs/mellojoy-open-checkout.log"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="$REPO/.venv/bin/python"

launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true

if [[ "${1:-}" == "uninstall" ]]; then
  rm -f "$PLIST"
  echo "解除しました"
  exit 0
fi

if [[ ! -x "$PYTHON" ]]; then
  echo "$PYTHON が見つかりません。README の「ローカルで試す」の手順 2 で .venv を作ってください" >&2
  exit 1
fi

mkdir -p "$(dirname "$PLIST")" "$(dirname "$LOG")"
# caffeinate -i で、監視中に Mac がスリープしないようにする
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/caffeinate</string>
    <string>-i</string>
    <string>${PYTHON}</string>
    <string>${REPO}/local/open_checkout.py</string>
    <string>--at</string><string>12:00</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key><integer>11</integer>
    <key>Minute</key><integer>58</integer>
  </dict>
  <key>StandardOutPath</key><string>${LOG}</string>
  <key>StandardErrorPath</key><string>${LOG}</string>
</dict>
</plist>
EOF

launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "登録しました（毎日 11:58 に起動）"
echo "  ログ: $LOG"
