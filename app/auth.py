"""操作台静态令牌鉴权（零依赖，仅 Python 标准库）。

设计边界（GCS-CHG-2026-002）：
- 只对 ``app/server.py`` 的 HTTP 接口生效，不改 ``gcs/`` 编排内核、CLI、评测与安装脚本；
- 独立 ``GCS_AUTH_*`` 命名空间，**不复用** ``GCS_LLM_*`` 的降级语义：
  模型层缺 key 是 fail-open（离线 mock），鉴权层必须 fail-closed；
- 不引入 JWT / 第三方框架 / 数据库 / 会话持久化 / TLS；
- 分级鉴权：写接口与含运行轨迹的读接口需鉴权，公开只读接口放行；
- 服务端只存 salted sha256 派生值，明文令牌仅在生成/轮换时打印一次；
- 凭据不进入命令行参数、日志、响应体、仓库与参赛提交包。

三态模式：
    off       完全关闭（仅作为故障回退，不可作默认）
    loopback  回环来源免凭据；非回环来源在受保护接口上要求 Bearer Token（默认）
    required  一律要求 Bearer Token（公开只读 GET 仍放行）

过期与轮换：
    GCS_AUTH_ISSUED_AT + GCS_AUTH_MAX_AGE_DAYS（默认 90 天）；
    loopback 过期仅告警，required 过期拒绝启动（fail-closed）；
    GCS_AUTH_TOKEN_PREV_HASH + GCS_AUTH_PREV_VALID_UNTIL 组成 24 小时双凭据窗口。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

MODES = ("off", "loopback", "required")
DEFAULT_MODE = "loopback"
DEFAULT_MAX_AGE_DAYS = 90
ROTATION_WINDOW_HOURS = 24
TOKEN_HEADER = "Authorization"
AUTH_SCHEME = "Bearer"
REALM = "GreenCompute"
WWW_AUTHENTICATE = f'{AUTH_SCHEME} realm="{REALM}"'

AUTH_KEYS = (
    "GCS_AUTH_MODE",
    "GCS_AUTH_TOKEN_HASH",
    "GCS_AUTH_TOKEN_PREV_HASH",
    "GCS_AUTH_PREV_VALID_UNTIL",
    "GCS_AUTH_ISSUED_AT",
    "GCS_AUTH_MAX_AGE_DAYS",
)

# 分级鉴权：唯一有副作用的写接口
PROTECTED_POST = frozenset({"/api/run"})


def load_env_file(path: Path, env: dict | None = None,
                  use_os_environ: bool = True) -> dict:
    """极简 .env 解析（标准库）：系统环境变量优先，.env 只补缺、不覆盖已有值。

    ``env`` 是显式覆盖层（用于测试注入），优先级高于系统环境；
    ``use_os_environ=False`` 用于凭据工具只读 .env 自身、不受进程环境干扰。
    与 ``install.py::load_dotenv`` 语义一致，但刻意不 import 安装脚本，
    避免鉴权与 LLM 配置耦合。
    """
    base = dict(os.environ) if use_os_environ else {}
    if env:
        base.update({k: str(v) for k, v in env.items()})
    if not path.is_file():
        return base
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return base
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in base:
            base[key] = value
    return base


def generate_token() -> str:
    """生成 256 bit 熵的随机令牌；只在生成/轮换时显示一次，之后不落明文。"""
    return secrets.token_urlsafe(32)


def hash_token(token: str, salt: str | None = None) -> str:
    """salted sha256，返回 ``salt$hexdigest``；.env 泄露也不能直接拿来用。

    令牌本身是高熵随机串（非人类口令），KDF 不带来额外抗爆破收益，
    因此选 salted sha256 而非 pbkdf2，避免每请求派生开销。
    """
    salt = salt or secrets.token_hex(16)
    digest = hashlib.sha256((salt + token).encode("utf-8")).hexdigest()
    return f"{salt}${digest}"


def verify_token(token: str, stored: str) -> bool:
    """常量时间比较；格式错误一律视为失败（fail-closed）。"""
    if not token or not stored or "$" not in stored:
        return False
    salt, _ = stored.split("$", 1)
    return hmac.compare_digest(hash_token(token, salt), stored)


def parse_bearer(value: str | None) -> str:
    """从 Authorization 头取 Bearer 令牌；非 Bearer 一律返回空串。"""
    if not value:
        return ""
    parts = value.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != AUTH_SCHEME.lower():
        return ""
    return parts[1].strip()


def is_loopback(addr) -> bool:
    """client_address 是否为回环来源。

    注意：反代 / Docker / SSH 端口转发下该值可能显示为回环，故 loopback
    模式只是「本机便利 + 对外自动收紧」，不是身份边界；需要强身份边界时用
    required 模式。
    """
    ip = addr[0] if isinstance(addr, (tuple, list)) and addr else str(addr or "")
    return ip == "::1" or ip == "localhost" or ip.startswith("127.")


def requires_auth(method: str, path: str) -> bool:
    """分级鉴权判定。

    受保护：``POST /api/run``（唯一有副作用）、``GET /api/run/<rid>``、
    ``GET /console/<rid>``（含完整轨迹与 LLM 用量）。
    公开：``/``、``/api/policies``、``/api/eval``、``/api/runs`` 等只读接口，
    保证 CI 的 ``curl /`` 探测、录屏深链接与作品证据展示不受影响。
    """
    m = method.upper()
    if m == "POST":
        return path in PROTECTED_POST
    if m == "GET":
        return path.startswith("/api/run/") or path.startswith("/console/")
    return False


def _parse_time(value: str) -> datetime | None:
    """解析 ISO 8601 时间；无时区按 UTC 处理，解析失败返回 None。"""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class AuthResult:
    """一次鉴权判定的结果；reason 只进服务端日志，不回显给客户端。"""
    ok: bool
    reason: str


class AuthConfig:
    """进程启动时读取一次配置（无热加载）， thereafter 只读。"""

    def __init__(self, env: dict | None = None, root: Path | None = None):
        root = root or Path(__file__).resolve().parent.parent
        merged = load_env_file(root / ".env", env)
        self.root = root
        self.mode = (merged.get("GCS_AUTH_MODE") or DEFAULT_MODE).strip().lower()
        self.token_hash = (merged.get("GCS_AUTH_TOKEN_HASH") or "").strip()
        self.prev_hash = (merged.get("GCS_AUTH_TOKEN_PREV_HASH") or "").strip()
        self.prev_until_raw = (merged.get("GCS_AUTH_PREV_VALID_UNTIL") or "").strip()
        self.issued_at_raw = (merged.get("GCS_AUTH_ISSUED_AT") or "").strip()
        try:
            self.max_age_days = int(merged.get("GCS_AUTH_MAX_AGE_DAYS")
                                    or DEFAULT_MAX_AGE_DAYS)
        except ValueError:
            self.max_age_days = DEFAULT_MAX_AGE_DAYS
        self.warnings: list[str] = []
        self.errors: list[str] = []

        if self.mode not in MODES:
            # 未知取值按最严格模式处理，绝不静默降级为 off
            self.errors.append(f"未知 GCS_AUTH_MODE={self.mode!r}，按 required 处理")
            self.mode = "required"
        self.enabled = self.mode != "off"

        if self.enabled and not self.token_hash:
            msg = "未配置 GCS_AUTH_TOKEN_HASH"
            if self.mode == "required":
                self.errors.append(msg + "：required 模式拒绝启动（fail-closed）")
            else:
                self.warnings.append(msg + "：loopback 模式仅本机可用，非回环请求一律 401")
        if self.issued_at_raw and _parse_time(self.issued_at_raw) is None:
            self.warnings.append("GCS_AUTH_ISSUED_AT 格式不可解析，已跳过过期检查")
        if self.expired:
            msg = f"凭据已超过 {self.max_age_days} 天上限"
            if self.mode == "required":
                self.errors.append(msg + "：required 模式拒绝启动，请先轮换")
            else:
                self.warnings.append(msg + "：loopback 模式仅告警，请尽快轮换")

    # ------------------------------------------------------------------
    @property
    def age_days(self) -> float | None:
        dt = _parse_time(self.issued_at_raw)
        if dt is None:
            return None
        return (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0

    @property
    def expired(self) -> bool:
        age = self.age_days
        return age is not None and age > self.max_age_days

    @property
    def prev_valid(self) -> bool:
        """旧凭据是否仍在轮换窗口内；未显式配置窗口即视为失效。"""
        if not self.prev_hash or not self.prev_until_raw:
            return False
        until = _parse_time(self.prev_until_raw)
        return until is not None and datetime.now(timezone.utc) <= until

    @property
    def may_start(self) -> bool:
        """fail-closed：required 模式缺凭据或凭据过期时拒绝启动。"""
        return not self.errors

    # ------------------------------------------------------------------
    def startup_messages(self) -> list[str]:
        """启动横幅：可见地暴露配置状态，绝不打印令牌或派生值。"""
        out = [f"鉴权模式：{self.mode}（凭据{'已配置' if self.token_hash else '未配置'}）"]
        if not self.enabled:
            out.append("WARN GCS_AUTH_MODE=off：所有接口无需凭据，仅限可信本机调试")
        elif self.mode == "loopback":
            out.append("本机回环免凭据；非回环来源在写接口 / 运行轨迹 / 看板文件上要求 Bearer Token")
        else:
            out.append("所有受保护接口一律要求 Bearer Token（公开只读 GET 仍放行）")
        age = self.age_days
        if age is not None:
            out.append(f"凭据已签发 {age:.1f} 天 / 上限 {self.max_age_days} 天"
                       + ("（已过期）" if self.expired else ""))
        else:
            out.append("未记录签发时间（GCS_AUTH_ISSUED_AT），跳过过期检查")
        if self.prev_valid:
            out.append(f"轮换窗口开启：旧凭据在 {self.prev_until_raw} 前仍有效")
        for w in self.warnings:
            out.append("WARN " + w)
        for e in self.errors:
            out.append("ERROR " + e)
        return out

    # ------------------------------------------------------------------
    def check(self, headers, client_address, method: str, path: str) -> AuthResult:
        """执行一次鉴权判定；所有失败原因统一收敛为 401，避免凭据探测。"""
        if not self.enabled:
            return AuthResult(True, "off")
        if not requires_auth(method, path):
            return AuthResult(True, "public")
        if self.mode == "loopback" and is_loopback(client_address):
            return AuthResult(True, "loopback")
        token = parse_bearer(headers.get(TOKEN_HEADER))
        if not token:
            return AuthResult(False, "missing")
        if not self.token_hash:
            return AuthResult(False, "unconfigured")
        if verify_token(token, self.token_hash):
            return AuthResult(True, "token")
        if self.prev_valid and self.prev_hash and verify_token(token, self.prev_hash):
            return AuthResult(True, "prev")
        return AuthResult(False, "invalid")


def rotation_deadline(hours: int = ROTATION_WINDOW_HOURS) -> str:
    """生成轮换窗口截止时间的 ISO 8601 字符串（UTC）。"""
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()
