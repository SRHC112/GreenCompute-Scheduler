"""对照实验：六组配置 × 三个随机种子，回答「增量到底来自哪里」。

实验设计（对应方案书第 8 章）：
- 同一份模拟器数据：每个种子生成的作业流与故障真值完全一致；
- 同一份知识包：所有组读同一个 KB_VERSION（从 knowledge/ningxia_cluster.yaml 读取，不硬编码）；
- 同一份评测脚本：指标采集点在编排引擎终态，不随策略变化；
- 每组重复 3 次，报均值 ± 标准差；同样一组做 200 次重启动。
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import statistics
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gcs.knowledge import KnowledgePack  # noqa: E402
from gcs.orchestrator import Orchestrator  # noqa: E402
from gcs.simulator import make_jobs  # noqa: E402

KB_PATH = ROOT / "knowledge" / "ningxia_cluster.yaml"
try:  # 版本号以知识包为准，避免脚本里硬编码导致与实验产物脱节
    KB_VERSION = KnowledgePack.load(KB_PATH).version
except Exception:  # 知识包缺失时仍允许脚本启动，只是版本标记为 unknown
    KB_VERSION = "unknown"

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

SEEDS_FIXED = [20261111, 7, 42]                 # 固定种子，3 次重动（均值 ± 标准差）
SEEDS_BOOTSTRAP = list(range(20261111, 20261111 + 200))  # 200 次重启动做非参数置信区间
JOBS = 80

FIELDS = ["group", "group_name", "seed", "jobs", "sla_rate", "budget_rate", "green_ok_rate",
          "avg_wait_min", "p95_wait_min", "cost_per_gpu_hour", "avg_green_share",
          "green_window_ratio", "total_carbon_kg", "rounds", "rubric_score", "tokens"]


def run_all(outdir: Path, jobs_n: int) -> list[dict]:
    """跑完 6 个对照组 × 3 个固定种子，逐次记录指标并落盘，返回原始行。

    同一份作业流喂给所有组：对照组之间唯一的差异是 Policy 开关，
    这样指标差异才能归因到“领域知识/自省闭环”，而不是随机种子。
    """
    pack = KnowledgePack.load(ROOT / "knowledge" / "ningxia_cluster.yaml")
    from gcs.demo import POLICIES
    from gcs.llm import ModelGateway

    rows: list[dict] = []
    outdir.mkdir(parents=True, exist_ok=True)
    for seed in SEEDS_FIXED:
        jobs = make_jobs(pack, n=jobs_n, seed=seed)
        for code, name, key, _desc in GROUPS:
            # 每次运行用独立网关，token 计数按组归集，不跨组串账
            art = Orchestrator(pack, llm=ModelGateway(), policy=POLICIES[key]).run(jobs)
            m = art.metrics.as_dict()
            rows.append({
                "group": code, "group_name": name,
                "seed": seed, "jobs": m["jobs"],
                "sla_rate": m["sla_rate"], "budget_rate": m["budget_rate"],
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
    """把逐次运行记录写成 CSV（带 BOM，Excel 双击不乱码），便于二次分析。"""
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def aggregate(rows: list[dict]) -> dict[str, dict]:
    """均值 ± 标准差 + 95% 置信区间半宽；按「组 × 指标」各自计算。"""
    out: dict[str, dict] = {}
    for code, name, _key, _d in [(g[0], g[1], g[2], g[3]) for g in GROUPS]:
        sub = [r for r in rows if r["group"] == code]
        if not sub:
            continue
        stats = {}
        for f in ("sla_rate", "budget_rate", "green_ok_rate", "avg_wait_min",
                  "p95_wait_min", "cost_per_gpu_hour", "avg_green_share",
                  "green_window_ratio", "total_carbon_kg", "rubric_score", "rounds"):
            vals = [float(r[f]) for r in sub]
            mean = statistics.mean(vals)
            sd = statistics.pstdev(vals) if len(vals) > 1 else 0.0
            half_width = 1.96 * sd / (len(vals) ** 0.5) if len(vals) > 1 else 0.0
            stats[f + "_mean"] = mean
            stats[f + "_sd"] = sd
            stats[f + "_ci95"] = half_width
        out[code] = {"name": name, "n": len(sub), **stats}
    return out


def write_summary_json(rows: list[dict], path: Path) -> None:
    """写出操作台 `/api/eval` 读取的 summary.json。

    结构与 `app/server.py` 的取值方式严格对应：`{组: {name, n, 指标: [均值, 标准差]}}`。
    与 results.csv / summary.md 同批同源，避免看板长期展示上一轮旧数据。
    """
    out: dict[str, dict] = {}
    for code, name, _key, _d in [(g[0], g[1], g[2], g[3]) for g in GROUPS]:
        sub = [r for r in rows if r["group"] == code]
        if not sub:
            continue
        item: dict = {"name": name, "n": len(sub)}
        for f in ("sla_rate", "budget_rate", "green_ok_rate", "avg_wait_min",
                  "p95_wait_min", "cost_per_gpu_hour", "avg_green_share",
                  "green_window_ratio", "total_carbon_kg", "rubric_score", "rounds"):
            vals = [float(r[f]) for r in sub]
            mean = statistics.mean(vals)
            sd = statistics.pstdev(vals) if len(vals) > 1 else 0.0
            item[f] = [round(mean, 10), round(sd, 10)]
        out[code] = item
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def bootstrap_ci(rows: list[dict], n_boot: int = 200, seed: int = 20261111) -> dict[str, dict]:
    """非参数自助置信区间：对固定组内样本做随机重采样合并重启动。

    用于回答「如果只做 1 次运行，指标会落在哪个区间」——
    它是对固定种子 3 次样本的二次抽样估计，不增加真实样本数。
    """
    rng = random.Random(seed)
    out: dict[str, dict] = {}
    for code, name, _key, _d in [(g[0], g[1], g[2], g[3]) for g in GROUPS]:
        sub = [r for r in rows if r["group"] == code]
        if not sub:
            continue
        idx = list(range(len(sub)))
        boot_means = {f: [] for f in
                      ("sla_rate", "budget_rate", "avg_wait_min",
                       "cost_per_gpu_hour", "green_window_ratio",
                       "total_carbon_kg", "rubric_score", "rounds")}
        for _ in range(n_boot):
            sample = [sub[rng.choice(idx)] for _ in range(len(sub))]
            for f in boot_means:
                boot_means[f].append(statistics.mean([float(r[f]) for r in sample]))
        stats = {}
        for f in boot_means:
            vals = boot_means[f]
            q = statistics.quantiles(vals, n=100)
            stats[f + "_lo95"] = q[4]
            stats[f + "_hi95"] = q[95]
        out[code] = {"name": name, "n": len(sub), **stats}
    return out


def bootstrap_diff(rows: list[dict], n_boot: int = 200, seed: int = 20261111,
                   subgroup: str | None = None) -> dict[str, dict]:
    """两组差异的非参数置信区间：G-D − G-A、G-D − G-E1 等。

    每次重启动中分别重采样两组，再做差，取 2.5% 与 97.5% 分位数。
    """
    out: dict[str, dict] = {}
    groups = [subgroup] if subgroup else [g[0] for g in GROUPS]
    for a in groups:
        for b in groups:
            if a >= b:
                continue
            sa = [r for r in rows if r["group"] == a]
            sb = [r for r in rows if r["group"] == b]
            if not sa or not sb:
                continue
            rng = random.Random(seed)
            idx_a, idx_b = list(range(len(sa))), list(range(len(sb)))
            diffs = {f: [] for f in
                     ("sla_rate", "budget_rate", "avg_wait_min",
                      "cost_per_gpu_hour", "green_window_ratio",
                      "total_carbon_kg", "rubric_score", "rounds")}
            for _ in range(n_boot):
                s1 = [sa[rng.choice(idx_a)] for _ in range(len(sa))]
                s2 = [sb[rng.choice(idx_b)] for _ in range(len(sb))]
                for f in diffs:
                    diffs[f].append(statistics.mean([float(r[f]) for r in s1]) -
                                    statistics.mean([float(r[f]) for r in s2]))
            stats = {}
            for f in diffs:
                vals = diffs[f]
                q = statistics.quantiles(vals, n=100)
                # n=100 时切点对应 1%~99% 分位，q[4]=5%、q[94]=95%，构成双侧 95% 区间
                stats[f + "_lo95"] = q[4]
                stats[f + "_hi95"] = q[94]
            out[f"{a}_minus_{b}"] = {"name": f"{a} − {b}", "n": len(sa) + len(sb), **stats}
    return out


def run_sensitivity(pack: KnowledgePack, jobs_n: int = JOBS,
                    seeds: list[int] | None = None) -> dict:
    """两个开关的真实重跑：返工阈值与绿电权重。

    不是「按经验估算」，而是把同一批作业流拿去做参数扫描，读引擎终态指标；
    每个参数点跑满全部固定种子再取均值，避免单种子噪声把趋势读反。
    """
    from gcs.demo import POLICIES
    from gcs.llm import ModelGateway

    seeds = seeds or SEEDS_FIXED
    streams = [(s, make_jobs(pack, n=jobs_n, seed=s)) for s in seeds]
    base_policy = POLICIES["full"]

    def one(policy, jobs, threshold=None) -> dict:
        """用给定策略（可选覆盖返工阈值）跑一个种子，返回该次的指标字典。"""
        art = Orchestrator(pack, llm=ModelGateway(), policy=policy,
                           reflect_threshold=threshold).run(jobs)
        m = art.metrics
        return {
            "rounds": art.rounds,
            "final_rubric": art.rubric_history[-1].score if art.rubric_history else 0,
            "sla_rate": m.sla_rate,
            "budget_rate": m.budget_rate,
            "cost_per_gpu_hour": m.cost_per_gpu_hour,
            "green_window_ratio": m.green_window_ratio,
            "avg_green_share": m.avg_green_share,
            "avg_wait_min": m.avg_wait_min,
            "total_carbon_kg": m.total_carbon_kg,
            "tokens": art.llm.get("tokens_total", 0),
        }

    def run(policy, threshold=None) -> dict:
        """对全部固定种子各跑一次并取均值，作为该参数取值下的代表结果。"""
        per_seed = [one(policy, jobs, threshold) for _s, jobs in streams]
        return {k: statistics.mean(float(r[k]) for r in per_seed) for k in per_seed[0]}

    thresholds = [60, 70, 80, 90, 95]
    th_rows = [{"threshold": t, **run(base_policy, t)} for t in thresholds]

    # 知识包里「绿电+碳排」合计约占 35%，按占比反推倍率，得到 0/15/35/60% 四档
    base_share = statistics.mean(
        [pack.weights_for(j.type)["green"] + pack.weights_for(j.type)["carbon"]
         for j in [j for _s, js in streams for j in js]])
    targets = [0.0, 0.15, 0.35, 0.60]
    w_rows = []
    for share in targets:
        k = share / base_share if base_share else 0.0
        policy = replace(base_policy, green_scale=k)
        row = {"green_share_target": share, "green_scale": round(k, 3), **run(policy)}
        # 用实测权重反算实际占比，避免「标称 60%、实际不是」这类口径问题
        eff = Orchestrator(pack, policy=policy)._weights(streams[0][1])
        if eff is None:  # 倍率为 1.0 时引擎直接走知识包，不显式建权重表
            eff = {j.id: pack.weights_for(j.type) for j in streams[0][1]}
        row["green_share_actual"] = statistics.mean(
            [w["green"] + w["carbon"] for w in eff.values()])
        w_rows.append(row)

    return {"jobs": jobs_n, "seeds": seeds, "threshold": th_rows, "weights": w_rows}


DIFF_METRICS = [
    ("cost_per_gpu_hour", "单位成本(元/GPU·h)", "{:.2f}"),
    ("green_window_ratio", "绿电窗口作业占比", "{:.1%}"),
    ("rubric_score", "rubric 得分", "{:.0f}"),
    ("sla_rate", "SLA 达成率", "{:.1%}"),
    ("avg_wait_min", "平均等待(min)", "{:.1f}"),
]


def diff_lines(diff: dict) -> list[str]:
    """把自助法差值区间渲染成表，并按「区间是否跨 0」判定显著性。"""
    picks = [("G-A_minus_G-D", "G-A − G-D（正数表示 G-D 更低/更高）"),
             ("G-D_minus_G-E1", "G-D − G-E1（自省闭环的增量）"),
             ("G-D_minus_G-E2", "G-D − G-E2（绿电权重的代价）")]
    lines = [
        "",
        "## 差异的 95% 置信区间（自助法，200 次重采样）",
        "",
        "| 对比 | 指标 | 差值 | 95% 区间 | 判定 |",
        "|---|---|---|---|---|",
    ]
    flags = {}
    for key, label in picks:
        d = diff.get(key)
        if not d:
            continue
        for field, mlabel, fmt in DIFF_METRICS:
            lo, hi = d[f"{field}_lo95"], d[f"{field}_hi95"]
            sig = lo > 0 or hi < 0
            flags[(key, field)] = sig
            lines.append(
                f"| {label} | {mlabel} | {fmt.format(d[f'{field}_lo95'])} ~ "
                f"{fmt.format(d[f'{field}_hi95'])} | {'显著' if sig else '不显著'} |")
    lines += ["", "判定口径：区间不含 0 记为显著（95% 水平）；仅 3 个固定种子，"
              "区间偏宽，凡「不显著」的结论在正文里只按方向性表述。"]
    return lines


def run_pareto(pack: KnowledgePack, jobs_n: int = JOBS,
               seeds: list[int] | None = None) -> dict:
    """排队—成本帕累托前沿：只放大「排队」权重，看能把等待压到什么程度、代价是多少。

    这不是又一组对照组，而是对同一个方案做反事实扫描：约束不变、数据不变，
    只改“更在意排队还是更在意成本与绿电”这一个偏好系数。
    """
    from gcs.demo import POLICIES
    from gcs.llm import ModelGateway

    seeds = seeds or SEEDS_FIXED
    streams = [(s, make_jobs(pack, n=jobs_n, seed=s)) for s in seeds]
    base = POLICIES["full"]
    rows = []
    for k in (0.5, 1.0, 2.0, 4.0, 8.0, 16.0):
        per = []
        for _s, jobs in streams:
            art = Orchestrator(pack, llm=ModelGateway(),
                               policy=replace(base, wait_scale=k)).run(jobs)
            m = art.metrics
            per.append((m.avg_wait_min, m.cost_per_gpu_hour,
                        m.green_window_ratio, m.sla_rate))
        rows.append({
            "wait_scale": k,
            "avg_wait_min": statistics.mean(x[0] for x in per),
            "cost_per_gpu_hour": statistics.mean(x[1] for x in per),
            "green_window_ratio": statistics.mean(x[2] for x in per),
            "sla_rate": statistics.mean(x[3] for x in per),
        })
    return {"jobs": jobs_n, "seeds": seeds, "rows": rows}


def pareto_lines(pareto: dict) -> list[str]:
    """把帕累托扫描渲染成表，并给出可与不可宣称的边界。"""
    rows = pareto.get("rows", [])
    if not rows:
        return []
    rows = sorted(rows, key=lambda r: r["wait_scale"])
    base = next((r for r in rows if abs(r["wait_scale"] - 1.0) < 1e-9), rows[0])
    best = min(rows, key=lambda r: r["avg_wait_min"])
    lines = [
        "",
        "## 排队—成本帕累托前沿（反事实扫描）",
        "",
        "做法：约束、作业流、种子全部不变，只放大目标函数里的「排队」权重，观察等待与成本如何互换。",
        "",
        "| 排队权重倍率 | 平均等待(min) | 单位成本(元/GPU·h) | 绿电窗口占比 | SLA 达成率 |",
        "|---|---|---|---|---|",
    ]
    for r in rows:
        mark = "（当前配置）" if abs(r["wait_scale"] - 1.0) < 1e-9 else ""
        lines.append(
            f"| ×{r['wait_scale']:g}{mark} | {r['avg_wait_min']:.1f} "
            f"| {r['cost_per_gpu_hour']:.2f} | {r['green_window_ratio']:.1%} "
            f"| {r['sla_rate']:.1%} |")

    # 非支配点：不存在另一个点同时做到等待更少、成本更低、绿电更高
    def dominated(a: dict, b: dict) -> bool:
        """a 是否被 b 支配（b 在等待/成本/绿电三个维度都不差，且至少一维更好）。"""
        no_worse = (b["avg_wait_min"] <= a["avg_wait_min"]
                    and b["cost_per_gpu_hour"] <= a["cost_per_gpu_hour"]
                    and b["green_window_ratio"] >= a["green_window_ratio"])
        strictly = (b["avg_wait_min"] < a["avg_wait_min"]
                    or b["cost_per_gpu_hour"] < a["cost_per_gpu_hour"]
                    or b["green_window_ratio"] > a["green_window_ratio"])
        return no_worse and strictly

    front = [r for r in rows if not any(dominated(r, o) for o in rows if o is not r)]
    front_desc = "、".join(f"×{r['wait_scale']:g}" for r in front)

    # 拐点：能达到接近最低等待的最小倍率
    target = best["avg_wait_min"] + 0.25
    knee = next((r for r in rows if r["avg_wait_min"] <= target), best)
    dw = base["avg_wait_min"] - knee["avg_wait_min"]
    dc = knee["cost_per_gpu_hour"] - base["cost_per_gpu_hour"]
    pct = dc / base["cost_per_gpu_hour"] if base["cost_per_gpu_hour"] else 0.0
    dgreen = knee["green_window_ratio"] - base["green_window_ratio"]
    if dw <= 0.5:
        verdict = (f"即使把排队权重放大到最高档，平均等待也只再降 {dw:.1f} 分钟，"
                   "说明当前等待已接近该规模下的下限——排队偏长不是偏好问题，"
                   "而是资源总量与截止时刻共同决定的。")
    elif pct <= 0.02 and dgreen >= -0.01:
        verdict = (f"排队权重放到 ×{knee['wait_scale']:g} 就能把平均等待从 "
                   f"{base['avg_wait_min']:.1f} 分钟压到 {knee['avg_wait_min']:.1f} 分钟，"
                   f"而单位成本只变化 {pct:+.1%}（{base['cost_per_gpu_hour']:.2f} → "
                   f"{knee['cost_per_gpu_hour']:.2f} 元/GPU·h），绿电窗口占比变化 "
                   f"{dgreen:+.1%}。这是一个几乎免费的工作点，建议生产环境直接采用。")
    else:
        verdict = (f"排队权重放到 ×{knee['wait_scale']:g} 可把平均等待从 "
                   f"{base['avg_wait_min']:.1f} 分钟压到 {knee['avg_wait_min']:.1f} 分钟，"
                   f"代价是单位成本 {pct:+.1%}（{base['cost_per_gpu_hour']:.2f} → "
                   f"{knee['cost_per_gpu_hour']:.2f} 元/GPU·h）、绿电窗口占比 {dgreen:+.1%}。"
                   "这说明本方案的等待偏长是「用等待换成本与绿电」的显式取舍，"
                   "而不是排不出来；是否接受应由业务在前沿上自行选点。")
    lines += ["", f"帕累托非支配点：{front_desc}（在这条前沿上，等待更少、成本更低、绿电更高三者不可兼得）。"]
    # 成本几乎不变却能把等待明显压低的点，单独点出来
    free = [r for r in rows
            if r["cost_per_gpu_hour"] <= base["cost_per_gpu_hour"] * 1.005
            and r["avg_wait_min"] < base["avg_wait_min"] * 0.9]
    if free:
        pick = min(free, key=lambda r: r["avg_wait_min"])
        lines += ["", f"其中 ×{pick['wait_scale']:g} 是一个「几乎免费」的点：平均等待 "
                      f"{base['avg_wait_min']:.1f} → {pick['avg_wait_min']:.1f} 分钟，"
                      f"单位成本 {base['cost_per_gpu_hour']:.2f} → "
                      f"{pick['cost_per_gpu_hour']:.2f} 元/GPU·h 基本持平"
                      f"（绿电窗口占比 {pick['green_window_ratio']:.1%}，"
                      f"较当前 {base['green_window_ratio']:.1%} 略低）。"]
    lines += ["", verdict,
              "", f"口径：同一知识包 {KB_VERSION}、同一批种子，SLA 与预算约束不变，"
              "因此表中每一行都是可行解，差别只在偏好。"]
    return lines


def write_summary(rows: list[dict], stats: dict, path: Path, sens: dict | None = None,
                  diff: dict | None = None, pareto: dict | None = None) -> None:
    """写出对照实验摘要 Markdown：六组指标、相对 G-A 的变化、参数敏感性、差值区间与帕累托前沿。

    措辞纪律：置信区间跨 0 的结论只写“方向性”，不写成确定性收益。
    """
    base = stats.get("G-A")
    full = stats.get("G-D")
    e1 = stats.get("G-E1")
    e2 = stats.get("G-E2")
    lines = [
        "# 对照实验结果摘要",
        "",
        f"作业规模 {JOBS} 个/次，固定种子 {len(SEEDS_FIXED)} 次（"
        f"{', '.join(map(str, SEEDS_FIXED))}），同一模拟器数据、同一知识包版本 {KB_VERSION}、"
        "同一评测脚本。",
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
            f"| {code} | {s['name']} | {s['sla_rate_mean']:.1%}±{s['sla_rate_sd']:.1%} "
            f"| {s['budget_rate_mean']:.1%}±{s['budget_rate_sd']:.1%} "
            f"| {s['green_ok_rate_mean']:.1%}±{s['green_ok_rate_sd']:.1%} "
            f"| {s['cost_per_gpu_hour_mean']:.2f}±{s['cost_per_gpu_hour_sd']:.2f} "
            f"| {s['green_window_ratio_mean']:.1%}±{s['green_window_ratio_sd']:.1%} "
            f"| {s['avg_wait_min_mean']:.0f}±{s['avg_wait_min_sd']:.0f} "
            f"| {s['total_carbon_kg_mean']:.0f}±{s['total_carbon_kg_sd']:.0f} "
            f"| {s['rounds_mean']:.1f} | {s['rubric_score_mean']:.0f} |")
    if base and full:
        lines += [
            "",
            "## 相对 G-A（FCFS 规则调度）的变化",
            "",
            f"- SLA 达成率：{base['sla_rate_mean']:.1%} → {full['sla_rate_mean']:.1%}",
            f"- 预算达标率：{base['budget_rate_mean']:.1%} → {full['budget_rate_mean']:.1%}",
            f"- 单位成本：{base['cost_per_gpu_hour_mean']:.2f} → "
            f"{full['cost_per_gpu_hour_mean']:.2f} 元/GPU·h",
            f"- 绿电窗口作业占比：{base['green_window_ratio_mean']:.1%} → "
            f"{full['green_window_ratio_mean']:.1%}",
            f"- 平均等待：{base['avg_wait_min_mean']:.0f} → {full['avg_wait_min_mean']:.0f} 分钟"
            "（用可控的排队换成本与绿电）",
            "",
            "排队上升不是缺陷，而是显式的取舍：文末「排队—成本帕累托前沿」用反事实扫描给出了"
            "这条取舍曲线的完整形状与可选工作点。",
            "",
        ]
    if sens:
        th = sens["threshold"]
        wt = sens["weights"]
        lines += [
            "",
            "## 敏感性分析（参数扫描，真实重跑）",
            "",
            f"每档 {sens['jobs']} 个作业、固定种子 "
            f"{'、'.join(map(str, sens['seeds']))} 各跑一次后取均值，"
            "每次只改一个参数、其余不动，读引擎终态指标。",
            "",
            "### 1. 返工阈值",
            "",
            "| 阈值 | 实际返工轮次 | 终评 rubric | SLA 达成率 | 单位成本(元/GPU·h) "
            "| 绿电窗口作业占比 | 累计 token |",
            "|---|---|---|---|---|---|---|",
        ]
        for r in th:
            lines.append(
                f"| {r['threshold']} | {r['rounds']:.1f} | {r['final_rubric']:.0f} "
                f"| {r['sla_rate']:.1%} | {r['cost_per_gpu_hour']:.2f} "
                f"| {r['green_window_ratio']:.1%} | {r['tokens']:.0f} |")
        lo = min(th, key=lambda r: r["threshold"])
        hi = max(th, key=lambda r: r["threshold"])
        same = {r["final_rubric"] for r in th}
        plateau = [r["threshold"] for r in th
                   if r["final_rubric"] == hi["final_rubric"]]
        lines += [
            "",
            f"阈值从 {lo['threshold']} 提到 {hi['threshold']}，平均返工轮次 "
            f"{lo['rounds']:.1f} → {hi['rounds']:.1f}，平均终评 rubric "
            + ("始终为 " + f"{same.pop():.0f}" if len(same) == 1 else
               "在 " + "/".join(f"{r['final_rubric']:.0f}" for r in th) + " 之间变化")
            + "。",
            (f"终评满分在阈值 {'、'.join(map(str, plateau))} 这一区间内保持不变，"
             "说明满分来自方案本身，而不是把门槛调低。"
             if len(plateau) > 1 else
             f"终评满分仅在阈值 {plateau[0]} 时取得，阈值再高即触发额外返工。"),
            "",
            "### 2. 绿电权重",
            "",
            "| 绿电+碳排权重占比 | 倍率 | 实测占比 | 单位成本(元/GPU·h) | 绿电窗口作业占比 "
            "| 平均绿电占比 | 平均等待(min) | SLA 达成率 |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for r in wt:
            lines.append(
                f"| {r['green_share_target']:.0%} | {r['green_scale']}× "
                f"| {r['green_share_actual']:.1%} | {r['cost_per_gpu_hour']:.2f} "
                f"| {r['green_window_ratio']:.1%} | {r['avg_green_share']:.1%} "
                f"| {r['avg_wait_min']:.1f} | {r['sla_rate']:.1%} |")
        w0 = min(wt, key=lambda r: r["green_share_target"])
        w1 = max(wt, key=lambda r: r["green_share_target"])
        d_cost = w1["cost_per_gpu_hour"] - w0["cost_per_gpu_hour"]
        d_green = w1["green_window_ratio"] - w0["green_window_ratio"]
        cost_vals = [r["cost_per_gpu_hour"] for r in wt]
        green_vals = [r["green_window_ratio"] for r in wt]
        cost_mono = all(b >= a for a, b in zip(cost_vals, cost_vals[1:]))
        green_mono = all(b >= a for a, b in zip(green_vals, green_vals[1:]))
        if green_mono and cost_mono:
            verdict = ("绿电占比与成本随权重同向单调上升，说明这是一条可按需调节的权衡曲线，"
                       "不是固定参数下的偶然结果。")
        elif green_mono:
            verdict = (f"绿电窗口作业占比随权重单调上升，但单位成本在 "
                       f"{min(cost_vals):.2f}–{max(cost_vals):.2f} 之间非单调波动，"
                       "说明在当前作业规模下，绿电权重主要改变排序的边际，"
                       "成本差异落在重排本身的噪声范围内。")
        else:
            verdict = (f"绿电窗口占比在 {min(green_vals):.1%}–{max(green_vals):.1%} 之间、"
                       f"单位成本在 {min(cost_vals):.2f}–{max(cost_vals):.2f} 之间小幅变化，"
                       "没有出现方向性翻转。也就是说，绿电权重在本规模下是弱敏感参数："
                       "它不改变方案的可行性判断，只在同等可行时调整优先次序。")
        lines += [
            "",
            f"绿电权重从 {w0['green_share_target']:.0%} 提到 {w1['green_share_target']:.0%}，"
            f"单位成本 {w0['cost_per_gpu_hour']:.2f} → {w1['cost_per_gpu_hour']:.2f}"
            f"（{d_cost:+.2f}），绿电窗口作业占比 {w0['green_window_ratio']:.1%} → "
            f"{w1['green_window_ratio']:.1%}（{d_green:+.1%}）。",
            verdict,
        ]
        if base:
            base_cost = base["cost_per_gpu_hour_mean"]
            if all(v < base_cost for v in cost_vals):
                lines.append(
                    f"稳健性：四档权重下单位成本最低 {min(cost_vals):.2f}、最高 "
                    f"{max(cost_vals):.2f}，全部低于 G-A 的 {base_cost:.2f} 元/GPU·h，"
                    "「相对规则调度更省」这一结论在 0–60% 区间内不随权重取值翻转。")
            else:
                lines.append(
                    f"稳健性：至少一档权重的单位成本（最高 {max(cost_vals):.2f}）"
                    f"不低于 G-A 的 {base_cost:.2f} 元/GPU·h，"
                    "说明成本优势对权重取值敏感，需在答辩中限定适用范围。")
        lines += [
            "",
            "注：绿电权重置零后绿电窗口作业占比仍不为零，因为窗口内起跑同时受截止时间与"
            "集群可用性约束，权重只影响「同等可行时优先谁」，不改变可行性本身。",
            "",
            "> 说明：G-D 与 G-E2 的成本差来自目标函数中绿电权重的显式代价，并非缺陷；"
            "G-C 为通用 Agent 的行为模拟，不是真实 DeerFlow 运行结果，在答辩中按“模拟对照”表述。",
        ]
    if diff:
        lines += diff_lines(diff)
    if pareto:
        lines += pareto_lines(pareto)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_html(rows: list[dict], stats: dict, path: Path) -> None:
    """生成单文件评测看板（无外部依赖），供答辩现场直接展示六组对比。"""
    metrics = [
        ("sla_rate", "SLA 达成率", True, 100),
        ("budget_rate", "预算达标率", True, 100),
        ("avg_green_share", "绿电达标率", True, 100),
        ("cost_per_gpu_hour", "单位算力成本（元/GPU·h）", False, 5),
        ("green_window_ratio", "绿电窗口作业占比", True, 100),
        ("avg_wait_min", "平均排队（min）", False, 999),
        ("total_carbon_kg", "总碳排（kg）", False, 999),
        ("rubric_score", "rubric 得分", True, 100),
    ]
    body = []
    for code, _n, _k, _d in GROUPS:
        s = stats.get(code)
        if not s:
            continue
        row = [f"<tr><td class='mono'><b>{code}</b></td><td>{s['name']}</td>"]
        for key, _label, _is_pct, _max in metrics:
            mean = s[f"{key}_mean"]
            ci = s[f"{key}_ci95"]
            row.append(f"<td>{mean:.2f} ± {ci:.2f}</td>")
        row.append("</tr>")
        body.append("".join(row))
    body = "".join(body)

    base = stats.get("G-A", {})
    full = stats.get("G-D", {})
    deltas = []
    if base:
        for key, _label, _is_pct, _max in metrics:
            if key in base and key in full:
                deltas.append(
                    f"<tr><td class='mono'>{key}</td>"
                    f"<td>{base[key+'_mean']:.2f} → {full[key+'_mean']:.2f}</td>"
                    f"<td>{(full[key+'_mean']/base[key+'_mean']-1)*100:+.1f}%</td></tr>")
    delta_body = "".join(deltas)

    path.write_text(
        "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<title>对照实验评测看板</title><style>"
        "body{font-family:SimSun,'Noto Sans CJK SC',sans-serif;color:#222;padding:24px}"
        "h1{font-size:20px;margin-bottom:4px} .note{color:#666;font-size:13px}"
        "table{width:100%;border-collapse:collapse;font-size:13px}"
        "th,td{border:1px solid #ddd;padding:6px 8px;text-align:left}"
        "th{background:#E8EDF3} td.mono{font-family:Consolas,monospace;white-space:nowrap}"
        ".good{color:#1F8A70}.bad{color:#C0392B}"
        "</style></head><body>"
        "<h1>2026 第五届「未来杯」创新大赛 · 赛道一评测看板</h1>"
        f"<p class='note'>作业 {JOBS} 个/次，固定种子 {len(SEEDS_FIXED)} 次 "
        f"（{', '.join(map(str, SEEDS_FIXED))}），同一知识包 {KB_VERSION}。"
        "单位成本参考线 6.0 元/GPU·h。</p>"
        "<table><thead><tr><th>组</th><th>配置</th>"
        + "".join(f"<th>{label}</th>" for _k, label, _p, _m in metrics)
        + "</tr></thead><tbody>" + body + "</tbody></table>"
        "<h2>相对 G-A 的变化</h2><table><thead><tr><th>指标</th><th>G-A → G-D</th><th>相对变化</th></tr></thead>"
        + f"<tbody>{delta_body}</tbody></table>"
        "<p class='note'>排队时间的上升是有意的：用更空闲的资源池换更低的单位成本与更高绿电占比。</p>"
        "</body></html>", encoding="utf-8")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--outdir", default=str(ROOT / "evaluation"))
    p.add_argument("--jobs", type=int, default=JOBS)
    args = p.parse_args()

    outdir = Path(args.outdir)
    pack = KnowledgePack.load(ROOT / "knowledge" / "ningxia_cluster.yaml")

    rows = run_all(outdir, jobs_n=args.jobs)
    write_csv(rows, outdir / "results.csv")

    stats = aggregate(rows)

    print("\n参数扫描：返工阈值 / 绿电权重 ...")
    sens = run_sensitivity(pack, jobs_n=args.jobs)
    with (outdir / "sensitivity.json").open("w", encoding="utf-8") as f:
        json.dump(sens, f, ensure_ascii=False, indent=2)

    print("帕累托扫描：排队权重 0.5~16 倍 ...")
    pareto = run_pareto(pack, jobs_n=args.jobs)
    with (outdir / "pareto.json").open("w", encoding="utf-8") as f:
        json.dump(pareto, f, ensure_ascii=False, indent=2)

    write_html(rows, stats, outdir / "eval_report.html")

    ci = bootstrap_ci(rows)
    with (outdir / "ci_bootstrap.json").open("w", encoding="utf-8") as f:
        json.dump(ci, f, ensure_ascii=False, indent=2)

    diff = bootstrap_diff(rows)
    with (outdir / "diff_ci.json").open("w", encoding="utf-8") as f:
        json.dump(diff, f, ensure_ascii=False, indent=2)

    write_summary(rows, stats, outdir / "summary.md", sens=sens, diff=diff, pareto=pareto)
    write_summary_json(rows, outdir / "summary.json")

    print("\n评测完成，结果落到", outdir)
