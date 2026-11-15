#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""绿算调度 GreenCompute-Scheduler · 一键安装脚本。

特点：
- **只用标准库**，运行本脚本之前不需要安装任何依赖（PyYAML 由脚本装进虚拟环境）；
- **跨平台**：Windows / macOS / Linux，自动识别虚拟环境目录；
- **幂等且不覆盖**：`.venv` / `.env` / `run.bat` / `run.sh` / `requirements.txt` 等
  已存在时一律跳过，不删除、不改写用户配置；
- **不改动项目现有源码**：模型配置通过运行入口注入环境变量生效
  （项目本身只读 os.getenv，不会自动加载 .env）；
- 安装校验阶段强制离线模式，**不会消耗模型额度**。

用法：
    python install.py                     一键安装（交互询问是否接入大模型）
    python install.py --no-llm            跳过交互，离线模式安装（自动化 / CI）
    python install.py --llm               进入大模型配置（可用环境变量提供 Key）
    python install.py --check             仅预检（含 PyYAML 运行时依赖校验），不做任何修改
    python install.py --dir DIR           指定项目目录（脚本不在项目内时自动 clone 到此）
    python install.py --run demo          安装完成后运行（自动加载 .env + 虚拟环境）
    python install.py --run server       启动可视化操作台
    python install.py --run test / eval
    python install.py --run -- python -m gcs.demo --policy fcfs

脚本不在项目目录内运行时，会自动 clone 官方仓库到 ./GreenCompute-Scheduler 再安装。
"""

from __future__ import annotations

import argparse
import getpass
import locale
import logging
import os
import re
import shutil
import subprocess
import sys
from urllib.parse import urlparse
from pathlib import Path

REPO_URL = "https://github.com/SRHC112/GreenCompute-Scheduler.git"
MIN_PY = (3, 10)
_LOG = logging.getLogger(__name__)
# --check 的运行时依赖校验范围：requirements.txt 声明的唯一第三方运行时依赖
PY_YAML_MIN = (6, 0)       # 含
PY_YAML_MAX = (7, 0)       # 不含
SCRIPT_DIR = Path(__file__).resolve().parent
CWD = Path.cwd()

# 判定「这是一个完整项目」所需的关键文件
REQUIRED = (
    "requirements.txt",
    "gcs/__init__.py",
    "gcs/demo.py",
    "gcs/llm.py",
    "app/server.py",
    "knowledge/ningxia_cluster.yaml",
    "tests/test_smoke.py",
)

LLM_KEYS = ("GCS_LLM_BASE_URL", "GCS_LLM_API_KEY", "GCS_LLM_MODEL")
DEFAULT_BASE = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"

# --run 快捷命令
ALIASES = {
    "demo": ["-m", "gcs.demo", "--jobs", "60", "--seed", "20261111"],
    "server": ["app/server.py", "--open"],
    "test": ["-m", "unittest", "discover", "-s", "tests", "-v"],
    "eval": ["evaluation/run_eval.py"],
}

# 与 install.bat 同约束：ASCII-only（cmd 在多字节文本下会截断行）、
# 块内不出现括号、结尾透传退出码；中文提示由 install.py 以 UTF-8 输出。
RUN_BAT = """@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\\Scripts\\python.exe" (
  echo [ERROR] Not installed yet. Please run install.bat first.
  exit /b 1
)
".venv\\Scripts\\python.exe" "install.py" --run %*
set "GCS_RC=%errorlevel%"
if not "%GCS_RC%"=="0" (
  echo.
  echo [ERROR] Run failed - see the Chinese messages above.
)
exit /b %GCS_RC%
"""

RUN_SH = """#!/usr/bin/env bash
# 由 install.py 生成：自动加载 .env 并使用项目虚拟环境运行
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE" || exit 1
if [ ! -x ".venv/bin/python" ]; then
  echo "[ERROR] 尚未安装，请先运行 ./install.sh"
  exit 1
fi
exec ".venv/bin/python" "install.py" --run "$@"
"""

ENV_HEADER = (
    "# 由 install.py 生成。此文件已被 .gitignore 忽略，请勿提交到仓库。\n"
    "# 通过 run.bat / run.sh 运行时会自动加载到环境变量；也可手动修改后保存。\n"
)


class InstallError(Exception):
    """可预期错误：携带面向用户的中文说明与处理建议。"""

    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.message = message
        self.hint = hint


# ---------------------------------------------------------------- 输出与执行
def setup_console() -> None:
    """Windows 控制台默认 cp1252，中文输出会抛 UnicodeEncodeError，这里统一处理。

    当前口径：子进程输出按本地首选编码（中文系统通常 GBK/CP936）用
    errors="replace" 解码，保证「安装脚本本身输出」与「子进程输出」两套编码
    互不影响；不做 chcp 变更，避免 chcp 65001 与子进程解码发生冲突。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            # line_buffering 避免脚本日志与子进程输出交错乱序。
            # 用 getattr 取 reconfigure：静态类型是 TextIO 协议，未声明该方法
            # （mypy union-attr），运行时不存在时跳过即可。
            reconfigure = getattr(stream, "reconfigure", None)
            if reconfigure is not None:
                reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except Exception:  # best-effort：非 TTY / 已重定向流不支持 reconfigure
            _LOG.debug("控制台 UTF-8 重配失败", exc_info=True)


