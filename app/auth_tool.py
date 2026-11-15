"""操作台鉴权凭据工具：生成 / 轮换 / 查看状态（零依赖，仅标准库）。

用法（在项目根目录执行）：
    python app/auth_tool.py generate     # 首次生成令牌（明文只打印一次）
    python app/auth_tool.py rotate       # 轮换，旧凭据保留 24 小时窗口
    python app/auth_tool.py status       # 查看模式、凭据年龄与轮换窗口

凭据写入项目根 ``.env``（已被 .gitignore 忽略），只保存 salted sha256 派生值；
明文令牌只在本命令的标准输出出现一次，不落盘、不进日志、不进仓库与参赛提交包。
本工具不并入 ``install.py`` 的 LLM 配置流程（LLM_KEYS / offline_env() /
configure_llm()），也不具备「缺配置就降级放行」的 fail-open 语义；仅在 .env
权限收紧这一处复用其纯函数实现，避免两份 ACL 策略漂移。
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_LOG = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.auth import (  # noqa: E402
    DEFAULT_MAX_AGE_DAYS,
    DEFAULT_MODE,
    ROTATION_WINDOW_HOURS,
    AuthConfig,
    generate_token,
    hash_token,
    load_env_file,
    rotation_deadline,
)


def read_env(path: Path) -> dict:
    """只读 .env 自身（不掺入系统环境变量，避免进程里残留的旧值干扰判定）。"""
    return load_env_file(path, env={}, use_os_environ=False)


def upsert_env(path: Path, updates: dict[str, str]) -> None:
    """按 KEY=VALUE 更新 .env：保留注释与其他键，缺失键追加到文件末尾。"""
    try:
        lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    except UnicodeError as exc:
        # 非 UTF-8 .env 拒绝读写，避免按系统默认编码误读后覆盖损坏文件
        print(f"[WARN] {path} 不是 UTF-8 编码，拒绝读写（避免损坏非 UTF-8 文件）", file=sys.stderr)
        raise
    out: list[str] = []
    seen: set[str] = set()
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                out.append(f"{key}={updates[key]}")
                seen.add(key)
                continue
        out.append(line)
    missing = [(k, v) for k, v in updates.items() if k not in seen]
    if missing:
        if out and out[-1].strip():
            out.append("")
        out.append("# 操作台静态令牌鉴权（GCS_AUTH_*，与 GCS_LLM_* 相互独立）")
        out.extend(f"{k}={v}" for k, v in missing)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")  # OSError 由调用处兜底


def harden(path: Path) -> None:
    """收紧 .env 权限：复用 install.py 的单一实现，保持本工具原有输出格式。"""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_gcs_install_for_harden", ROOT / "install.py")
        if spec is None or spec.loader is None:
            raise ImportError(f"无法为 {ROOT / 'install.py'} 创建模块规格")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as exc:
        print(f"WARN 无法加载权限收紧实现（{type(exc).__name__}），请手动检查 .env 的 ACL。")
        return
    status = mod.harden_env_permissions(path, notify=lambda msg: print("WARN " + msg))
    if status == "ok":
        if os.name == "nt":
            print("OK   .env 权限已收紧（仅当前用户可读）。")
        else:
            print("OK   .env 权限已设为 600。")


def _print_token(token: str) -> None:
    print("\n" + "=" * 68)
    print("访问凭据（明文只显示这一次，请立即保存到密码管理器）：")
    print(f"    {token}")
    print("=" * 68)
    print("使用方式：")
    print("  · 浏览器：在操作台页首「访问凭据」输入框粘贴；")
    print("  · curl：  curl -H \"Authorization: Bearer <凭据>\" ...")
    print("  · 请勿写进命令行历史、日志文件、源码、仓库或参赛提交包。")
    print("  · 服务端需重启才会读取新配置（无热加载）。\n")


def cmd_generate(path: Path, force: bool = False) -> int:
    cur = read_env(path)
    if cur.get("GCS_AUTH_TOKEN_HASH") and not force:
        print("检测到已有凭据。轮换请执行 `python app/auth_tool.py rotate`；")
        print("确要强制重置（会使当前凭据立即失效）请追加 --force。")
        return 2
    token = generate_token()
    updates: dict[str, str] = {
        "GCS_AUTH_TOKEN_HASH": hash_token(token),
        "GCS_AUTH_ISSUED_AT": datetime.now(timezone.utc).isoformat(),
    }
    if not cur.get("GCS_AUTH_MODE"):
        updates["GCS_AUTH_MODE"] = DEFAULT_MODE
    if not cur.get("GCS_AUTH_MAX_AGE_DAYS"):
        updates["GCS_AUTH_MAX_AGE_DAYS"] = str(DEFAULT_MAX_AGE_DAYS)
    try:
        upsert_env(path, updates)
    except (OSError, UnicodeError) as exc:
        # 目录不存在 / 无写权限 / 非 UTF-8 .env：给出可操作提示，不抛裸栈
        print(f"[ERROR] 无法写入 {path}：{exc}")
        print("[HINT] 请确认目标目录存在且当前账户有写权限；"
              "也可用 --env 指定一个已存在的 .env 文件路径。")
        return 2
    harden(path)
    print(f"已写入 {path}（模式 {cur.get('GCS_AUTH_MODE', DEFAULT_MODE)}，"
          f"过期上限 {cur.get('GCS_AUTH_MAX_AGE_DAYS', DEFAULT_MAX_AGE_DAYS)} 天）。")
    _print_token(token)
    return 0


def cmd_rotate(path: Path, hours: int = ROTATION_WINDOW_HOURS) -> int:
    if hours < 0:
        print("[ERROR] --hours 不能为负（旧凭据保留窗口至少 0 小时）。")
        return 2
    cur = read_env(path)
    old = cur.get("GCS_AUTH_TOKEN_HASH")
    if not old:
        print("尚未生成凭据，请先执行 `python app/auth_tool.py generate`。")
        return 2
    token = generate_token()
    until = rotation_deadline(hours)
    try:
        upsert_env(path, {
            "GCS_AUTH_TOKEN_PREV_HASH": old,
            "GCS_AUTH_PREV_VALID_UNTIL": until,
            "GCS_AUTH_TOKEN_HASH": hash_token(token),
            "GCS_AUTH_ISSUED_AT": datetime.now(timezone.utc).isoformat(),
        })
    except OSError as exc:
        print(f"[ERROR] 无法写入 {path}：{exc}")
        print("[HINT] 请确认目标目录存在且当前账户有写权限；"
              "也可用 --env 指定一个已存在的 .env 文件路径。")
        return 2
    harden(path)
    print(f"轮换完成：旧凭据在 {until} 前仍可用（窗口 {hours} 小时），之后自动失效。")
    _print_token(token)
    return 0


def cmd_status(path: Path) -> int:
    cfg = AuthConfig(root=path.parent)
    print(f"配置文件：{path}（{'存在' if path.is_file() else '不存在'}）")
    for line in cfg.startup_messages():
        print("  " + line)
    print(f"  允许启动：{'是' if cfg.may_start else '否'}")
    print("  说明：本命令不显示凭据或派生值。")
    return 0


def main(argv: list[str] | None = None) -> int:
    # stdout/stderr 都重配：argparse 的错误提示走 stderr，只配 stdout 会在
    # cp1252 控制台上把中文转成 \uXXXX 转义。
    for _stream in (sys.stdout, sys.stderr):
        _reconfigure = getattr(_stream, "reconfigure", None)
        if _reconfigure is not None:
            try:
                _reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # best-effort：失败不阻断主流程（编码/发送兜底）
                _LOG.debug("流重配 UTF-8 失败", exc_info=True)
    ap = argparse.ArgumentParser(description="绿算调度操作台鉴权凭据工具")
    ap.add_argument("command", nargs="?", default="generate",
                    choices=("generate", "rotate", "status"))
    ap.add_argument("--env", default=None, help="指定 .env 路径（默认项目根 .env）")
    ap.add_argument("--force", action="store_true", help="generate 时强制覆盖已有凭据")
    ap.add_argument("--hours", type=int, default=ROTATION_WINDOW_HOURS,
                    help="rotate 的双凭据窗口时长（默认 24 小时）")
    args = ap.parse_args(argv)
    path = Path(args.env).resolve() if args.env else ROOT / ".env"

    if args.command == "generate":
        return cmd_generate(path, force=args.force)
    if args.command == "rotate":
        return cmd_rotate(path, hours=args.hours)
    return cmd_status(path)


if __name__ == "__main__":
    raise SystemExit(main())
