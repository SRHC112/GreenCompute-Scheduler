#!/usr/bin/env bash
# 绿算调度一键安装入口（逻辑全部在 install.py，本文件只负责找到 Python 并转发参数）
set -e
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"
if command -v python3 >/dev/null 2>&1; then PY=python3; else PY=python; fi
exec "$PY" "$HERE/install.py" "$@"
