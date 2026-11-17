"""零依赖可视化操作台：只用 Python 标准库起一个 HTTP 服务。

为什么自己写而不用 Flask/FastAPI：
1. 参赛要求「一条命令跑起来」，评委机器上不保证有第三方框架；
2. 本机零安装依赖 = 现场演示不会因为 pip 失败而开天窗；
3. 前端用原生 JS + CSS，避免 CDN 断网导致白屏（离线评审场景很常见）。

四区块（对应演示动线）：
    ① 任务发起：填作业数/种子/策略组 → 触发一次真实编排
    ② DAG 拓扑：八个状态机与节点状态，返工边高亮
    ③ 运行轨迹：逐条事件，含角色、节点、缺陷归因
    ④ 评测看板：六组对照实验结果与相对提升

启动：
    python app/server.py            # 默认 http://127.0.0.1:8000
    python app/server.py --port 8080
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import socket
import sys
import threading
import webbrowser
from datetime import datetime
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gcs import __version__  # noqa: E402
from gcs.demo import POLICIES  # noqa: E402
from gcs.knowledge import KnowledgeError, load_knowledge  # noqa: E402
from gcs.llm import ModelGateway  # noqa: E402
from gcs.orchestrator import Orchestrator  # noqa: E402
from gcs.report import render_console  # noqa: E402
from html import escape  # noqa: E402
from gcs.metrics_meta import DEFINITION, LABEL, NOTE, TERM  # noqa: E402
from gcs.webassets import DAG_JS
from gcs.simulator import make_jobs  # noqa: E402
from app.auth import AuthConfig, WWW_AUTHENTICATE  # noqa: E402

_LOG = logging.getLogger(__name__)

try:  # P3-1：知识包缺失/非法时给中文提示，不抛裸 traceback
    PACK = load_knowledge(ROOT / "knowledge" / "ningxia_cluster.yaml")
except KnowledgeError as exc:
    sys.stderr.write(f"[FATAL] 知识包加载失败：{exc}\n"
                     "[FATAL] 请确认 knowledge/ningxia_cluster.yaml 存在且为合法 YAML。\n")
    raise SystemExit(2) from exc


def _js_json(value) -> str:
    """内嵌 <script> 的指标字典：ensure_ascii 后无任何 HTML 元字符，另防 </script> 闭合。"""
    return json.dumps(value, ensure_ascii=True).replace("<", "\\u003c")


_PLACEHOLDER_RE = re.compile(
    r"\{(ver|dag_js|metric_terms|metric_labels|metric_defs|metric_notes|jobs_min|jobs_max)\}")


def render_page() -> bytes:
    """组装操作台页面：单遍替换所有占位符，插入的值不再被后续替换扫描（避免二次替换注入）。"""
    mapping = {
        "ver": escape(PACK.version, quote=True),
        "dag_js": DAG_JS,
        "metric_terms": _js_json(TERM),
        "metric_labels": _js_json(LABEL),
        "metric_defs": _js_json(DEFINITION),
        "metric_notes": _js_json(NOTE),
        "jobs_min": str(MIN_JOBS),
        "jobs_max": str(MAX_JOBS),
    }
    return _PLACEHOLDER_RE.sub(lambda m: mapping[m.group(1)], PAGE).encode("utf-8")


RUNS_DIR = ROOT / "runs"
# 模型在线预检只做一次（首个 /api/run 时触发）：鉴权无效则全程降级，避免每个请求都失败重试
_LLM_PROBE_DONE = False
_LLM_PROBE_OK = True
MAX_BODY = 1024 * 1024          # P1-2.1：请求体上限 1 MiB，超限返回 413

# 作业规模上下限：与 gcs/demo.py::MIN_JOBS/MAX_JOBS 同源。
# HTTP 侧不 import gcs.demo 的常量（保持服务最小依赖面，可独立启动）；
# 若口径改动，此处与 gcs/demo.py 需同步修改。
MIN_JOBS = 1
MAX_JOBS = 300
BODY_READ_TIMEOUT = 10           # 正文读取超时（秒）：短于连接级 60s，避免半开连接长期占线程
AUTH = AuthConfig(root=ROOT)    # 进程启动时读取一次鉴权配置，无热加载

# 内存中的最近若干次运行，供看板取用
_STORE: dict[str, dict] = {}
_LOCK = threading.Lock()

# 运行编号白名单：只允许 run_YYYYmmdd_HHMMSS_<策略键> 的形式。
# 这既是入参校验，也顺手堵掉了 /console/../../x 这类路径穿越。
RUN_ID_RE = re.compile(r"^run_\d{8}_\d{6}_[A-Za-z0-9_-]+$")


def safe_run_id(rid: str) -> str | None:
    """合法则原样返回，非法返回 None（调用方按 400 处理）。"""
    return rid if RUN_ID_RE.match(rid) else None


def _as_int(value, name: str) -> int:
    """把请求里的数值参数转成 int；非整数值（1.5、True、"abc"）一律报错。"""
    if isinstance(value, bool):
        raise ValueError(f"{name} 必须是整数")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            raise ValueError(f"{name} 必须是整数") from None
    raise ValueError(f"{name} 必须是整数")


def _write_atomic(path: Path, text: str) -> None:
    """原子写：写临时文件后 os.replace，避免读方看到半成品内容。

    Windows 上目标文件正被占用时 os.replace 可能抛 PermissionError，
    此时退化为直接写（并记日志），不因并发读而让整次运行失败。
    """
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    try:
        os.replace(tmp, path)
    except OSError:
        _LOG.debug("os.replace 失败，退化为直接写 %s", path, exc_info=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def run_once(jobs_n: int, seed: int, policy_key: str) -> dict:
    """一键生一个可回放运行：先跑通编排，再渲染单文件看板并落入内存缓存。"""
    global _LLM_PROBE_DONE, _LLM_PROBE_OK
    jobs = make_jobs(PACK, n=jobs_n, seed=seed)
    llm = ModelGateway(token_budget=int(PACK.rework_budget["max_tokens"]))
    # 启动预检只做一次：鉴权无效时提前降级并明示，避免每个请求都失败重试
    if llm.online and not _LLM_PROBE_DONE:
        _LLM_PROBE_DONE = True
        _LLM_PROBE_OK, reason = llm.probe()
        if not _LLM_PROBE_OK:
            print(f"[WARN] {reason}", file=sys.stderr)
            print("[WARN] 已切换为离线确定性模式：指标数值不变，"
                  "归因叙述与 rubric 判定改由模板生成。", file=sys.stderr)
    if llm.online and not _LLM_PROBE_OK:
        llm.online = False
        llm.mode = "offline-mock(precheck-failed)"
    art = Orchestrator(PACK, llm=llm, policy=POLICIES[policy_key]).run(jobs)

    run_id = datetime.now().strftime("run_%Y%m%d_%H%M%S_%f")
    out_dir = RUNS_DIR / f"{run_id}_{policy_key}"
    out_dir.mkdir(parents=True, exist_ok=True)
    render_console(art, PACK.fingerprint(), out_dir / "console.html")
    artifact = art.to_dict()
    # 追加 seed / policy_key：磁盘回读（服务重启后回看历史运行）需要它们，
    # 否则同一条运行的内存路径与磁盘路径字段不一致（sweep-http D7）
    artifact["seed"] = seed
    artifact["policy_key"] = policy_key
    # 原子写：先写 .tmp 再 os.replace，避免中途崩溃留下半截 JSON
    # （半截 JSON 会让 load_run_from_disk 返回 None，损坏与不存在无法区分）
    _write_atomic(out_dir / "artifact.json",
                  json.dumps(artifact, ensure_ascii=False, indent=2))
    _write_atomic(out_dir / "metrics.json",
                  json.dumps(art.metrics.as_dict(), ensure_ascii=False, indent=2))

    payload = {
        "run_id": out_dir.name,
        "policy": art.policy,
        "policy_key": policy_key,
        "seed": seed,
        "jobs": art.metrics.jobs,
        "metrics": art.metrics.as_dict(),
        "rounds": art.rounds,
        "rubric": art.rubric_history[-1].as_dict() if art.rubric_history else None,
        "defects": art.defect_history[-1] if art.defect_history else None,
        "dag": art.dag.to_dict(),
        "events": [
            # P2-3.4：把「每个角色只拿到自己需要的工具」的审计记录并入轨迹，
            # 供操作台「使用工具」列展示；_emit 保证 audit 与 events 一一对应。
            {**e.as_dict(),
             "tool": art.audit[i]["tool"] if i < len(art.audit) else "-"}
            for i, e in enumerate(art.events)
        ],
        "narrative": art.narrative,
        "llm": art.llm,
        "elapsed_s": round(art.elapsed_s, 3),
        "console_url": f"/console/{out_dir.name}",
    }
    with _LOCK:
        _STORE[out_dir.name] = payload
        while len(_STORE) > 200:  # M9：内存运行表上限，淘汰最旧，防长期运行无限增长
            _STORE.pop(next(iter(_STORE)))
    return payload


# 前端 loadBoard 会按 g[metric][0] 取值并渲染成表，缺键会在浏览器里抛 TypeError
# 导致「评测面板静默空白」——这里在服务端先做结构自检，不合规就给出可读提示。
EVAL_REQUIRED_METRICS = ("sla_rate", "budget_rate", "cost_per_gpu_hour",
                          "green_window_ratio", "avg_wait_min", "rubric_score")


def eval_board() -> dict:
    """拿到对照实验汇总（若未跑过或产物结构异常则返回 available=False + 提示）。

    三种降级：文件不存在 / JSON 无法解析 / 结构不满足前端渲染契约，
    一律返回可读中文提示，而不是让前端拿到半成品数据后静默空白。
    """
    p = ROOT / "evaluation" / "summary.json"
    if not p.exists():
        return {"available": False, "message": "尚未运行对照实验，请先执行 python evaluation/run_eval.py"}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"available": False,
                "message": "评测产物无法解析：文件内容不是合法 JSON；"
                           "请重新执行 python evaluation/run_eval.py"}
    if not isinstance(data, dict) or not data:
        return {"available": False,
                "message": "评测产物结构异常（为空或不是对象）；"
                           "请重新执行 python evaluation/run_eval.py"}
    bad: list[str] = []
    for key, grp in data.items():
        if not isinstance(grp, dict) or "name" not in grp:
            bad.append(f"{key} 缺少 name")
            continue
        for metric in EVAL_REQUIRED_METRICS:
            value = grp.get(metric)
            if not (isinstance(value, list) and len(value) == 2):
                bad.append(f"{key}.{metric} 缺失或格式异常")
                break
    if bad:
        return {"available": False,
                "message": "评测产物结构异常：" + "；".join(bad[:3])
                           + "；请重新执行 python evaluation/run_eval.py"}
    return {"available": True, "groups": data}


def list_runs(limit: int = 50) -> list[dict]:
    """P2-3.2：只读列出磁盘上的历史运行，支持重启后回溯。

    刻意不把历史 artifact.json 全量载入 _STORE（避免启动耗时与内存膨胀）；
    实际内容在访问 /api/run/<rid> 时按需从磁盘读取。

    ``limit`` 收窄单次返回条数（P2：该接口为公开只读，仅暴露 run_id 与布尔标记，
    不返回正文；限制条数可进一步压缩历史 run 编号的枚举面）。
    """
    if not RUNS_DIR.is_dir():
        return []
    out: list[dict] = []
    for d in sorted(RUNS_DIR.iterdir(), key=lambda x: x.name, reverse=True):
        if not d.is_dir() or safe_run_id(d.name) is None:
            continue
        out.append({
            "run_id": d.name,
            "has_console": (d / "console.html").is_file(),
            "has_artifact": (d / "artifact.json").is_file(),
            "console_url": f"/console/{d.name}",
        })
    return out[:max(1, limit)]


def limit_from_query(raw_path: str, default: int = 50, hard_max: int = 200) -> int:
    """解析 ``/api/runs?limit=N``；缺省/非法/越界一律收敛到 [1, hard_max]。

    越界不做报错，避免把参数校验细节回显给公开接口的调用方。
    """
    raw = (parse_qs(urlparse(raw_path).query).get("limit") or [""])[0].strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(1, min(hard_max, value))


def load_run_from_disk(rid: str) -> dict | None:
    """把历史 artifact.json 转成与 run_once 返回值同形的 payload。"""
    f = RUNS_DIR / rid / "artifact.json"
    if not f.is_file():
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    rubric = data.get("rubric") or []
    audit = data.get("audit") or []
    # policy_key：优先取 artifact 里的；旧运行没有该字段时，从目录名
    # run_<时间戳>_<policy_key> 的末段还原，并校验确实是已知策略
    policy_key = data.get("policy_key")
    if not policy_key and "_" in rid:
        candidate = rid.rsplit("_", 1)[-1]
        policy_key = candidate if candidate in POLICIES else None
    events = data.get("events") or []
    return {
        "run_id": rid,
        "policy": data.get("policy"),
        "policy_key": policy_key,
        "seed": data.get("seed"),
        "jobs": (data.get("metrics") or {}).get("jobs"),
        "metrics": data.get("metrics") or {},
        "rounds": data.get("rounds"),
        "rubric": rubric[-1] if rubric else None,
        "defects": (data.get("defects") or [None])[-1],
        "dag": data.get("dag"),
        # 与 run_once 同形：events[i] 与 audit[i] 一一对应，补上「使用工具」列
        "events": [
            {**e, "tool": audit[i].get("tool", "-")
             if i < len(audit) and isinstance(audit[i], dict) else "-"}
            for i, e in enumerate(events)
        ],
        "narrative": data.get("narrative") or "",
        "llm": data.get("llm") or {},
        "elapsed_s": data.get("elapsed_s"),
        "console_url": f"/console/{rid}",
    }


PAGE = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" href="data:,">
<title>绿算调度 · 操作台</title><style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui,"Noto Sans CJK SC","PingFang SC","Microsoft YaHei",sans-serif;
     background:#EEF1F6;color:#232A33}
header{background:#1F3352;color:#fff;padding:16px 24px;border-bottom:4px solid #E08A3C;
       display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px}
header h1{font-size:20px;font-weight:700}
header .sub{font-size:12px;color:#AFC2DD}
nav{display:flex;gap:8px;padding:14px 24px 0}
nav button{border:1px solid #D2DAE6;background:#fff;color:#33415A;font-size:13px;
           padding:8px 16px;border-radius:4px 4px 0 0;cursor:pointer;font-family:inherit}
nav button.on{background:#1F3352;color:#fff;border-color:#1F3352}
main{padding:0 24px 40px}
.panel{display:none;padding-top:14px}
.panel.on{display:block}
.card{background:#fff;border:1px solid #E2E7EF;border-radius:6px;padding:16px 18px;margin-bottom:14px}
.card h2{font-size:15px;color:#1F3352;margin-bottom:3px}
.card .hint{font-size:12px;color:#8B96A8;margin-bottom:12px;line-height:1.6}
.row{display:flex;gap:12px;flex-wrap:wrap;align-items:flex-end}
label{display:block;font-size:12px;color:#5A6779;margin-bottom:5px}
input,select{font-family:inherit;font-size:13px;padding:7px 10px;border:1px solid #C9D3E3;
             border-radius:4px;background:#fff;min-width:120px}
button.go{background:#E08A3C;color:#fff;border:none;padding:9px 20px;border-radius:4px;
          font-size:13.5px;cursor:pointer;font-family:inherit}
button.go:disabled{background:#B9C2CF;cursor:not-allowed}
.kpis{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-top:14px}
.kpi{border:1px solid #E2E7EF;border-radius:6px;padding:12px 14px;background:#fff;border-top:3px solid #2F6DB5}
.kpi .l{font-size:11.5px;color:#7A8699}
.kpi .v{font-size:22px;font-weight:700;color:#1F3352;margin-top:4px}
.kpi .s{font-size:11px;color:#8B96A8;margin-top:3px}
.kpi.g{border-top-color:#1F8A70}.kpi.o{border-top-color:#E08A3C}
#dag{position:relative;height:420px}
.node{position:absolute;border-radius:5px;border:1px solid #C9D3E3;background:#F7F9FC;padding:5px 7px;
      font-size:11px;line-height:1.25;overflow:hidden}
.node .id{font-size:9px;color:#8B96A8}
.node.done{background:#EAF3EE;border-color:#1F8A70}
.node.loop{border-style:dashed;background:#FDF3E8;border-color:#E08A3C}
.node .st{position:absolute;top:4px;right:5px;font-size:9px;color:#1F8A70}
.legend{font-size:11px;color:#7A8699;margin-top:8px}
.legend i{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:5px}
.trace{max-height:520px;overflow:auto;font-size:12.5px}
.ev{display:grid;grid-template-columns:42px 1fr 92px;gap:8px;padding:7px 0;border-bottom:1px dashed #EDF0F5}
.ev .sq{font-size:10.5px;color:#A6B0C0}
.ev .st{display:inline-block;font-size:9.5px;padding:1px 6px;border-radius:3px;background:#E8EEF7;color:#2F6DB5;margin-right:6px}
.ev .ag{color:#8B96A8;font-size:10.5px;margin-left:6px}
.ev .dt{color:#3A4553;line-height:1.45;margin-top:2px}
.ev .tool{font-size:10px;color:#2F6DB5;text-align:right;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.auth{display:flex;gap:6px;align-items:center}
.auth label{margin:0;color:#AFC2DD;font-size:11px}
.auth input{min-width:170px;padding:5px 8px;font-size:12px;background:#fff}
.auth .sub{font-size:11px;white-space:nowrap}
table{width:100%;border-collapse:collapse;font-size:12px}
th{background:#1F3352;color:#fff;padding:8px;text-align:left;font-size:11.5px}
td{padding:7px 8px;border-bottom:1px solid #EDF0F5;color:#3A4553}
tr:nth-child(even) td{background:#F8FAFD}
.track{height:8px;background:#EDF0F5;border-radius:4px;overflow:hidden;min-width:120px}
.fill{height:100%;background:linear-gradient(90deg,#2F6DB5,#1F8A70);border-radius:4px}
.muted{color:#8B96A8;font-size:12.5px;padding:8px 0}
.err{background:#FBECEC;border:1px solid #E8B7B3;color:#8C2F26;padding:10px 12px;border-radius:4px;font-size:13px}
</style></head><body>
<header><div><h1>绿算调度 · 多智能体编排操作台</h1>
<div class="sub">2026 第五届“未来杯”创新大赛 · 赛道一 Agent 编排创新应用 · 知识包 """ + "{ver}" + """</div></div>
<div class="auth"><label for="tok">访问凭据</label>
<input id="tok" type="password" placeholder="对外演示时填写" autocomplete="off">
<span id="authstate" class="sub">本机运行 · 确定性可复现</span></div></header>
<nav>
  <button class="on" data-p="p1">① 任务发起</button>
  <button data-p="p2">② DAG 拓扑</button>
  <button data-p="p3">③ 运行轨迹</button>
  <button data-p="p4">④ 评测看板</button>
</nav>
<main>
<section class="panel on" id="p1">
  <div class="card"><h2>发起一次调度任务</h2>
    <div class="hint">选择策略组与作业规模，服务端会真实跑一遍多智能体编排（画像→调度→执行→五层校验→自省→定向返工），并把结果写进 runs/ 目录。</div>
    <div class="row">
      <div><label>策略组</label><select id="policy"></select></div>
      <div><label>作业数量</label><input id="jobs" type="number" value="60" min="{jobs_min}" max="{jobs_max}"></div>
      <div><label>随机种子</label><input id="seed" type="number" value="20261111"></div>
      <div><button class="go" id="run">开始编排</button></div>
    </div>
    <div id="msg" style="margin-top:12px"></div>
    <div class="kpis" id="kpis"></div>
    <div id="narr" class="muted"></div>
    <div id="hist" class="hint" style="margin-top:8px"></div>
  </div>
</section>
<section class="panel" id="p2">
  <div class="card"><h2>DAG 拓扑与节点状态</h2>
    <div class="hint">实线为依赖边，虚线为返工边；返工边只在验证不通过时激活，实现「只退责任节点、不整链重跑」。</div>
    <div id="dag"></div>
    <div class="legend"><span><i style="background:#1F8A70"></i>已完成</span>
      <span><i style="background:#E08A3C"></i>返工节点</span>
      <span><i style="background:#C9D3E3"></i>依赖边</span></div>
  </div>
</section>
<section class="panel" id="p3">
  <div class="card"><h2>运行轨迹（逐条事件）</h2>
    <div class="hint">每个事件带状态、角色、节点与细节；缺陷会给出责任角色与定向返工指令；最右列是该事件实际使用的工具（角色工具白名单审计）。</div>
    <div class="trace" id="trace"></div>
  </div>
</section>
<section class="panel" id="p4">
  <div class="card"><h2>六组对照实验看板</h2>
    <div class="hint">同一模拟器数据、同一知识包、同一评测脚本，3 个种子重复 3 次，报均值±标准差。</div>
    <div id="board"></div>
  </div>
</section>
</main>
<script>
const $=s=>document.querySelector(s);
const MT={metric_terms};
const ML={metric_labels};
const MD={metric_defs};
const MN={metric_notes};
// esc() 由共享前端资源 DAG_JS 提供（函数声明提升，可在任意位置调用）。
let lastDag=null;
// 静态 Token 只存当前标签页的 sessionStorage：不进 URL、不进 localStorage、
// 不进日志，关闭标签即失效；由 authFetch 统一注入 Authorization 头。
function getToken(){return sessionStorage.getItem('gcs_auth_token')||'';}
function authFetch(url,opts){
  opts=opts||{};
  const h=Object.assign({},opts.headers||{});
  if(getToken()) h['Authorization']='Bearer '+getToken();
  return fetch(url,Object.assign({},opts,{headers:h}));
}
$('#tok').value=getToken();
$('#authstate').textContent=getToken()?'凭据已设置':'本机运行 · 确定性可复现';
$('#tok').addEventListener('change',()=>{
  sessionStorage.setItem('gcs_auth_token',$('#tok').value.trim());
  $('#authstate').textContent=getToken()?'凭据已设置':'本机运行 · 确定性可复现';
});
function goPanel(pid){
  document.querySelectorAll('nav button').forEach(x=>x.classList.toggle('on',x.dataset.p===pid));
  document.querySelectorAll('.panel').forEach(p=>p.classList.toggle('on',p.id===pid));
  if(pid==='p2'&&lastDag) requestAnimationFrame(()=>drawDag(lastDag));
  if(pid==='p4') loadBoard();
}
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>goPanel(b.dataset.p));
window.addEventListener('resize',()=>{
  if(lastDag&&$('#p2').classList.contains('on')) drawDag(lastDag);
});
authFetch('/api/policies').then(r=>r.json()).then(d=>{
  const sel=$('#policy');
  d.policies.forEach(p=>{const o=document.createElement('option');o.value=p.key;o.textContent=p.name;sel.appendChild(o);});
});
{dag_js}
function renderRun(d){
  const m=d.metrics;
  const k=[[MT.sla_rate,(m.sla_rate*100).toFixed(1)+'%',MN.sla_rate,''],
    [MT.cost_per_gpu_hour,'¥'+m.cost_per_gpu_hour.toFixed(2),MN.cost_per_gpu_hour,'o'],
    [MT.avg_green_share,(m.avg_green_share*100).toFixed(1)+'%',MN.avg_green_share,'g'],
    [MT.avg_wait_min,m.avg_wait_min.toFixed(0)+' min','排队 P95 '+m.p95_wait_min.toFixed(0)+' min（不含执行）',''],
    [MT.rounds,String(d.rounds),'rubric '+(d.rubric?d.rubric.score:'-')+' / '+(d.rubric&&d.rubric.threshold!=null?d.rubric.threshold:'-'),'o']];
  $('#kpis').innerHTML=k.map(x=>`<div class="kpi ${esc(x[3])}"><div class="l">${esc(x[0])}</div><div class="v">${esc(x[1])}</div><div class="s">${esc(x[2])}</div></div>`).join('');
  $('#narr').textContent='运行摘要：'+d.narrative;
  drawDag(d.dag);
  $('#trace').innerHTML=d.events.map(e=>`<div class="ev"><div class="sq">#${esc(String(e.seq).padStart(2,'0'))}</div><div>`+
    `<span class="st">${esc(e.state)}</span><span class="ag">${esc(e.agent)} · ${esc(e.node)}</span><div class="dt">${esc(e.detail)}</div></div>`+
    `<div class="tool" title="使用工具">${esc(e.tool||'-')}</div></div>`).join('');
}
$('#run').onclick=async()=>{
  $('#run').disabled=true;$('#msg').innerHTML='<div class="muted">编排执行中，请稍候……</div>';
  try{
    const r=await authFetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({jobs:parseInt($('#jobs').value,10),seed:parseInt($('#seed').value,10),policy:$('#policy').value})});
    const d=await r.json();
    if(r.status===401)throw new Error('未授权：请在页首「访问凭据」填入 Token 后重试');
    if(!r.ok)throw new Error(d.error||'运行失败');
    $('#msg').innerHTML=`<div class="muted">已完成：${esc(d.run_id)}（耗时 ${esc(d.elapsed_s)}s，模型 ${esc(d.llm.mode)}，token ${esc(d.llm.tokens_total)}）`+
      ` <button id="openconsole" class="go" style="padding:4px 10px;margin-left:8px">打开单文件看板</button></div>`;
    $('#openconsole').onclick=async()=>{
      try{
        // /console/<rid> 是浏览器导航，无法带自定义头；改为取回单文件 HTML
        // 后写入新窗口，保持「零改动看板 + 鉴权头可用」。
        const rr=await authFetch(d.console_url);
        if(rr.status===401)throw new Error('未授权：请先填写访问凭据');
        if(!rr.ok)throw new Error('看板读取失败');
        const html=await rr.text();
        const w=window.open('','_blank');
        if(!w)throw new Error('浏览器拦截了新窗口，请允许弹出窗口');
        w.document.open();w.document.write(html);w.document.close();
      }catch(e){$('#msg').innerHTML=`<div class="err">看板打开失败：${esc(e.message)}</div>`;}
    };
    renderRun(d);
    loadHistory();
  }catch(e){$('#msg').innerHTML=`<div class="err">运行失败：${esc(e.message)}</div>`;}
  $('#run').disabled=false;
};
async function loadBoard(){
  let d;
  try{d=await (await authFetch('/api/eval')).json();}
  catch(e){$('#board').innerHTML=`<div class="muted">评测看板加载失败：${esc(String(e))}</div>`;return;}
  if(!d.available){$('#board').innerHTML=`<div class="muted">${esc(d.message)}</div>`;return;}
  if(!d.groups||!Object.keys(d.groups).length){$('#board').innerHTML=`<div class="muted">评测产物为空，请重新执行 python evaluation/run_eval.py</div>`;return;}
  const metrics=[['sla_rate',ML.sla_rate,1],['budget_rate',ML.budget_rate,1],
    ['cost_per_gpu_hour',ML.cost_per_gpu_hour,0],['green_window_ratio',ML.green_window_ratio,1],
    ['avg_wait_min',ML.avg_wait_min,0],['rubric_score',ML.rubric_score,0]];
  let h='';
  metrics.forEach(m=>{
    const mx=Math.max(...Object.values(d.groups).map(g=>g[m[0]][0]))||1;
    h+=`<h3 style="font-size:13.5px;color:#1F3352;margin:14px 0 6px" title="${esc(MD[m[0]])}">${esc(m[1])}</h3><table><thead><tr><th>组</th><th>配置</th><th>对比</th><th>值</th><th>波动</th></tr></thead><tbody>`;
    Object.entries(d.groups).forEach(([k,g])=>{
      const v=g[m[0]][0],sd=g[m[0]][1];
      h+=`<tr><td><b>${esc(k)}</b></td><td>${esc(g.name)}</td><td><div class="track"><div class="fill" style="width:${esc((v/mx*100).toFixed(1))}%"></div></div></td>`+
        `<td>${esc(m[2]?(v*100).toFixed(1)+'%':v.toFixed(2))}</td><td>±${esc(m[2]?(sd*100).toFixed(1)+'%':sd.toFixed(2))}</td></tr>`;
    });
    h+='</tbody></table>';
  });
  $('#board').innerHTML=h;
}
// P2-3.2：重启后仍可回溯 runs/ 下已归档的运行（服务端按需从磁盘读取）
async function loadHistory(){
  try{
    const d=await (await authFetch('/api/runs')).json();
    const runs=(d.runs||[]).slice(0,5);
    if(!runs.length){$('#hist').textContent='历史运行：暂无归档记录';return;}
    $('#hist').innerHTML='历史运行（最近 5 次，点击回看轨迹）： '+
      runs.map(r=>`<a href="#" data-rid="${esc(r.run_id)}" style="margin-right:10px">${esc(r.run_id)}</a>`).join('');
    $('#hist').querySelectorAll('a').forEach(a=>a.onclick=async(ev)=>{
      ev.preventDefault();
      try{
        const rr=await authFetch('/api/run/'+a.dataset.rid);
        const dd=await rr.json();
        if(!rr.ok)throw new Error(dd.error||'读取失败');
        renderRun(dd);goPanel('p3');
      }catch(e){$('#msg').innerHTML=`<div class="err">历史读取失败：${esc(e.message)}</div>`;}
    });
  }catch(e){$('#hist').textContent='历史运行列表不可用';}
}
loadHistory();
// 深链接：?panel=2&autorun=1 可直接落到某个面板并自动跑一次，方便录屏与截图
const QS=new URLSearchParams(location.search);
if(QS.get('panel')) goPanel('p'+QS.get('panel'));
if(QS.get('autorun')==='1'){
  if(QS.get('jobs')) $('#jobs').value=QS.get('jobs');
  if(QS.get('seed')) $('#seed').value=QS.get('seed');
  if(QS.get('policy')) $('#policy').value=QS.get('policy');
  const iv=setInterval(()=>{
    if($('#policy').options.length>0){clearInterval(iv);$('#run').click();}
  },120);
}
</script></body></html>"""