def info(msg: str) -> None:
    print(f"[INFO] {msg}")


def ok(msg: str) -> None:
    print(f"[ OK ] {msg}")


def warn(msg: str) -> None:
    print(f"[WARN] {msg}")


def skip(msg: str) -> None:
    print(f"[SKIP] {msg}")


def _child_encoding() -> str:
    """子进程输出解码编码：中文系统按本地首选编码（GBK/CP936），保证子进程
    失败提示可读；errors="replace" 保证解码失败不会中断流程。

    与 setup_console 的策略统一：安装脚本自己输出统一按 UTF-8 + errors="replace"
    处理，子进程按本地首选编码处理，两套策略互不重叠，不做 chcp 变更。
    """
    if os.name == "nt":
        try:
            return locale.getpreferredencoding(False) or "utf-8"
        except Exception:  # best-effort：极端环境下退回 UTF-8
            return "utf-8"
    return "utf-8"


def run(cmd, *, cwd=None, env=None, capture=True, check=False, hint=""):
    """统一子进程执行；失败时附带末尾输出与处理建议。"""
    argv = [str(c) for c in cmd]
    try:
        proc = subprocess.run(
            argv, cwd=str(cwd) if cwd else None, env=env,
            capture_output=capture, text=True,
            encoding=_child_encoding(), errors="replace",
        )
    except OSError as exc:
        raise InstallError(f"无法执行命令：{argv[0]}", f"系统错误：{exc}") from exc
    if check and proc.returncode != 0:
        detail = ((proc.stdout or "") + (proc.stderr or "")).strip()
        tail = "\n".join(detail.splitlines()[-12:])
        raise InstallError(
            f"命令失败（退出码 {proc.returncode}）：{' '.join(argv)}",
            (hint + ("\n--- 输出末尾 ---\n" + tail if tail else "")).strip(),
        )
    return proc


def run_checked(cmd, *, cwd=None, env=None, hint=""):
    return run(cmd, cwd=cwd, env=env, check=True, hint=hint)


def offline_env() -> dict:
    """剥离模型相关变量，保证安装校验阶段一定是离线模式。"""
    return {k: v for k, v in os.environ.items() if k not in LLM_KEYS}


# ---------------------------------------------------------------- 预检与定位
def check_python() -> None:
    v = sys.version_info[:2]
    if v < MIN_PY:
        raise InstallError(
            f"Python 版本过低：当前 {v[0]}.{v[1]}，项目要求 >= {MIN_PY[0]}.{MIN_PY[1]}",
            "请安装 Python 3.10+ 后重新运行（Windows 安装时勾选 Add to PATH）。",
        )
    ok(f"Python 环境：{sys.version.split()[0]}（要求 >= 3.10）")


def check_runtime_deps() -> str:
    """--check 的运行时依赖校验：仅校验 PyYAML，不联网、不安装、不写任何文件。

    注：预检阶段会写入并立即删除一个可写性探针文件（.gcs_install_probe），
    除此之外不改动项目内任何文件。

    校验范围：requirements.txt 声明的唯一第三方运行时依赖 PyYAML（版本 >=6.0,<7.0）；
    测试期 / CI 期依赖、模型 SDK、可选组件均不在本校验范围内。
    失败条件（任一即返回非零并给中文提示）：
      1) importlib.util.find_spec("yaml") 为 None —— 未安装；
      2) import yaml 抛异常 —— 已安装但不可导入；
      3) yaml.__version__ 不在 [6.0, 7.0) —— 版本不兼容或无法解析。
    """
    import importlib.util
    if importlib.util.find_spec("yaml") is None:
        raise InstallError(
            "运行时依赖缺失：未找到 PyYAML（yaml 模块）",
            "--check 只做只读预检，不会安装任何东西；请运行 python install.py 完整安装，"
            "或手动执行 pip install -r requirements.txt。",
        )
    try:
        import yaml
    except Exception as exc:  # 导入期异常统一转成可操作的中文提示
        raise InstallError(
            f"运行时依赖不可导入：PyYAML 加载失败（{exc}）",
            "请重新安装依赖：pip install -r requirements.txt；或运行 python install.py 完整安装。",
        ) from exc
    ver = str(getattr(yaml, "__version__", ""))
    m = re.match(r"^(\d+)\.(\d+)", ver)
    got = (int(m.group(1)), int(m.group(2))) if m else None
    if got is None or not (PY_YAML_MIN <= got < PY_YAML_MAX):
        raise InstallError(
            f"运行时依赖版本不兼容：PyYAML {ver or 'unknown'}，要求 "
            f">= {PY_YAML_MIN[0]}.{PY_YAML_MIN[1]} 且 < {PY_YAML_MAX[0]}.{PY_YAML_MAX[1]}",
            "请执行 pip install -r requirements.txt 更新到受支持版本后重新预检。",
        )
    ok(f"运行时依赖：PyYAML {ver}（>=6.0,<7.0，已导入；--check 不安装、不联网）")
    return ver


