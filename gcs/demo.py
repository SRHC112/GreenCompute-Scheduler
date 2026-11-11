"""一键演示：生成作业流 → 多智能体编排 → 校验返工 → 导出看板。

用法：
    python -m gcs.demo                 # 默认 40 个作业
    python -m gcs.demo --jobs 60 --seed 7
    python -m gcs.demo --policy fcfs   # 跑对照组
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from .knowledge import KnowledgePack
from .llm import ModelGateway
from .orchestrator import Orchestrator, Policy
from .report import render_console, render_markdown
from .simulator import make_jobs

ROOT = Path(__file__).resolve().parent.parent
POLICIES = {
    "full": Policy(name="G-D 本方案", description="知识包 + 多目标调度 + 自省返工"),
    "fcfs": Policy(name="G-A FCFS", rule="fcfs", max_rounds=0),
    "sjf": Policy(name="G-B SJF+约束优先", rule="sjf", max_rounds=0),
    "generic": Policy(name="G-C 通用 Agent（模拟）", weights_mode="uniform",
                      candidate_mode="earliest", max_rounds=0),
    "no_reflect": Policy(name="G-E1 去自省闭环", max_rounds=0),
    "no_green": Policy(name="G-E2 去绿电权重", weights_mode="no_green"),
}


def main(argv: list[str] | None = None) -> int:
    try:  # Windows 控制台默认 GBK，统一成 UTF-8 避免中文与货币符号乱码
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="绿算调度端到端演示")
    ap.add_argument("--jobs", type=int, default=40)
    ap.add_argument("--seed", type=int, default=20261111)
    ap.add_argument("--policy", choices=sorted(POLICIES), default="full")
    ap.add_argument("--knowledge", default=str(ROOT / "knowledge" / "ningxia_cluster.yaml"))
    args = ap.parse_args(argv)

    pack = KnowledgePack.load(args.knowledge)
    jobs = make_jobs(pack, n=args.jobs, seed=args.seed)
    llm = ModelGateway(token_budget=int(pack.rework_budget["max_tokens"]))
    art = Orchestrator(pack, llm=llm, policy=POLICIES[args.policy]).run(jobs)

    run_id = datetime.now().strftime("run_%Y%m%d_%H%M%S") + f"_{args.policy}"
    outdir = ROOT / "runs" / run_id
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "artifact.json").write_text(
        json.dumps(art.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    (outdir / "metrics.json").write_text(
        json.dumps(art.metrics.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    render_console(art, pack.fingerprint(), outdir / "console.html")
    render_markdown(art, outdir / "report.md")
    latest = ROOT / "runs" / "latest.html"
    latest.write_text((outdir / "console.html").read_text(encoding="utf-8"), encoding="utf-8")

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