# 安全响应头统一集合：应用自身响应、stdlib 错误路径、并发上限 503 三处共用，
# 避免出现「一部分错误响应没有安全头」的不一致（sweep-http D9/D10）。
CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
       "img-src data:; connect-src 'self'; base-uri 'none'; form-action 'none'; "
       "frame-ancestors 'none'")


@lru_cache(maxsize=8)
def _host_allowlist(bound: str, extra: str) -> frozenset[str]:
    """Host 白名单（P3-3，防 DNS rebinding）：只比较主机名，忽略端口。

    纳入回环名、`--host` 绑定地址、本机主机名/FQDN 及其解析出的 IP，
    以及 `GCS_ALLOWED_HOSTS`（逗号分隔）显式追加的域名。
    """
    hosts = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
    if bound and bound not in ("0.0.0.0", "::"):
        hosts.add(bound.lower())
    try:
        name = socket.gethostname()
    except OSError:
        name = ""
    for cand in (name, socket.getfqdn() if name else ""):
        if cand:
            hosts.add(cand.lower())
            hosts.add(cand.split(".")[0].lower())
    if name:
        try:
            for info in socket.getaddrinfo(name, None):
                hosts.add(str(info[4][0]).lower())
        except OSError:  # best-effort：解析失败只影响白名单广度，不阻断服务
            _LOG.debug("本机 IP 解析失败，Host 白名单仅含已知条目", exc_info=True)
    for cand in extra.split(","):
        cand = cand.strip().lower()
        if cand:
            hosts.add(cand)
    return frozenset(hosts)