def is_valid_project(path: Path) -> bool:
    return all((path / rel).is_file() for rel in REQUIRED)


def find_project_root() -> Path | None:
    for candidate in (SCRIPT_DIR, CWD):
        if is_valid_project(candidate):
            return candidate.resolve()
    return None


def ensure_project(target: str | None) -> tuple[Path, bool]:
    """返回 (项目根目录, 是否为本次自动 clone)。绝不覆盖已有目录。"""
    root = find_project_root()
    if root:
        if target:
            info(f"已定位到项目目录，--dir 参数忽略：{root}")
        return root, False

    info("当前目录不是项目根目录，准备获取项目源码")
    dest = (Path(target).expanduser().resolve() if target
            else (CWD / "GreenCompute-Scheduler").resolve())
    if is_valid_project(dest):
        ok(f"复用已有项目目录：{dest}")
        return dest, False
    if dest.exists() and any(dest.iterdir()):
        raise InstallError(
            f"目标目录已存在且非空：{dest}",
            "为避免覆盖数据，请用 --dir 指定一个空目录，或把本脚本放到项目根目录后再运行。",
        )

    git = shutil.which("git")
    if not git:
        raise InstallError(
            "未检测到 git，无法自动获取项目源码",
            "请手动下载项目 ZIP 并解压，把 install.py 放进项目根目录后重新运行；"
            "或安装 Git 后重试。",
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    info(f"正在 clone 项目到 {dest}（仅首次需要）……")
    run_checked([git, "clone", "--depth", "1", REPO_URL, str(dest)],
                hint="请检查网络或代理设置，也可手动下载源码后把脚本放进项目根目录。")
    if not is_valid_project(dest):
        raise InstallError("clone 完成后项目结构不完整", "请删除该目录后重试。")
    return dest, True


def write_llm_env(env_path: Path, cfg: dict) -> None:
    """把 LLM 配置写入 .env：保留已有非 LLM 键（如 GCS_AUTH_*、注释），仅覆盖/追加 LLM 键。

    这样 `install.py --llm` 不会抹掉 `auth_tool generate` 写入的鉴权凭据（M8 口径统一）。
    """
    keep: list[str] = []
    if env_path.exists():
        try:
            for ln in env_path.read_text(encoding="utf-8").splitlines():
                if not any(ln.strip().startswith(k + "=") for k in LLM_KEYS):
                    keep.append(ln)
        except (OSError, UnicodeError) as exc:
            # 权限或编码异常下都不能静默清空 keep，否则会抹掉 .env 里的 GCS_AUTH_* 等键
            reason = ("内容不是有效的 UTF-8 文本" if isinstance(exc, UnicodeError)
                      else "无法读取")
            raise InstallError(
                f"已有 .env {reason}：{exc}",
                "为避免覆盖其中的鉴权配置，本次未写入；请确认当前账户对 .env "
                "有读取权限（Windows 可检查 ACL）、且文件为 UTF-8 编码后重试。",
            ) from exc
    while keep and keep[-1].strip() == "":
        keep.pop()
    if not keep:
        text = ENV_HEADER + "".join(f"{k}={cfg[k]}\n" for k in LLM_KEYS)
    else:
        text = "\n".join(keep) + "\n" + "".join(f"{k}={cfg[k]}\n" for k in LLM_KEYS)
    # 取值校验：值里带换行会被 .env 解析成第二个键（配置注入），
    # 带首尾空白则会被读取时的 strip() 静默截断（Key 变短 → 一直 401）
    for key, value in cfg.items():
        raw = str(value)
        if any(ch in raw for ch in "\r\n\0"):
            raise InstallError(
                f"{key} 的取值包含换行或空字符，拒绝写入 .env",
                "这类取值会在 .env 解析时被当成额外的键（配置注入），"
                "请删除换行后重试。",
            )
        if raw != raw.strip():
            raise InstallError(
                f"{key} 的取值包含首尾空白，拒绝写入 .env",
                "读取时的 strip() 会静默截断该值（例如把 API Key 改短），请去掉空白后重试。",
            )
    # 原子写：先写临时文件再 os.replace，避免中途崩溃留下半截 .env
    # newline="\n"：.env 用 LF；Windows 工具链读 .env 到 CRLF 可能引起多余空白。
    tmp_path = env_path.with_name(env_path.name + ".tmp")
    tmp_path.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp_path, env_path)
    harden_env_permissions(env_path)


