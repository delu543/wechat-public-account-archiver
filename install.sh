#!/bin/zsh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
CHECK_ONLY=0
OPEN_CONTROL=1

for argument in "$@"; do
  case "$argument" in
    --check) CHECK_ONLY=1 ;;
    --no-open) OPEN_CONTROL=0 ;;
    *)
      echo "未知参数：$argument" >&2
      exit 2
      ;;
  esac
done

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "此项目仅支持 macOS。" >&2
  exit 1
fi

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "未找到 Python 3.9+。" >&2
  exit 1
fi

if ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)'; then
  echo "Python 版本过低，需要 3.9 或更新版本。" >&2
  exit 1
fi

if ! xcrun --find swiftc >/dev/null 2>&1; then
  echo "未找到 Swift 编译器，请先运行：xcode-select --install" >&2
  exit 1
fi

if [[ ! -d "/Applications/WeChat.app" && ! -d "/Applications/微信.app" \
   && ! -d "$HOME/Applications/WeChat.app" && ! -d "$HOME/Applications/微信.app" ]]; then
  echo "提示：未在常见位置找到 Mac 微信；可以先安装，再运行归档。"
fi

echo "预检通过：macOS、Python 和 Swift 编译器可用。"
if (( CHECK_ONLY )); then
  exit 0
fi

if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
  "$PYTHON_BIN" -m venv "$ROOT/.venv"
fi

"$ROOT/.venv/bin/python" -m pip install --upgrade pip
"$ROOT/.venv/bin/python" -m pip install -r "$ROOT/requirements.txt"
"$ROOT/.venv/bin/python" -m pytest -q "$ROOT/tests"
"$ROOT/.venv/bin/python" "$ROOT/main.py" --config "$ROOT/config.yaml" automation-install-launch-agent

HEALTH_URL="http://127.0.0.1:8765/api/health"
healthy=0
for _ in {1..20}; do
  if /usr/bin/curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then
    healthy=1
    break
  fi
  sleep 1
done

if (( ! healthy )); then
  echo "后台服务未在 20 秒内就绪，请查看：~/Library/Application Support/WeChatPublicAccountArchive/logs/" >&2
  exit 1
fi

echo "安装完成。"
echo "控制台：http://127.0.0.1:8765"
echo "Word：~/Library/Application Support/WeChatPublicAccountArchive/output/weekly/"

if (( OPEN_CONTROL )); then
  /usr/bin/open "http://127.0.0.1:8765"
fi