def allowed_hosts() -> frozenset[str]:
    """当前进程的 Host 白名单快照（绑定地址或环境变量变化后自动失效重算）。"""
    return _host_allowlist(Handler.bound_host or "",
                           os.getenv("GCS_ALLOWED_HOSTS") or "")


def host_only(raw: str) -> str:
    """从 Host 头里取出主机名（去掉端口与 IPv6 方括号）。

    Host 白名单与 Origin 同源判定必须用**同一套**解析，否则 IPv6 回环下
    ``[::1]:8000`` 会被解析成 ``[`` 与 ``::1`` 两个不同的值，导致真同源请求
    被误判为跨源（sweep-http D1）。
    """
    raw = (raw or "").strip()
    if raw.startswith("["):
        return raw[1:].split("]")[0]
    return raw.split(":")[0]


# 安全响应头统一集合：应用自身响应、stdlib 错误路径、并发上限 503 三处共用，
# 避免出现「一部分错误响应没有安全头」的不一致（sweep-http D9/D10、R1）。
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Content-Security-Policy": CSP,
}


class Handler(BaseHTTPRequestHandler):
    """HTTP 服务端：前端页面、运行 API、看板静态文件全部走这里。

    只允许 run_YYYYmmdd_HHMMSS_<策略键> 这种格式的运行编号穿过，保护
    磁盘路径不被 ../ 之类写入的路径穿越攻击。
    """

    server_version = f"GreenCompute/{__version__}"
    # P3-2：置空 sys_version 并直接返回 server_version，避免默认拼上 "Python/3.13.x"
    sys_version = ""

    def version_string(self) -> str:
        """只回产品名与版本，不回解释器精确版本（P3-2）。"""
        return self.server_version
    # main() 注入的绑定地址，供 Host 白名单使用（测试直接构造 Handler 时为 None）
    bound_host: str | None = None
    timeout = 60  # M9：单连接读超时，防慢速/半开连接长期占用线程
    auth_config = AUTH

    def log_message(self, fmt, *args):
        """受限访问日志：只记时间 / 来源 / 请求行 / 状态码 / 字节数。

        鉴权相关：原实现被 pass 掉，导致越权完全不可观测。默认 fmt 不含
        Authorization 头与请求正文，因此这里不会泄露凭据。
        """
        try:
            msg = fmt % args
        except Exception:
            msg = " ".join(str(a) for a in args)
        sys.stderr.write(
            f"[{self.log_date_time_string()}] {self.address_string()} {msg}\n")

    def _send(self, code: int, body: bytes, ctype: str,
              extra: dict[str, str] | None = None) -> None:
        """回复客户端：先发状态行与头，再发正文，确保 Content-Length 准确。

        注意：状态行/响应头只能发送一次，重复发送会让 Content-Length 与实际
        字节数错位，客户端解析必然出错。extra 用于 401 的 WWW-Authenticate。
        """
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        # RFC 7230 §3.3.2：204 响应不得携带 Content-Length
        if code != 204:
            self.send_header("Content-Length", str(len(body)))
        # P3-6：补齐最小安全响应头（HSTS 仅在 HTTPS 反代下有意义，见 docs/DEPLOY.md）
        for key, value in SECURITY_HEADERS.items():
            self.send_header(key, value)
        # 页面为单文件内联 JS/CSS，故放开 inline；其余一律同源/禁用，阻断外部加载
        # （CSP 已并入 SECURITY_HEADERS，stdlib 错误路径与 503 同样携带）
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def send_error(self, code, message=None, explain=None):
        """stdlib 自带的错误路径（畸形请求行 / 未实现方法）也补齐安全响应头。

        默认实现发 HTML 文本且不带任何安全头；这里统一成 JSON + 与 :meth:`_send`
        相同的头集合，避免「一部分错误响应没有安全头」的不一致（sweep-http D9）。
        """
        try:
            short, _long = self.responses[code]
        except KeyError:
            short = "请求错误"
        body = json.dumps({"error": short}, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(code, short)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            for key, value in SECURITY_HEADERS.items():
                self.send_header(key, value)
            self.end_headers()
            if code != 204 and code != 304:
                self.wfile.write(body)
        except Exception:  # best-effort：连接已断开时不再抛二次异常
            _LOG.debug("send_error 响应发送失败 %s", code, exc_info=True)

    def _json(self, obj, code: int = 200,
              extra: dict[str, str] | None = None) -> None:
        """以 UTF-8 JSON 回复；中文不转义，便于直接读接口。"""
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", extra=extra)

    def _unauthorized(self, reason: str) -> None:
        """统一 401：不区分无凭据 / 错误 / 过期 / 未配置，避免凭据探测。"""
        path = self.path.split("?")[0]
        sys.stderr.write(f"[AUTH] 401 {self.address_string()} {self.command} "
                         f"{path} reason={reason}\n")
        self._json({"error": "未授权：需要有效的访问凭据"}, 401,
                   extra={"WWW-Authenticate": WWW_AUTHENTICATE})

    def _host_ok(self) -> bool:
        """Host 校验（P3-3）：缺失 → 400；不在白名单 → 403。失败时已自行回复。"""
        raw = (self.headers.get("Host") or "").strip()
        if not raw:
            self.close_connection = True  # 拒绝时不读正文，避免残留字节污染后续请求
            self._json({"error": "缺少 Host 请求头"}, 400)
            return False
        # RFC 9110：Host 必须唯一；带 userinfo（user@host）也不是合法 Host
        if len(self.headers.get_all("Host") or []) > 1:
            self.close_connection = True
            self._json({"error": "重复的 Host 请求头"}, 400)
            return False
        if "@" in raw or "/" in raw:
            self.close_connection = True
            self._json({"error": "Host 请求头不合法"}, 400)
            return False
        host = host_only(raw)
        if host.lower() in allowed_hosts():
            return True
        self.close_connection = True
        sys.stderr.write(f"[SEC] 拒绝非白名单 Host：{raw}\n")
        self._json({"error": "Host 不在允许列表内"}, 403)
        return False

    def _guard(self, method: str) -> bool:
        """所有路由的统一入口判定；失败时已自行发出 400/403/401。"""
        if not self._host_ok():
            return False
        path = self.path.split("?")[0]
        result = self.auth_config.check(self.headers, self.client_address,
                                        method, path)
        if result.ok:
            return True
        self._unauthorized(result.reason)
        return False

    def do_GET(self) -> None:  # noqa: N802
        """GET 统一异常边界：未捕获异常返回 500，不泄露内部信息、不打断服务（M9）。"""
        try:
            self._do_GET()
        except Exception as exc:
            sys.stderr.write(
                f"[ERROR] do_GET 未捕获异常 {self.path.split('?')[0]}："
                f"{type(exc).__name__}: {exc}\n")
            try:
                self._json({"error": "服务内部错误，请稍后重试"}, 500)
            except Exception:  # best-effort：失败不阻断主流程（编码/发送兜底）
                _LOG.debug("500 响应发送失败 %s", self.path, exc_info=True)

    def _do_GET(self) -> None:  # noqa: N802
        """GET 路由：首页、策略列表、评测看板、单次运行结果与看板文件。

        所有带运行编号的路由都先过 safe_run_id，非法编号直接 400，
        不合法就根本拼不出路径，这是路径穿越的防线。
        分级鉴权：写接口、运行轨迹与看板文件过 _guard；公开只读 GET 放行。
        """
        if not self._guard("GET"):
            return
        path = self.path.split("?")[0]
        if path == "/favicon.ico":
            # 浏览器默认会请求它；显式回 204 空响应，避免控制台出现无意义的 404
            self._send(204, b"", "image/x-icon")
        elif path in ("/", "/index.html"):
            self._send(200, render_page(), "text/html; charset=utf-8")
        elif path == "/api/policies":
            self._json({"policies": [{"key": k, "name": v.name, "desc": v.description}
                                      for k, v in POLICIES.items()]})
        elif path == "/api/eval":
            self._json(eval_board())
        elif path == "/api/runs":
            # 公开只读；?limit= 可收窄返回条数（默认 50，上限 200）
            self._json({"runs": list_runs(limit_from_query(self.path))})
        elif path.startswith("/api/run/"):
            rid = safe_run_id(path.rsplit("/", 1)[-1])
            if rid is None:
                self._json({"error": "运行编号格式不合法"}, 400)
                return
            with _LOCK:
                data = _STORE.get(rid)
            if not data:
                # 重启后 _STORE 为空，按 P2-3.2 从磁盘按需回读历史产物
                data = load_run_from_disk(rid)
            if not data:
                self._json({"error": "未找到该次运行"}, 404)
            else:
                self._json(data)
        elif path.startswith("/console/"):
            rid = safe_run_id(path.rsplit("/", 1)[-1])
            if rid is None:
                self._json({"error": "运行编号格式不合法"}, 400)
                return
            f = RUNS_DIR / rid / "console.html"
            if not f.exists():
                self._json({"error": "未找到该次运行的看板"}, 404)
            else:
                self._send(200, f.read_bytes(), "text/html; charset=utf-8")
        else:
            self._json({"error": "未找到的路径"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        """POST /api/run：鉴权 + 请求体上限 + 参数校验后跑一次，返回结果 JSON。"""
        if not self._guard("POST"):
            return
        if self.path.split("?")[0] != "/api/run":
            self._json({"error": "未找到的路径"}, 404)
            return
        # H3 加固：防 CSRF。仅接受 application/json（浏览器 no-cors 无法设置该类型），
        # 且 Origin（若存在）必须同源，阻断跨站页面在 loopback 免凭据下触发的写请求。
        ctype_raw = (self.headers.get("Content-Type") or "").strip()
        ctype = ctype_raw.split(";")[0].strip().lower()
        charset = ""
        for part in ctype_raw.split(";")[1:]:
            if part.strip().lower().startswith("charset="):
                charset = part.split("=", 1)[1].strip().strip('"').lower()
        # 请求体按 UTF-8 解码；声明其它字符集一律 415，避免误解码成乱码
        if ctype != "application/json" or (charset and charset not in ("utf-8", "utf8")):
            self._json({"error": "仅接受 application/json（charset 须为 utf-8）请求"}, 415)
            return
        origin = self.headers.get("Origin")
        if origin:
            # 同源判定与 Host 白名单共用 host_only()，否则 IPv6 回环会被误判跨源。
            # 这里只比对主机名、不比 scheme/port：反代终止 TLS 时浏览器 Origin 是
            # https://域名，而本服务看到的是上游 127.0.0.1:8000，比 scheme/port 会
            # 误杀合法请求；跨源 JSON POST 必触发预检，而本服务 OPTIONS 回 501 且
            # 不下发任何 Access-Control-Allow-*，浏览器本身会阻断（纵深防御）。
            req_host = host_only(self.headers.get("Host") or "")
            if urlparse(origin).hostname != req_host:
                self._json({"error": "跨源请求被拒绝"}, 403)
                return
        try:
            try:
                n = int(self.headers.get("Content-Length", 0))
            except (TypeError, ValueError):
                self._json({"error": "Content-Length 不合法"}, 400)
                return
            if n < 0:
                # P2：负值与「超限」是两类错误，分别给 400 / 413，避免文案误导
                self.close_connection = True
                self._json({"error": "Content-Length 不合法"}, 400)
                return
            if n > MAX_BODY:
                # 超限不读取正文，直接关闭连接，避免残留字节污染后续请求
                self.close_connection = True
                self._json({"error": f"请求体过大（上限 {MAX_BODY} 字节）"}, 413)
                return
            if n == 0:
                # 缺 Content-Length / 空正文不再按默认值真跑一次 60 作业
                self._json({"error": "请求体为空，请提供 jobs / seed / policy"}, 400)
                return
            # 正文读取用更短的连接内超时（默认 10s）：声明长度大于实际正文时
            # 立刻回 400，不必占满 60s 的连接级超时（Slowloris 变体，sweep-http R3/D1）
            self.connection.settimeout(BODY_READ_TIMEOUT)
            try:
                raw_body = self.rfile.read(n)
            except (TimeoutError, socket.timeout):
                self.close_connection = True
                self._json({"error": "请求体不完整（读取超时）"}, 400)
                return
            finally:
                try:
                    self.connection.settimeout(self.timeout)
                except OSError:  # best-effort：连接已断时忽略
                    pass
            if not raw_body.strip():
                self._json({"error": "请求体为空，请提供 jobs / seed / policy"}, 400)
                return
            body = json.loads(raw_body)
            if not isinstance(body, dict):
                raise ValueError("请求体必须是 JSON 对象")
            # 与 CLI（gcs/demo.py MIN_JOBS=1）同一口径：越界直接 400，不静默 clamp；
            # 且必须是整数值（1.5 这类小数会被 int() 截断成 1，属于静默改口径）
            jobs_n = _as_int(body.get("jobs", 60), "jobs")
            if not MIN_JOBS <= jobs_n <= MAX_JOBS:
                raise ValueError(f"jobs 必须在 {MIN_JOBS}-{MAX_JOBS} 之间")
            seed = _as_int(body.get("seed", 20261111), "seed")
            if not 0 <= seed < 2 ** 32:
                raise ValueError("seed 必须在 0 到 2^32-1 之间")
            key = body.get("policy", "full")
            if key not in POLICIES:
                raise ValueError(f"未知策略组 {key}")
            self._json(run_once(jobs_n, seed, key))
        except json.JSONDecodeError:
            self._json({"error": "请求体不是合法 JSON"}, 400)
        except UnicodeDecodeError:
            self._json({"error": "请求体必须是 UTF-8 编码"}, 400)
        except ValueError as exc:
            # 只回显我们自己抛出的中文错误（带中文的）；其余（如 int(nan) 的
            # “cannot convert float NaN to integer”）一律归一化，不回显 Python 原文，
            # 与 500 分支「不向客户端回显内部异常」的策略保持一致（sweep-http R2）。
            msg = str(exc)
            if any("\u4e00" <= ch <= "\u9fff" for ch in msg):
                self._json({"error": f"请求参数不合法：{msg}"}, 400)
            else:
                self._json({"error": "请求参数不合法：类型或取值超出允许范围"}, 400)
        except (TypeError, OverflowError, RecursionError):
            self._json({"error": "请求参数不合法：类型或取值超出允许范围"}, 400)
        except Exception as exc:
            # F1 加固：不向客户端回显内部异常类型/信息（防信息泄露），
            # 仅在服务端 stderr 记录完整异常，兼顾可观测性与安全性。
            sys.stderr.write(
                f"[ERROR] 未捕获异常 {self.command} {self.path.split('?')[0]}："
                f"{type(exc).__name__}: {exc}\n")
            try:
                self._json({"error": "服务内部错误，请稍后重试"}, 500)
            except Exception:  # best-effort：响应发送失败不再抛二次异常（sweep-http D11）
                _LOG.debug("500 响应发送失败 %s", self.path, exc_info=True)


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """限制并发处理线程数：超限直接 503 关闭，防线程/内存被慢连接耗尽（M9）。"""

    # 跨平台差异：Windows 的 SO_REUSEADDR 允许第二个实例「抢占」同一端口并
    # 打印启动成功，但请求仍全部流向第一个实例（误导性极强）；Linux/macOS 则
    # 直接 EADDRINUSE。关闭复用后两平台行为一致：第二个实例明确报「端口被占用」。
    allow_reuse_address = False

    def __init__(self, *args, max_concurrency: int = 64, **kwargs):
        super().__init__(*args, **kwargs)
        self._sem = threading.BoundedSemaphore(max_concurrency)

    def process_request(self, request, client_address):
        if not self._sem.acquire(blocking=False):
            try:
                head = ["HTTP/1.1 503 Service Unavailable",
                        "Content-Type: application/json; charset=utf-8",
                        "Content-Length: 0",
                        "Retry-After: 2",
                        "Connection: close"]
                for key, value in SECURITY_HEADERS.items():
                    head.append(f"{key}: {value}")
                request.sendall(("\r\n".join(head) + "\r\n\r\n").encode("latin-1"))
            except Exception:  # best-effort：失败不阻断主流程（编码/发送兜底）
                _LOG.debug("503 回写失败 %s", client_address, exc_info=True)
            finally:
                try:
                    request.close()
                except Exception:  # best-effort：失败不阻断主流程（编码/发送兜底）
                    _LOG.debug("503 连接关闭失败 %s", client_address, exc_info=True)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._sem.release()


def _open_browser(url: str) -> None:
    """--open 时尝试拉起默认浏览器；headless/无桌面环境失败则打印可手动访问的地址（P3-11）。"""
    try:
        ok = webbrowser.open(url)
    except Exception:  # best-effort：失败不阻断主流程（编码/发送兜底）
        _LOG.debug("webbrowser.open 抛异常 %s", url, exc_info=True)
        ok = False
    if not ok:
        print(f"[提示] 未能自动打开浏览器（当前环境可能无桌面会话），请手动访问：{url}")


def main() -> int:
    """命令行入口：解析 --host/--port/--open，启动线程化 HTTP 服务，Ctrl+C 退出。"""
    # line_buffering=True：stdout 被重定向/管道时也逐行输出，
    # 评委重定向到文件时仍能第一时间看到「操作台已启动」横幅。
    # stderr 也需重配：argparse 报错（如 --port 越界）走 stderr，
    # 只配 stdout 时在 cp1252 控制台上中文会变成 \uXXXX 转义。
    for _stream in (sys.stdout, sys.stderr):
        _reconfigure = getattr(_stream, "reconfigure", None)
        if _reconfigure is not None:
            try:
                if _stream is sys.stdout:
                    _reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
                else:
                    _reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # best-effort：失败不阻断主流程（编码/发送兜底）
                _LOG.debug("流重配 UTF-8 失败", exc_info=True)
    ap = argparse.ArgumentParser(description="绿算调度操作台")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000,
                    help="监听端口（1-65535；0 表示由系统分配临时端口）")
    ap.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    args = ap.parse_args()
    # 端口取值域：越界值会让 socket 抛 OverflowError 裸栈，必须提前拦
    if not 0 <= args.port <= 65535:
        ap.error(f"--port 必须在 0-65535 之间（收到 {args.port}）")

    for line in AUTH.startup_messages():
        print(f"[AUTH] {line}")
    if not AUTH.may_start:
        print("[AUTH] 启动中止（fail-closed）：请先执行 "
              "`python app/auth_tool.py generate` 或 `rotate`，"
              "或将 GCS_AUTH_MODE 设为 loopback / off（仅限可信本机调试）。")
        return 2
    if AUTH.enabled and not AUTH.token_hash:
        print(f"[AUTH] 当前绑定 {args.host}；未配置凭据时，"
              "非回环来源的写接口与轨迹接口一律 401。")

    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    Handler.bound_host = args.host  # 供 Host 白名单（P3-3）
    try:
        srv = BoundedThreadingHTTPServer((args.host, args.port), Handler,
                                         max_concurrency=64)
    except socket.gaierror as exc:
        # 地址族/地址解析问题：改端口无效，必须换绑定地址
        print(f"[ERROR] 无法解析绑定地址 {args.host}（{exc}）；"
              "本服务默认使用 IPv4（AF_INET），请改用 127.0.0.1 或 0.0.0.0。",
              file=sys.stderr)
        return 2
    except OSError as exc:
        # 端口被占用：给出与 README「常见问题」一致的中文原因与处置建议
        print(f"[ERROR] 端口 {args.port} 无法监听（{type(exc).__name__}: {exc}）", file=sys.stderr)
        print(f"[ERROR] 请先关闭占用该端口的程序，或改用其他端口："
              f"python app/server.py --port {args.port + 1}", file=sys.stderr)
        return 2
    display_host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    # 端口为 0 时由系统分配临时端口，横幅必须显示真实端口，否则打不开
    real_port = srv.server_address[1] if srv.server_address else args.port
    url = f"http://{display_host}:{real_port}/"
    print(f"操作台已启动：{url}")
    print("按 Ctrl+C 停止。")
    if args.open:
        threading.Timer(0.8, _open_browser, args=(url,)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