def precheck(root: Path) -> None:
    missing = [rel for rel in REQUIRED if not (root / rel).is_file()]
    if missing:
        raise InstallError(
            f"项目文件缺失：{', '.join(missing)}",
            "请确认下载的是完整源码（而非单个文件），或重新 clone 仓库。",
        )
    # 知识包只做「语法 + 顶层结构」预检（不 import 项目模块，避免与安装脚本耦合）：
    # 语法错误应在这里暴露，而不是等首次运行才失败。
    #
    # 但「系统 Python 未装 PyYAML」是全新机器的常态，不能把它报成「知识包解析失败」
    # ——那会把「依赖未装」误导成「配置文件写坏了」，用户会去改 YAML 而问题在依赖。
    # 因此这里把两种情况分开：缺依赖 → 跳过并明确说明；YAML 真坏 → 报解析失败。
    # 完整安装路径下，知识包仍会被校验：venv 装好依赖后 verify_install() 跑单测，
    # 单测会加载知识包，等价校验不会丢失（只是推迟到依赖就绪之后）。
    kb = root / "knowledge" / "ningxia_cluster.yaml"
    if kb.is_file():
        try:
            import yaml  # noqa: F401  （仅探测可用性；解析在 else 分支）
        except ImportError:
            skip("知识包语法预检：当前解释器未安装 PyYAML，已跳过"
                 "（完整安装会在依赖装好后由单元测试加载知识包完成校验）")
        else:
            try:
                data = yaml.safe_load(kb.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("知识包根节点必须是映射（键值对）")
            except Exception as exc:
                raise InstallError(
                    f"知识包解析失败：{type(exc).__name__}: {exc}",
                    "请检查 knowledge/ningxia_cluster.yaml 的 YAML 语法与顶层结构；"
                    "也可先执行 python -m gcs.demo --jobs 5（冒烟演示）确认知识包可正常加载。",
                ) from exc
    probe = root / ".gcs_install_probe"
    try:
        # 探针写固定 LF，避免 Windows CRLF 与 .gitattributes eol=lf 漂移；内容为空
        probe.write_text("ok", encoding="utf-8", newline="\n")
    except OSError as exc:
        # P3-9：探针失败 ≠ 目录不可写（也可能是安全软件/组策略拦截新建文件）。
        # 用 os.access 做二次判定，只有两者都认为不可写时才下「不可写」结论，减少误报。
        if not os.access(root, os.W_OK):
            raise InstallError(f"项目目录不可写：{root}",
                               f"系统错误：{exc}；请换一个有写权限的目录。") from exc
        raise InstallError(
            f"项目目录写入探针失败：{root}",
            f"系统错误：{exc}；目录权限显示可写，可能是安全软件或组策略拦截了新建文件，"
            "请改用用户主目录下的目录后重试。",
        ) from exc
    try:
        probe.unlink()
    except OSError:  # best-effort：残留探针不影响安装，不阻断主流程
        _LOG.debug("安装探针清理失败：%s", probe, exc_info=True)
    ok("项目结构完整，目录可写")


# ---------------------------------------------------------------- 安装
def venv_python(root: Path) -> Path:
    if os.name == "nt":
        return root / ".venv" / "Scripts" / "python.exe"
    return root / ".venv" / "bin" / "python"


def ensure_venv(root: Path) -> Path:
    py = venv_python(root)
    if py.is_file():
        p = run([str(py), "-c", "import sys; sys.exit(0)"], cwd=root)
        if p.returncode == 0:
            skip(f"虚拟环境已存在，直接复用（不覆盖）：{root / '.venv'}")
            return py
        raise InstallError(
            f"已有虚拟环境无法运行：{root / '.venv'}",
            "为避免破坏数据，脚本不会自动删除；请手动移除 .venv 后重新运行。",
        )
    info("创建虚拟环境 .venv ……")
    run_checked([sys.executable, "-m", "venv", str(root / ".venv")], cwd=root,
                hint="若提示 ensurepip 缺失，请安装带 venv 组件的 Python 发行版。")
    if not py.is_file():
        raise InstallError("虚拟环境创建后未找到解释器", "请检查 Python 安装是否完整。")
    ok("虚拟环境创建完成")
    return py


def install_deps(root: Path, py: Path) -> str:
    info("安装依赖（PyYAML）……")
    run_checked([str(py), "-m", "pip", "install", "--disable-pip-version-check",
                 "-r", "requirements.txt"], cwd=root,
                hint="若因网络失败，可先设置代理，或手动执行："
                     "pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple")
    p = run_checked([str(py), "-c", "import yaml; print(yaml.__version__)"], cwd=root,
                    hint="依赖已安装但无法导入，请检查虚拟环境是否完整。")
    ver = (p.stdout or "").strip() or "unknown"
    ok(f"依赖安装完成：PyYAML {ver}")
    return ver


def verify_install(root: Path, py: Path) -> str:
    """全部在离线环境下验证，避免误触发模型调用 / 消耗额度。"""
    env = offline_env()
    p = run([str(py), "-m", "unittest", "discover", "-s", "tests"], cwd=root, env=env)
    out = (p.stdout or "") + (p.stderr or "")
    if p.returncode != 0 or "OK" not in out:
        tail = "\n".join(out.strip().splitlines()[-12:])
        raise InstallError("单元测试未通过", tail or "请查看上方测试输出。")
    m = re.search(r"Ran (\d+) tests", out)
    count = m.group(1) if m else "?"
    ok(f"单元测试：{count} 个用例全部通过")
    run_checked([str(py), "-m", "gcs.demo", "--jobs", "20", "--seed", "7"],
                cwd=root, env=env, hint="编排演示失败，请检查上方错误输出。")
    ok("编排演示：通过（强制离线模式，未消耗模型额度）")
    return count


# ---------------------------------------------------------------- 基础配置
def ensure_dirs(root: Path) -> None:
    runs = root / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    gitkeep = runs / ".gitkeep"
    if not gitkeep.exists():
        # 空文件；LF 保持与项目文本约定一致。
        gitkeep.write_text("", encoding="utf-8", newline="\n")
        ok("已创建运行产物目录 runs/")
    else:
        skip("runs/ 与 .gitkeep 已存在")


def load_dotenv(path: Path, env: dict) -> None:
    """极简 .env 解析：只认 KEY=VALUE，忽略注释/空行；不覆盖 env 中已有值。"""
    if not path.is_file():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        print(f"[WARN] 读取 {path} 失败（{type(exc).__name__}），按未配置继续：{exc}",
              file=sys.stderr)
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in env:
            env[key] = value


def _env_readable(path: Path) -> bool:
    """回读自检：确认当前进程仍能读 .env（收紧 ACL 后可能把自己锁在文件外）。

    契约：任何读取异常（权限 / 非 UTF-8 编码）一律返回 False，不向外抛，
    避免被调用处的外层 except 误归因为 icacls 故障。
    """
    try:
        path.read_text(encoding="utf-8")
        return True
    except (OSError, UnicodeError):
        return False


def _windows_account() -> str:
    """返回 icacls 可接受的当前账户标识；无法确定时返回空串。

    USERNAME 在服务账户 / CI / 非交互 shell 下可能缺失，先退回 getpass；
    域环境（USERDOMAIN 与本机名不同）需要 DOMAIN\\user 形式才解析得到账户。
    """
    user = os.environ.get("USERNAME") or ""
    if not user:
        try:
            user = getpass.getuser()
        except Exception:  # best-effort：极简环境无 pwd/环境变量，按未知账户处理
            _LOG.debug("getpass.getuser() 失败，按未知账户处理", exc_info=True)
            return ""
    if not user:
        return ""
    domain = os.environ.get("USERDOMAIN") or ""
    computer = os.environ.get("COMPUTERNAME") or ""
    if domain and computer and domain.upper() != computer.upper() and "\\" not in user:
        return f"{domain}\\{user}"
    return user


def _restore_env_acl(path: Path, account: str, notify=None) -> bool:
    """尽力回滚：恢复继承并重新显式授权当前账户，保证 .env 仍可访问。

    语义是「恢复可访问性」而非精确还原原 ACL；``/grant:r`` 为 replace，
    重复调用不会累积 ACE。返回是否成功；失败时通过 notify 单独告警，
    调用处据此区分「已回滚」与「回滚失败」，不静默吞错。
    """
    try:
        proc = subprocess.run(
            ["icacls", str(path), "/inheritance:e", "/grant:r", f"{account}:F"],
            capture_output=True, timeout=20,
        )
        success, detail = proc.returncode == 0, ""
    except Exception as exc:
        success, detail = False, f"（{type(exc).__name__}）"
    if not success and notify:
        notify(f".env ACL 回滚失败{detail}，请手动检查文件权限。")
    return success


def harden_env_permissions(path: Path, notify=None) -> str:
    """收紧 .env 权限，返回 ``ok`` / ``skipped`` / ``failed``。

    Windows 策略（修复「接入大模型 / 一键安装可能因权限失败」）：
    1. 先 ``/grant:r`` 给当前账户完全控制，**再** ``/inheritance:r`` 移除继承；
       任一步失败都只告警并停止，绝不留下「无继承且无授权」的中间态。
    2. 每步之后回读自检；不可读即回滚 ACL 并告警。
    3. 账户无法确定、icacls 不可用 / 超时 / 非 0 均不阻断安装。
    Unix 仍为 chmod 600 + 回读自检。``notify`` 供 auth_tool 复用同一实现时
    保持自己的输出格式；默认使用 install.warn。
    """
    emit = notify or warn
    if not path.is_file():
        return "skipped"
    if os.name != "nt":
        try:
            os.chmod(path, 0o600)
        except OSError:
            emit(".env 权限未收紧（chmod 失败），请手动检查文件权限。")
            return "failed"
        if not _env_readable(path):
            emit(".env 权限未收紧（收紧后不可读），请手动检查文件权限。")
            return "failed"
        return "ok"

    account = _windows_account()
    if not account:
        emit(".env 权限未收紧：无法确定当前账户，请手动检查文件 ACL。")
        return "skipped"
    try:
        grant = subprocess.run(
            ["icacls", str(path), "/grant:r", f"{account}:F"],
            capture_output=True, timeout=20,
        )
        if grant.returncode != 0:
            if not _env_readable(path):
                _restore_env_acl(path, account, emit)
                emit(".env 权限未收紧（icacls 授权失败且回读失败），请手动检查 ACL。")
            else:
                emit(".env 权限未收紧（icacls 授权失败），请手动检查 ACL。")
            return "failed"
        if not _env_readable(path):
            tail = "已回滚 ACL" if _restore_env_acl(path, account, emit) else \
                   "ACL 回滚失败，请手动检查"
            emit(f".env 权限未收紧（授权后回读失败，{tail}），请手动检查 ACL。")
            return "failed"
        drop = subprocess.run(
            ["icacls", str(path), "/inheritance:r"],
            capture_output=True, timeout=20,
        )
        if drop.returncode != 0:
            if not _env_readable(path):
                _restore_env_acl(path, account, emit)
                emit(".env 权限未收紧（icacls 移除继承失败且回读失败），请手动检查 ACL。")
            else:
                emit(".env 权限未收紧（icacls 移除继承失败），请手动检查 ACL。")
            return "failed"
        if not _env_readable(path):
            tail = "已回滚 ACL" if _restore_env_acl(path, account, emit) else \
                   "ACL 回滚失败，请手动检查"
            emit(f".env 权限未收紧（移除继承后回读失败，{tail}），请手动检查 ACL。")
            return "failed"
        return "ok"
    except Exception as exc:
        _restore_env_acl(path, account, emit)
        emit(f".env 权限未收紧（icacls 调用失败：{type(exc).__name__}），请手动检查 ACL。")
        return "failed"


def prompt(text: str, default: str = "") -> str:
    if not sys.stdin.isatty():  # 非交互环境（CI / 管道）直接用默认值
        return default
    try:
        value = input(text).strip()
    except EOFError:
        return default
    return value or default


def ask_yes_no(question: str) -> bool:
    if not sys.stdin.isatty():
        return False
    while True:
        try:
            value = input(f"{question} [y/N]：").strip().lower()
        except EOFError:
            return False
        if value in ("", "n", "no", "否"):
            return False
        if value in ("y", "yes", "是"):
            return True
        print("      请输入 y（接入）或 n（不接入），直接回车表示不接入。")


def _base_url_is_safe(url: str) -> bool:
    """接口地址能否安全携带 API Key：https 任意主机，或本机回环的 http。

    与运行时 gcs/llm.py::_secure_base_url 保持同一口径——安装期不得比运行期更严，
    否则本地代理 / 自建推理服务（如 http://127.0.0.1:11434/v1）在安装期会被误拒。
    """
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme == "https" and parsed.hostname:
        return True
    if parsed.scheme == "http" and (parsed.hostname or "") in ("127.0.0.1", "localhost", "::1"):
        return True
    return False


def collect_llm_config() -> dict:
    base_d = os.environ.get("GCS_LLM_BASE_URL") or DEFAULT_BASE
    model_d = os.environ.get("GCS_LLM_MODEL") or DEFAULT_MODEL
    key_d = os.environ.get("GCS_LLM_API_KEY") or ""

    print("  请填写接入信息（回车使用方括号内的默认值）：")
    base = prompt(f"    接口地址 [{base_d}]：", base_d)
    model = prompt(f"    模型名称 [{model_d}]：", model_d)
    if key_d:
        print("    检测到环境变量中的 Key（出于安全不回显任何字符）")
        if sys.stdin.isatty():
            try:
                key = getpass.getpass("    回车沿用现有 Key，或输入新 Key（不回显）：").strip() or key_d
            except Exception:  # best-effort：无 TTY 时 getpass 会失败，沿用环境变量里的 Key
                _LOG.debug("getpass 读取失败，沿用环境变量中的 Key", exc_info=True)
                key = key_d
        else:
            key = key_d
    elif not sys.stdin.isatty():
        raise InstallError(
            "非交互环境下缺少 API Key",
            "请先设置环境变量 GCS_LLM_API_KEY 再运行 --llm，或改用 --no-llm 离线安装。",
        )
    else:
        try:
            key = getpass.getpass("    API Key（输入不回显）：").strip()
        except Exception as exc:
            raise InstallError(
                "无法安全读取 API Key",
                f"当前终端不支持隐藏输入（{type(exc).__name__}）；"
                "请设置环境变量 GCS_LLM_API_KEY 后重试，避免 Key 明文回显。",
            ) from exc

    if not _base_url_is_safe(base):
        raise InstallError(
            "接口地址必须是 HTTPS（本机回环 http 除外）",
            "只允许 https:// 开头的地址，或本机回环的 http://127.0.0.1 / http://localhost，"
            f"避免 API Key 以明文在网络上传输；当前：{base!r}",
        )
    if not key:
        raise InstallError("API Key 不能为空", "请重新运行脚本并填写，或选择不接入大模型。")
    if not model:
        raise InstallError("模型名称不能为空", "例如 deepseek-chat / qwen-plus / glm-4-flash。")
    return {"GCS_LLM_BASE_URL": base.rstrip("/"),
            "GCS_LLM_API_KEY": key,
            "GCS_LLM_MODEL": model}


def probe_llm_mode(root: Path, py: Path, env: dict) -> str:
    """只构造 ModelGateway 检查配置是否被项目读到，不发起任何网络请求。"""
    code = ("from gcs.llm import ModelGateway; "
            "g = ModelGateway(); print(g.mode + '|' + g.model)")
    p = run([str(py), "-c", code], cwd=root, env=env)
    out = ((p.stdout or "") + (p.stderr or "")).strip()
    if p.returncode != 0:
        raise InstallError("模型配置校验失败", out)
    return out


def merged_env(root: Path) -> dict:
    """运行入口使用的环境：系统环境变量优先，.env 只补充缺失项。"""
    env = dict(os.environ)
    load_dotenv(root / ".env", env)
    return env


def configure_llm(root: Path, py: Path, args) -> str:
    env_path = root / ".env"
    want = args.llm or (not args.no_llm and ask_yes_no(
        "\n是否接入大模型（OpenAI 兼容接口，如 DeepSeek / GLM / Qwen）？\n"
        "  接入：模型负责归因叙述与语义 Judge；不接入：使用离线确定性模式，"
        "全流程可跑通且指标数值不变。\n  请选择"))

    if not want:
        if env_path.is_file():
            skip(".env 已存在，保持原有配置不变")
            warn("选择不接入不会删除已有 .env，运行入口仍会加载其中的模型配置；"
                 "如需彻底离线，请手动移除或清空该文件。")
        else:
            skip("未接入大模型，不创建 .env（离线确定性模式）")
        state = probe_llm_mode(root, py, offline_env())
        if "offline-mock" not in state:
            warn(f"系统环境变量中已有模型配置，运行入口可能以在线模式工作：{state}")
        ok(f"离线模式校验通过：{state}")
        return "未接入（离线确定性模式）"

    # 选择接入：已有 .env 绝不覆盖
    if env_path.is_file():
        skip(f".env 已存在，为保护现有配置不做覆盖：{env_path}")
        info("如需修改，请直接编辑 .env；run.bat / run.sh 会在下次运行时自动加载。")
        state = probe_llm_mode(root, py, merged_env(root))
        if not state.startswith("online"):
            warn(f"已有 .env 当前未使项目进入在线模式（{state}），请检查文件内容。")
        return f"沿用已有配置（{state}）"

    cfg = collect_llm_config()
    try:
        write_llm_env(env_path, cfg)
    except OSError as exc:
        raise InstallError(f"写入 .env 失败：{exc}", "请确认目录可写后重试。") from exc

    env = merged_env(root)
    missing = [k for k in LLM_KEYS if not env.get(k)]
    if missing:
        raise InstallError(f".env 回读校验失败，缺少：{', '.join(missing)}",
                           "请检查 .env 内容后重新运行。")
    state = probe_llm_mode(root, py, env)
    if not state.startswith("online"):
        raise InstallError("配置未生效：项目仍处于离线模式",
                           f"ModelGateway 状态：{state}；请检查 .env 键值。")
    ok(f"模型接入配置完成并校验通过：{state}")
    return f"已接入（{state}）"


def ensure_script_in_project(root: Path) -> None:
    """自动 clone / 手动解压源码时，项目目录可能没有 install.py，而运行入口依赖它。"""
    target = root / "install.py"
    if target.is_file():
        skip("项目内已存在 install.py，保持原样")
        return
    try:
        shutil.copy2(Path(__file__).resolve(), target)
        ok("已将 install.py 放入项目目录（运行入口依赖它）")
    except OSError as exc:
        raise InstallError("无法把 install.py 复制到项目目录",
                           f"{exc}；请手动复制后重试。") from exc


def generate_launchers(root: Path) -> None:
    bat, sh = root / "run.bat", root / "run.sh"
    if bat.exists():
        skip("run.bat 已存在，不覆盖")
    else:
        # newline=""：内容里已经是 CRLF，避免 Python 再按 os.linesep 翻译成 \r\r\n
        bat.write_text(RUN_BAT.replace("\n", "\r\n"), encoding="utf-8", newline="")
        ok("已生成 Windows 运行入口 run.bat")
    if sh.exists():
        skip("run.sh 已存在，不覆盖")
    else:
        # newline="\n"：run.sh 必须在 POSIX 上保持 LF，否则 shebang 会带 \r
        sh.write_text(RUN_SH, encoding="utf-8", newline="\n")
        try:
            os.chmod(sh, 0o755)
        except OSError:  # best-effort：Windows / FAT 卷不支持 POSIX 权限位，忽略即可
            _LOG.debug("run.sh chmod 0755 失败（Windows 下无意义）", exc_info=True)
        ok("已生成 macOS/Linux 运行入口 run.sh")


# ---------------------------------------------------------------- 运行入口
def resolve_run_cmd(rest: list[str]) -> list[str]:
    if not rest:
        return list(ALIASES["demo"])
    example = "示例：python install.py --run -- python -m gcs.demo --policy fcfs"
    # argparse 不同版本可能保留或丢弃 "--"，两种都兼容
    if rest[0] == "--":
        rest = rest[1:]
        if rest and rest[0] in ("python", "python3", "py"):
            rest = rest[1:]
        if not rest:
            raise InstallError("自定义命令为空", example)
        return rest
    if rest[0] in ("python", "python3", "py"):
        cmd = rest[1:]
        if not cmd:
            raise InstallError("自定义命令为空", example)
        return cmd
    if rest[0].startswith("-"):  # 例如直接以 -m 开头的自定义命令
        return rest
    key = rest[0].lower()
    if key not in ALIASES:
        raise InstallError(f"未知运行命令：{key}",
                           "可用：demo / server / test / eval，或用 -- 传入自定义命令。")
    return list(ALIASES[key]) + rest[1:]


def run_mode(args) -> int:
    root = find_project_root()
    if not root:
        raise InstallError(
            "当前目录不是项目根目录，无法运行",
            "请先运行 python install.py 完成安装，再在项目目录内使用 --run。",
        )
    py = venv_python(root)
    if not py.is_file():
        raise InstallError("尚未安装（未找到 .venv）", "请先运行 python install.py。")
    env = merged_env(root)  # 系统变量优先，.env 补充
    cmd = resolve_run_cmd(args.rest)
    info(f"运行：{' '.join(cmd)}")
    proc = subprocess.run([str(py)] + cmd, cwd=str(root), env=env)
    return proc.returncode


# ---------------------------------------------------------------- 主流程
def parse_args(argv):
    ap = argparse.ArgumentParser(
        description="绿算调度 GreenCompute-Scheduler 一键安装脚本（仅标准库）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--no-llm", action="store_true", help="跳过交互，按离线模式安装")
    ap.add_argument("--llm", action="store_true", help="进入大模型接入配置")
    ap.add_argument("--check", action="store_true", help="仅做环境预检，不做任何修改")
    ap.add_argument("--run", action="store_true", help="安装完成后的运行入口（加载 .env + 虚拟环境）")
    ap.add_argument("--dir", default=None, help="指定项目目录 / 自动 clone 的目标目录")
    ap.add_argument("rest", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if args.no_llm and args.llm:
        ap.error("--no-llm 与 --llm 不能同时使用")
    if args.run and (args.no_llm or args.llm or args.check or args.dir):
        ap.error("--run 不能与 --no-llm / --llm / --check / --dir 同时使用")
    return args


def main(argv=None) -> int:
    setup_console()
    args = parse_args(argv)
    try:
        check_python()
        if args.run:
            return run_mode(args)

        if args.check:  # 预检只读，绝不 clone / 写入
            if not is_valid_project(CWD):
                missing = [rel for rel in REQUIRED if not (CWD / rel).is_file()]
                raise InstallError(
                    f"--check 必须在项目目录内运行（当前目录 {CWD} 缺少：{', '.join(missing)}）",
                    "请先 cd 到包含 install.py 的项目根目录后再执行；"
                    f"也可直接运行完整安装：python install.py。",
                )
            ok(f"项目目录：{CWD}")
            # 依赖校验放在结构预检之前：全新机器上缺 PyYAML 是最常见的情况，
            # 应当先给出「pip install -r requirements.txt」这条可操作指引，
            # 而不是让它表现为后面的知识包/结构类错误。
            check_runtime_deps()
            precheck(CWD)
            ok("预检通过：未做任何修改。去掉 --check 即可开始安装。")
            return 0

        root, cloned = ensure_project(args.dir)
        ok(f"项目目录：{root}" + ("（本次自动获取）" if cloned else ""))
        precheck(root)

        py = ensure_venv(root)
        version = install_deps(root, py)
        tests = verify_install(root, py)
        ensure_dirs(root)
        llm_state = configure_llm(root, py, args)
        ensure_script_in_project(root)
        generate_launchers(root)

        print("\n" + "=" * 62)
        ok("安装完成，可以直接使用了")
        print(f"  项目目录   ：{root}")
        print(f"  虚拟环境   ：{root / '.venv'}")
        print(f"  依赖       ：PyYAML {version}")
        print(f"  单元测试   ：{tests} 个用例全部通过")
        print(f"  编排演示   ：通过（离线模式，未消耗模型额度）")
        print(f"  大模型接入 ：{llm_state}")
        print("  运行方式   ：run.bat demo      （Windows，跑完整演示）")
        print("               run.bat server    （Windows，启动可视化操作台）")
        print("               run.bat test      （Windows，跑冒烟测试）")
        print("               ./run.sh demo     （macOS / Linux）")
        print("               python install.py --run eval  （对照实验）")
        print("=" * 62)
        info("脚本可重复运行：已存在的 .venv / .env / 启动器不会被覆盖。")
        return 0
    except InstallError as exc:
        print(f"\n[ERROR] {exc.message}")
        if exc.hint:
            print(f"        建议：{exc.hint}")
        print("        修复后可重新运行本脚本，已完成的步骤会自动跳过。")
        return 1
    except KeyboardInterrupt:
        print("\n[INFO] 已取消。脚本可重复运行，已完成的步骤不会重做。")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
