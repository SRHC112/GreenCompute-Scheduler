"""一键演示：生成作业流 → 多智能体编排 → 校验返工 → 导出看板。

用法：
    python -m gcs.demo                 # 默认 40 个作业
    python -m gcs.demo --jobs 60 --seed 7
    python -m gcs.demo --policy fcfs   # 跑对照组

--jobs 下限为 1：0 / 负数会产生全零的无效指标，直接由 argparse 拒绝（退出码 2）。
合法取值的行为、输出结构与指标语义保持不变。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from .knowledge import KNOWLEDGE_HINT, KnowledgeError, load_knowledge
from .llm import ModelGateway
from .orchestrator import Orchestrator, Policy
from .report import render_console, render_markdown
from .simulator import make_jobs

_LOG = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
MIN_JOBS = 1  # 0 / 负数会导致 make_jobs 产出全零的无效指标，故设明确下限
MAX_JOBS = 300  # 与 app/server.py 的 HTTP 端口同一上界，三处口径（CLI/HTTP/前端）统一
POLICIES = {
    "full": Policy(name="G-D 本方案", description="知识包 + 多目标调度 + 自省返工"),
    "fcfs": Policy(name="G-A FCFS", rule="fcfs", max_rounds=0),
    "sjf": Policy(name="G-B SJF+约束优先", rule="sjf", max_rounds=0),
    "generic": Policy(name="G-C 通用 Agent（模拟）", weights_mode="uniform",
                      candidate_mode="earliest", max_rounds=0),
    "no_reflect": Policy(name="G-E1 去自省闭环", max_rounds=0),
    "no_green": Policy(name="G-E2 去绿电权重", weights_mode="no_green"),
}


def positive_jobs(raw: str) -> int:
    """--jobs 的类型与取值范围校验：非法取值给清晰中文提示，合法取值原样返回。

    与 HTTP 端（app/server.py 的 1-300）同一口径；此前 CLI 无上界而 HTTP 有，
    属同一参数两套口径——演示与评测场景 300 已远超所需，统一后不会改变任何
    合法作业数的编排、输出结构与指标语义。
    """
    try:
        n = int(raw)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"--jobs 必须是整数，收到 {raw!r}") from None
    if n < MIN_JOBS:
        raise argparse.ArgumentTypeError(
            f"--jobs 必须 >= {MIN_JOBS}（收到 {n}）；0 或负数会产出全零的无效指标")
    if n > MAX_JOBS:
        raise argparse.ArgumentTypeError(
            f"--jobs 必须 <= {MAX_JOBS}（收到 {n}）；演示与评测场景不需要更大规模")
    return n


def main(argv: list[str] | None = None) -> int:
    """端到端演示入口：生成作业流 → 多智能体编排 → 校验返工 → 导出看板。"""
    # Windows 控制台默认 GBK，统一成 UTF-8 避免中文与货币符号乱码。
    # 用 getattr 取 reconfigure（类型上 TextIO 协议没有该方法）；
    # 必须同时覆盖 stderr：argparse 的 usage/error 走 stderr，只配 stdout 时
    # 在 cp1252 控制台上中文会变成 \uXXXX 转义。
    for _stream in (sys.stdout, sys.stderr):
        _reconfigure = getattr(_stream, "reconfigure", None)
        if _reconfigure is not None:
            try:
                _reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # best-effort：失败不阻断主流程（编码/发送兜底）
                _LOG.debug("流重配 UTF-8 失败", exc_info=True)
    ap = argparse.ArgumentParser(description="绿算调度端到端演示")
    ap.add_argument("--jobs", type=positive_jobs, default=40,
                    help=f"作业数量，取值 {MIN_JOBS}-{MAX_JOBS}（默认 40）")
    ap.add_argument("--seed", type=int, default=20261111)
    ap.add_argument("--policy", choices=sorted(POLICIES), default="full")
    ap.add_argument("--knowledge", default=str(ROOT / "knowledge" / "ningxia_cluster.yaml"))
    args = ap.parse_args(argv)

    try:  # P3-1：知识包缺失/非法 → 中文错误 + 退出码 2，不打印裸 traceback
        pack = load_knowledge(args.knowledge)
    except KnowledgeError as exc:
        print(f"[ERROR] 知识包加载失败：{exc}", file=sys.stderr)
        print(f"[ERROR] {KNOWLEDGE_HINT}", file=sys.stderr)
        return 2
    jobs = make_jobs(pack, n=args.jobs, seed=args.seed)
    llm = ModelGateway(token_budget=int(pack.rework_budget["max_tokens"]))
    # 启动预检：在线配置先验一次「地址可达 + 密钥被接受」。
    # 鉴权失败时提前降级并明示原因，避免逐个作业反复失败、且让用户误以为结果是模型产出。
    if llm.online:
        ok, reason = llm.probe()
        if not ok:
            print(f"[WARN] {reason}", file=sys.stderr)
            print("[WARN] 已切换为离线确定性模式：指标数值不变，"
                  "归因叙述与 rubric 判定改由模板生成。", file=sys.stderr)
            llm.online = False
            llm.mode = "offline-mock(precheck-failed)"
    art = Orchestrator(pack, llm=llm, policy=POLICIES[args.policy]).run(jobs)

    run_id = datetime.now().strftime("run_%Y%m%d_%H%M%S_%f") + f"_{args.policy}"
    outdir = ROOT / "runs" / run_id
    outdir.mkdir(parents=True, exist_ok=True)
    try:  # F5：产物写盘失败不泄漏裸 traceback，统一中文提示 + 非零退出码（写盘错 rc=2）
        (outdir / "artifact.json").write_text(
            json.dumps(art.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        (outdir / "metrics.json").write_text(
            json.dumps(art.metrics.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        render_console(art, pack.fingerprint(), outdir / "console.html")
        render_markdown(art, outdir / "report.md")
        latest = ROOT / "runs" / "latest.html"
        latest.write_text((outdir / "console.html").read_text(encoding="utf-8"), encoding="utf-8")
    except OSError as exc:
        print(f"[ERROR] 产物写盘失败（--outdir {outdir}）：{exc}", file=sys.stderr)
        return 2

    s = art.summary()
    print("=" * 62)
    print(f"策略组    {s['policy']}")
    print(f"作业数    {s['jobs']}　返工轮次 {s['rounds']}　rubric {s['final_score']}")
    print(f"SLA 达成  {s['sla_rate']:.1%}")
    print(f"单位成本  {s['cost_per_gpu_hour']:.2f} 元 / GPU·h")
    print(f"平均绿电  {s['avg_green_share']:.1%}　碳排 {s['total_carbon_kg']:.1f} kg")
    print(f"token     {s['tokens']}")
    print(f"产物      {outdir}")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
