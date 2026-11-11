"""对照实验：五组配置 × 三个随机种子，回答「增量到底来自哪里」。

实验设计（对应方案书第 8 章）：
- 同一份模拟器数据：每个种子生成的作业流与故障真值完全一致；
- 同一份知识包：所有组读同一个 nx-2026.11；
- 同一份评测脚本：指标采集点在编排引擎终态，不随策略变化；
- 每组重复 3 次，报均值与标准差，原始结果逐行落 CSV 随作品提交。

产出：
    evaluation/results.csv     逐次原始结果（可追溯到每一次运行）
    evaluation/summary.md      均值 ± 标准差与相对提升
    evaluation/eval_report.html  可直接截图的对比看板
"""

from __future__ import annotations

import csv
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gcs.knowledge import KnowledgePack  # noqa: E402
from gcs.orchestrator import Orchestrator  # noqa: E402
from gcs.simulator import make_jobs  # noqa: E402

GROUPS = [
    ("G-A", "FCFS 规则调度（无 Agent 协作）", "fcfs",
     "先到先服务，代表最朴素的规则调度基线"),
    ("G-B", "SJF + 约束优先规则调度", "sjf",
     "最短作业优先，代表做过一轮优化的规则调度天花板"),
    ("G-C", "通用 Agent 直接调度（模拟，无领域知识包）", "generic",
     "等价于把通用长程 Agent Harness 直接用于垂直调度：不做窗口对齐、单轮不返工"),
    ("G-D", "本方案（知识包 + 多目标调度 + 自省返工）", "full",
     "完整方案"),
    ("G-E1", "消融：去掉自省闭环", "no_reflect",
     "只做一轮，无验证—归因—返工"),
    ("G-E2", "消融：去掉绿电权重", "no_green",
     "目标函数中绿电与碳排权重置零"),
]

SEEDS = [20261111, 7, 42]
JOBS = 80

FIELDS = ["group", "group_name", "seed", "jobs", "sla_rate", "budget_rate", "green_ok_rate",
          "avg_wait_min", "p95_wait_min", "cost_per_gpu_hour", "avg_green_share",
          "green_window_ratio", "total_carbon_kg", "rounds", "rubric_score", "tokens"]


def run_all(outdir: Path) -> list[dict]:
    pack = KnowledgePack.load(ROOT / "knowledge" / "ningxia_cluster.yaml")
    from gcs.demo import POLICIES

    rows: list[dict] = []
    outdir.mkdir(parents=True, exist_ok=True)
    for seed in SEEDS:
        jobs = make_jobs(pack, n=JOBS, seed=seed)
        for code, name, key, _desc in GROUPS:
            art = Orchestrator(pack, policy=POLICIES[key]).run(jobs)
            m = art.metrics.as_dict()
            rows.append({
                "group": code, "group_name": name, "seed": seed,
                "jobs": m["jobs"], "sla_rate": m["sla_rate"], "budget_rate": m["budget_rate"],
                "green_ok_rate": m["green_ok_rate"], "avg_wait_min": m["avg_wait_min"],
                "p95_wait_min": m["p95_wait_min"],
                "cost_per_gpu_hour": m["cost_per_gpu_hour"],
                "avg_green_share": m["avg_green_share"],
                "green_window_ratio": m["green_window_ratio"],
                "total_carbon_kg": m["total_carbon_kg"], "rounds": art.rounds,
                "rubric_score": art.rubric_history[-1].score if art.rubric_history else 0,
                "tokens": art.llm.get("tokens_total", 0),
            })
            print(f"{code:<5} seed={seed:<9} sla={m['sla_rate']:.1%} "
                  f"cost={m['cost_per_gpu_hour']:.2f} green_win={m['green_window_ratio']:.1%} "
                  f"rounds={art.rounds} rubric={rows[-1]['rubric_score']}")
    return rows


