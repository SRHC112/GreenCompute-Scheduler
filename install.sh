#!/usr/bin/env bash
# 绿算调度一键安装入口（逻辑全部在 install.py，本文件只负责按版本择优找到 Python 并转发参数）
# 约定：本文件必须为 LF 换行、shebang 不得带 CR，否则 macOS/Linux 会报 bad interpreter。
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE" || exit 1

MIN_MAJOR=3
MIN_MINOR=10

# 候选解释器按新到旧排列：优先高版本，最后才回落到 python3 / python。
CANDIDATES="python3.13 python3.12 python3.11 python3.10 python3 python"
DETECTED=""
CHOSEN=""

for py in $CANDIDATES; do
    command -v "$py" >/dev/null 2>&1 || continue
    ver_full="$("$py" --version 2>&1)" || continue
    ver_num="${ver_full#Python }"
    major="${ver_num%%.*}"
    rest="${ver_num#*.}"
    minor="${rest%%.*}"
    case "$major" in
        ''|*[!0-9]*) DETECTED="$DETECTED
  - $py（版本无法解析：$ver_full）" ; continue ;;
    esac
    case "$minor" in
        ''|*[!0-9]*) DETECTED="$DETECTED
  - $py（版本无法解析：$ver_full）" ; continue ;;
    esac
    DETECTED="$DETECTED
  - $py = $ver_num"
    if [ "$major" -gt "$MIN_MAJOR" ] || { [ "$major" -eq "$MIN_MAJOR" ] && [ "$minor" -ge "$MIN_MINOR" ]; }; then
        CHOSEN="$py"
        break
    fi
done

if [ -z "$CHOSEN" ]; then
    echo "错误：未找到 Python >= ${MIN_MAJOR}.${MIN_MINOR} 的解释器，无法继续安装。" >&2
    if [ -n "$DETECTED" ]; then
        echo "已检测到的候选解释器：$DETECTED" >&2
    else
        echo "未在 PATH 中发现任何 python / python3 可执行文件。" >&2
    fi
    echo "" >&2
    echo "提示：macOS 自带的 /usr/bin/python3 通常是 3.9.x，低于本项目要求的 ${MIN_MAJOR}.${MIN_MINOR}。" >&2
    echo "  macOS：brew install python@3.12（或从 https://www.python.org/downloads/ 安装 3.10+）" >&2
    echo "  Linux：sudo apt-get install python3.12（或按发行版用 dnf / pacman 的等价包名）" >&2
    echo "安装后重开终端，确认 python3 --version >= ${MIN_MAJOR}.${MIN_MINOR}，再重新运行 bash install.sh。" >&2
    exit 1
fi

echo "[INFO] 使用解释器：$CHOSEN（$("$CHOSEN" --version 2>&1)）"
exec "$CHOSEN" "$HERE/install.py" "$@"
