#!/bin/zsh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"

if [[ -x "$ROOT/.venv/bin/python" ]]; then
  "$ROOT/.venv/bin/python" "$ROOT/main.py" --config "$ROOT/config.yaml" automation-uninstall-launch-agent
else
  launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.local.wechat-public-account-archive-status.plist" 2>/dev/null || true
  launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.local.wechat-public-account-archive.plist" 2>/dev/null || true
  rm -f "$HOME/Library/LaunchAgents/com.local.wechat-public-account-archive-status.plist"
  rm -f "$HOME/Library/LaunchAgents/com.local.wechat-public-account-archive.plist"
fi

echo "自动服务已停止。数据库和 Word 仍保留在："
echo "~/Library/Application Support/WeChatPublicAccountArchive/"
