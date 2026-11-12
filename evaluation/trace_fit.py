"""参数对齐校验：把知识包里的关键参数与公开口径逐条对照。

为什么要做这件事
----------------
调度器里的每个先验（峰谷时段、电价、排放因子、绿电曲线）都直接决定指标数值。
如果这些数字是拍脑袋写的，那么所有实验结论都只是「自说自话」。
本脚本把可公开核对的部分固化成可执行断言：

  1. 分时电价时段 —— 对照宁夏 2024 年一般工商业分时电价口径；
  2. 峰谷比 —— 对照「峰 = 平 × 1.6，谷 = 平 × 0.6」的公开浮动规则；
  3. 排放因子 —— 对照生态环境部公告〔2024〕第 10 号公布的西北区域电网因子；
  4. 绿电曲线 —— 与由公开装机结构重构的参照曲线做形状拟合（MAE / RMSE / R² / 相关系数）。

用法：
    python evaluation/trace_fit.py
输出：
    evaluation/trace_fit.json   机器可读的校验结果
退出码：0 表示全部硬性断言通过，1 表示存在不一致（可用于 CI 卡口）。
"""

from __future__ import annotations

import json
import math
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gcs.knowledge import KnowledgePack, segment_of  # noqa: E402

OUT = ROOT / "evaluation" / "trace_fit.json"
TRACES = ROOT / "evaluation" / "traces"

# ---------------------------------------------------------------- 公开参照量

# 宁夏 2024 年一般工商业分时电价时段（来源：宁夏发改委 / 国网宁夏电力公开口径）
#   峰段 8:00-11:30、18:00-21:00
#   谷段 23:00-次日 7:00
#   平段 7:00-8:00、11:30-18:00、21:00-23:00
# 边界落在半点，按“起始小时所属时段”归类。
PUBLIC_TARIFF: dict[int, str] = {
    **{h: "valley" for h in (23, 0, 1, 2, 3, 4, 5, 6)},
    **{h: "peak" for h in (8, 9, 10, 11, 18, 19, 20)},
    **{h: "flat" for h in (7, 12, 13, 14, 15, 16, 17, 21, 22)},
}

# 峰谷浮动规则：一般工商业峰段上浮 60%、谷段下浮 40%
PUBLIC_PEAK_RATIO = 1.6
PUBLIC_VALLEY_RATIO = 0.6
RATIO_TOL = 0.02          # 允许 2% 的取整误差

# 生态环境部《关于发布2023年电力二氧化碳排放因子的公告》（公告〔2024〕第10号）
PUBLIC_CARBON_NW_GRID = 0.6773
PUBLIC_CARBON_NATIONAL = 0.5366

# 宁夏 2024 年底装机结构（来源：宁夏回族自治区 2024 年国民经济和社会发展统计公报）
PUBLIC_CAPACITY_SHARE = {"光伏": 0.160, "风电": 0.120, "水电": 0.079,
                          "生物质等": 0.029, "煤电": 0.612}
PUBLIC_RENEWABLE_WEIGHT_2024 = 0.35   # 可再生能源电力消纳责任权重（发改办能〔2024〕718号）

# 参照曲线的重构假设（明示，不隐藏）：光伏日内用半正弦钟形，风电日内用夜间高、午间低的余弦形；
# 权重取上面的公开装机占比。重构结果是「形态参照」，不是官方实测出力。
PV_PEAK_HOUR = 12.0
PV_RISE_HOUR = 6.0
PV_SET_HOUR = 19.0
WIND_PEAK_HOUR = 2.0


# ---------------------------------------------------------------- 计算工具

def reference_green_shape() -> list[float]:
    """按公开装机结构重构绿电出力日内形态（归一化到峰值 1）。"""
    pv_cap = PUBLIC_CAPACITY_SHARE["光伏"]
    wind_cap = PUBLIC_CAPACITY_SHARE["风电"]
    other = 1.0 - pv_cap - wind_cap      # 常规电源提供恒定基荷
    vals = []
    for h in range(24):
        # 光伏：日出到日落的半正弦
        if PV_RISE_HOUR <= h < PV_SET_HOUR:
            pv = math.sin(math.pi * (h - PV_RISE_HOUR) / (PV_SET_HOUR - PV_RISE_HOUR))
        else:
            pv = 0.0
        # 风电：夜间高、午间低
        wind = 0.5 * (1.0 + math.cos(2 * math.pi * (h - WIND_PEAK_HOUR) / 24))
        # 绿电出力占比 = (光伏 + 风电) / (总出力)，总出力按基荷 + 绿电峰值标定
        vals.append((pv_cap * pv + wind_cap * wind) / (other + pv_cap + wind_cap))
    m = max(vals) or 1.0
    return [v / m for v in vals]


def pearson(xs: list[float], ys: list[float]) -> float:
    """皮尔逊相关系数；序列方差为 0 时返回 0（无可判定关系）。"""
    if len(xs) != len(ys) or len(xs) < 2:
        return 0.0
    mx, my = statistics.mean(xs), statistics.mean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    return num / (dx * dy) if dx and dy else 0.0


def r2(xs: list[float], ys: list[float]) -> float:
    """决定系数 R²（以参照曲线为基准解释知识包曲线的变异）。"""
    my = statistics.mean(ys)
    ss_tot = sum((y - my) ** 2 for y in ys)
    if ss_tot == 0:
        return 0.0
    ss_res = sum((x - y) ** 2 for x, y in zip(xs, ys))
    return 1.0 - ss_res / ss_tot


# ---------------------------------------------------------------- 校验项