def write_csv(rows: list[dict], path: Path) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def aggregate(rows: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for code, name, _key, _d in [(g[0], g[1], g[2], g[3]) for g in GROUPS]:
        sub = [r for r in rows if r["group"] == code]
        if not sub:
            continue
        stats = {}
        for f in ("sla_rate", "budget_rate", "avg_wait_min", "cost_per_gpu_hour",
                  "avg_green_share", "green_window_ratio", "total_carbon_kg",
                  "rounds", "rubric_score"):
            vals = [float(r[f]) for r in sub]
            stats[f] = (statistics.mean(vals), statistics.pstdev(vals) if len(vals) > 1 else 0.0)
        out[code] = {"name": name, "n": len(sub), **stats}
    return out


def write_summary(rows: list[dict], stats: dict, path: Path) -> None:
    base = stats.get("G-A")
    lines = [
        "# 对照实验结果摘要",
        "",
        f"作业规模 {JOBS} 个/次，重复 {len(SEEDS)} 次（种子 {', '.join(map(str, SEEDS))}），"
        "同一模拟器数据、同一知识包版本 nx-2026.11、同一评测脚本。",
        "",
        "| 组别 | 配置 | SLA 达成率 | 预算达标率 | 绿电达标率 | 单位成本(元/GPU·h) | "
        "绿电窗口作业占比 | 平均等待(min) | 碳排(kg) | 返工轮次 | rubric |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for code, _n, _k, _d in GROUPS:
        s = stats.get(code)
        if not s:
            continue
        lines.append(
            f"| {code} | {s['name']} | {s['sla_rate'][0]:.1%}±{s['sla_rate'][1]:.1%} "
            f"| {s['budget_rate'][0]:.1%}±{s['budget_rate'][1]:.1%} "
            f"| {s['avg_green_share'][0]:.1%}±{s['avg_green_share'][1]:.1%} "
            f"| {s['cost_per_gpu_hour'][0]:.2f}±{s['cost_per_gpu_hour'][1]:.2f} "
            f"| {s['green_window_ratio'][0]:.1%}±{s['green_window_ratio'][1]:.1%} "
            f"| {s['avg_wait_min'][0]:.0f}±{s['avg_wait_min'][1]:.0f} "
            f"| {s['total_carbon_kg'][0]:.0f}±{s['total_carbon_kg'][1]:.0f} "
            f"| {s['rounds'][0]:.1f} | {s['rubric_score'][0]:.0f} |")
    if base:
        full = stats.get("G-D")
        if full:
            lines += [
                "",
                "## 相对 G-A（FCFS 规则调度）的变化",
                "",
                f"- SLA 达成率：{base['sla_rate'][0]:.1%} → {full['sla_rate'][0]:.1%}"
                f"（{(full['sla_rate'][0] - base['sla_rate'][0]) * 100:+.1f} 个百分点）",
                f"- 预算达标率：{base['budget_rate'][0]:.1%} → {full['budget_rate'][0]:.1%}",
                f"- 单位成本：{base['cost_per_gpu_hour'][0]:.2f} → {full['cost_per_gpu_hour'][0]:.2f} 元/GPU·h"
                f"（{(full['cost_per_gpu_hour'][0] / base['cost_per_gpu_hour'][0] - 1) * 100:+.1f}%）",
                f"- 绿电窗口作业占比：{base['green_window_ratio'][0]:.1%} → {full['green_window_ratio'][0]:.1%}",
                f"- 平均等待：{base['avg_wait_min'][0]:.0f} → {full['avg_wait_min'][0]:.0f} 分钟"
                "（用可控的排队换成本与绿电）",
                "",
                "> 说明：G-D 与 G-E2 的成本差来自目标函数中绿电权重的显式代价，"
                "并非缺陷；G-C 为通用 Agent 的行为模拟，不是真实 DeerFlow 运行结果，"
                "在答辩中按“模拟对照”表述。",
            ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_html(rows: list[dict], stats: dict, path: Path) -> None:
    metrics = [
        ("sla_rate", "SLA 达成率", True, 100),
        ("budget_rate", "预算达标率", True, 100),
        ("cost_per_gpu_hour", "单位成本 元/GPU·h", False, None),
        ("green_window_ratio", "绿电窗口作业占比", True, 100),
        ("avg_wait_min", "平均等待 分钟", False, None),
        ("rubric_score", "rubric 得分", False, None),
    ]
    blocks = []
    for key, label, is_pct, scale in metrics:
        vals = {code: stats[code][key][0] for code in stats}
        mx = max(vals.values()) or 1
        rows_html = ""
        for code, name, _k, _d in GROUPS:
            if code not in stats:
                continue
            mean, sd = stats[code][key]
            v = vals[code]
            w = (v / mx) * 100
            txt = f"{v * 100:.1f}%" if is_pct else f"{v:.2f}"
            rows_html += (
                f'<tr><td class="g">{code}</td><td>{name}</td>'
                f'<td><div class="track"><div class="fill" style="width:{w:.1f}%"></div></div></td>'
                f'<td class="v">{txt}</td><td class="sd">±{sd * 100:.1f}% / ±{sd:.2f}</td></tr>')
        blocks.append(f'<div class="blk"><h3>{label}</h3><table>{rows_html}</table></div>')

    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>对照实验结果</title><style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:"Microsoft YaHei","Noto Sans CJK SC",system-ui,sans-serif;background:#F2F4F8;color:#232A33}}
.hd{{background:#1F3352;color:#fff;padding:20px 28px;border-bottom:5px solid #E08A3C}}
.hd h1{{font-size:24px}} .hd p{{font-size:13px;color:#B9C9E2;margin-top:6px}}
.wrap{{max-width:1280px;margin:0 auto;padding:20px 26px 40px}}
.note{{background:#fff;border:1px solid #E2E7EF;border-left:4px solid #E08A3C;border-radius:5px;
      padding:12px 16px;font-size:13px;color:#4A5768;margin-bottom:18px;line-height:1.7}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}
.blk{{background:#fff;border:1px solid #E2E7EF;border-radius:6px;padding:14px 16px}}
.blk h3{{font-size:14px;color:#1F3352;margin-bottom:10px}}
table{{width:100%;border-collapse:collapse;font-size:12px}}
td{{padding:6px 6px;border-bottom:1px solid #F0F3F7;color:#3A4553;vertical-align:middle}}
td.g{{color:#1F3352;font-weight:700;width:46px}}
td.v{{text-align:right;width:86px;font-weight:600;color:#1F3352}}
td.sd{{text-align:right;width:120px;color:#9AA5B4;font-size:11px}}
.track{{height:9px;background:#EDF0F5;border-radius:5px;overflow:hidden}}
.fill{{height:100%;background:linear-gradient(90deg,#2F6DB5,#1F8A70);border-radius:5px}}
footer{{margin-top:18px;font-size:11.5px;color:#98A3B3;text-align:center}}
</style></head><body>
<div class="hd"><h1>对照实验：五组配置 × 三次重复</h1>
<p>同一模拟器数据 · 同一知识包 nx-2026.11 · 同一评测脚本 · 作业规模 {JOBS} 个/次 · 随机种子 {', '.join(map(str, SEEDS))}</p></div>
<div class="wrap">
<div class="note">G-A / G-B 为规则调度基线；G-C 为“通用 Agent 直接调度”的<strong>行为模拟</strong>
（等价于把通用长程 Agent Harness 搬到垂直调度场景：不做电价绿电窗口对齐、单轮不返工），
答辩时须如实标注为模拟对照，而非真实 DeerFlow 运行结果；G-E1 / G-E2 为消融组。
所有数字由同一套确定性评测脚本采集，原始逐行数据见 results.csv。</div>
<div class="grid">{''.join(blocks)}</div>
<footer>绿算调度 GreenCompute-Scheduler · 2026 第五届“未来杯”创新大赛 · 赛道一 Agent 编排创新应用</footer>
</div></body></html>"""
    path.write_text(html, encoding="utf-8")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    outdir = ROOT / "evaluation"
    print("开始对照实验……")
    rows = run_all(outdir)
    write_csv(rows, outdir / "results.csv")
    stats = aggregate(rows)
    (outdir / "summary.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2),
                                          encoding="utf-8")
    write_summary(rows, stats, outdir / "summary.md")
    write_html(rows, stats, outdir / "eval_report.html")
    print(f"\n产物：\n  {outdir / 'results.csv'}\n  {outdir / 'summary.md'}\n  "
          f"{outdir / 'eval_report.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