def check_tariff(pack: KnowledgePack) -> dict:
    """逐小时比对分时电价时段是否与公开口径一致。"""
    mismatch = []
    for h in range(24):
        got = segment_of(h, pack.tariff)
        want = PUBLIC_TARIFF[h]
        if got != want:
            mismatch.append({"hour": h, "pack": got, "public": want})
    return {
        "name": "分时电价时段对齐",
        "source": "宁夏 2024 年一般工商业分时电价口径",
        "checked_hours": 24,
        "mismatches": mismatch,
        "passed": not mismatch,
    }


def check_ratio(pack: KnowledgePack) -> dict:
    """核对每个资源池的峰谷比是否符合公开浮动规则。"""
    detail = []
    ok = True
    for p in pack.pools:
        flat = p.price["flat"]
        peak_ratio = p.price["peak"] / flat if flat else 0.0
        valley_ratio = p.price["valley"] / flat if flat else 0.0
        good = (abs(peak_ratio - PUBLIC_PEAK_RATIO) <= RATIO_TOL
                and abs(valley_ratio - PUBLIC_VALLEY_RATIO) <= RATIO_TOL)
        ok = ok and good
        detail.append({"pool": p.id, "peak_ratio": round(peak_ratio, 4),
                       "valley_ratio": round(valley_ratio, 4), "ok": good})
    return {
        "name": "峰谷浮动比例核对",
        "source": "峰 = 平 × 1.6、谷 = 平 × 0.6（宁夏一般工商业公开规则）",
        "pools": detail,
        "passed": ok,
    }


def check_carbon(pack: KnowledgePack) -> dict:
    """核对排放因子是否等于公开发布的西北区域电网因子。"""
    got = pack.grid_carbon_factor
    passed = abs(got - PUBLIC_CARBON_NW_GRID) < 1e-9
    return {
        "name": "电网排放因子核对",
        "source": "生态环境部公告〔2024〕第10号（2023 年西北区域电网 0.6773）",
        "pack": got,
        "public_nw_grid": PUBLIC_CARBON_NW_GRID,
        "public_national": PUBLIC_CARBON_NATIONAL,
        "passed": passed,
    }


def check_green(pack: KnowledgePack) -> dict:
    """把知识包绿电曲线与参照曲线做形状拟合，并对照公开口径说明水平差异。"""
    pack_curve = list(pack.green_share_by_hour)
    ref_curve = reference_green_shape()
    # 形状比较：双方都归一化到各自均值，只看日内形态
    pm = statistics.mean(pack_curve) or 1.0
    rm = statistics.mean(ref_curve) or 1.0
    pn = [v / pm for v in pack_curve]
    rn = [v / rm for v in ref_curve]
    mae = statistics.mean(abs(a - b) for a, b in zip(pn, rn))
    rmse = math.sqrt(statistics.mean((a - b) ** 2 for a, b in zip(pn, rn)))
    corr = pearson(pn, rn)
    fit_r2 = r2(rn, pn)
    mean_level = statistics.mean(pack_curve)
    wind_pv = PUBLIC_CAPACITY_SHARE["光伏"] + PUBLIC_CAPACITY_SHARE["风电"]
    return {
        "name": "绿电曲线与参照曲线拟合",
        "source": "参照曲线由宁夏 2024 年公开装机结构重构（形态参照，非官方实测出力）",
        "mae_normalized": round(mae, 4),
        "rmse_normalized": round(rmse, 4),
        "pearson_r": round(corr, 4),
        "r2": round(fit_r2, 4),
        "pack_mean_level": round(mean_level, 4),
        "public_wind_pv_capacity_share": wind_pv,
        "public_renewable_weight_2024": PUBLIC_RENEWABLE_WEIGHT_2024,
        "level_note": ("知识包曲线是「绿电消费占比」口径（含绿证与跨区绿电交易），"
                       "水平（%s）高于风光装机占比（%s）与"
                       "消纳责任权重（%s），三者口径不同、不可直接比较；"
                       "接入实测购电曲线后应替换本项。"
                       ) % (f"{mean_level:.0%}", f"{wind_pv:.0%}",
                            f"{PUBLIC_RENEWABLE_WEIGHT_2024:.0%}"),
        "passed": corr >= 0.85,
    }


def main() -> int:
    pack = KnowledgePack.load(ROOT / "knowledge" / "ningxia_cluster.yaml")
    checks = [check_tariff(pack), check_ratio(pack), check_carbon(pack), check_green(pack)]

    # 落一份参照曲线，便于人工比对与外部替换
    TRACES.mkdir(parents=True, exist_ok=True)
    ref = reference_green_shape()
    with (TRACES / "reference_green_shape.csv").open("w", encoding="utf-8") as f:
        f.write("hour,pack_green_share,reference_shape\n")
        for h in range(24):
            f.write(f"{h},{pack.green_share_by_hour[h]:.3f},{ref[h]:.4f}\n")

    payload = {
        "pack_version": pack.version,
        "checks": checks,
        "all_passed": all(c["passed"] for c in checks),
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"知识包版本：{pack.version}")
    for c in checks:
        flag = "通过" if c["passed"] else "不通过"
        extra = ""
        if c["name"].startswith("绿电曲线"):
            extra = ("（MAE {mae:.3f}，RMSE {rmse:.3f}，"
                     "相关系数 {corr:.3f}，R2 {r2:.3f}）").format(
                mae=c["mae_normalized"], rmse=c["rmse_normalized"],
                corr=c["pearson_r"], r2=c["r2"])
        if c["name"].startswith("分时电价"):
            extra = "（不一致小时数：%d）" % len(c["mismatches"])
        print(f"[{flag}] {c['name']}{extra}")
        print(f"        来源：{c['source']}")
    print("结果已写入", OUT)
    return 0 if payload["all_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
