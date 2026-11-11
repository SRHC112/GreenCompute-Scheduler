"""领域知识包：把“宁夏枢纽怎么算钱、什么时候绿电多、什么作业要多少卡”变成可校验的数据。

设计要点：
1. 所有调度相关的先验信息都来自这里，代码里不留任何“经验常数”。
2. 每次修改必须升级 version，便于对照实验锁定同一版本。
3. 校验失败直接抛异常（L1 结构校验的第一道闸），不允许带病运行。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SEGMENTS = ("valley", "flat", "peak")


@dataclass(frozen=True)
class Pool:
    id: str
    city: str
    gpu: str
    gpus: int
    pue: float
    gpu_power_kw: float
    amort_per_gpu_hour: float
    price: dict[str, float]
    note: str = ""

    def price_at(self, hour: int, tariff: dict[str, Any]) -> tuple[float, str]:
        segment = segment_of(hour, tariff)
        return self.price[segment], segment


@dataclass(frozen=True)
class JobProfile:
    name: str
    gpu_hours: tuple[float, float]
    gpus: tuple[int, int]
    sla_slack: float
    budget_per_gpu_hour: float
    weights: dict[str, float]

    def weight_sum(self) -> float:
        return round(sum(self.weights.values()), 6)


@dataclass
class KnowledgePack:
    version: str
    region: str
    tariff: dict[str, Any]
    green_share_by_hour: list[float]
    grid_carbon_factor: float
    pools: list[Pool]
    job_profiles: dict[str, JobProfile]
    constraints: dict[str, float]
    rework_budget: dict[str, float]
    path: Path | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    # ---------- 查询接口 ----------
    def pool(self, pool_id: str) -> Pool:
        for p in self.pools:
            if p.id == pool_id:
                return p
        raise KeyError(f"知识包中不存在资源池 {pool_id}")

    def green_at(self, minute: int) -> float:
        return self.green_share_by_hour[(minute // 60) % 24]

    def price_at(self, pool_id: str, minute: int) -> tuple[float, str]:
        return self.pool(pool_id).price_at((minute // 60) % 24, self.tariff)

    def weights_for(self, job_type: str) -> dict[str, float]:
        return dict(self.job_profiles[job_type].weights)

    def fingerprint(self) -> str:
        """知识包指纹：对照实验里用来证明“三组用的是同一份知识”"""
        return f"{self.version}/{len(self.pools)}pools/{len(self.job_profiles)}profiles"

    # ---------- 加载与校验 ----------
    @classmethod
    def load(cls, path: str | Path) -> "KnowledgePack":
        path = Path(path)
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        pack = cls(
            version=str(data["version"]),
            region=str(data["region"]),
            tariff=data["tariff"],
            green_share_by_hour=[float(x) for x in data["green_share_by_hour"]],
            grid_carbon_factor=float(data["grid_carbon_factor"]),
            pools=[
                Pool(
                    id=p["id"],
                    city=p["city"],
                    gpu=p["gpu"],
                    gpus=int(p["gpus"]),
                    pue=float(p["pue"]),
                    gpu_power_kw=float(p["gpu_power_kw"]),
                    amort_per_gpu_hour=float(p["amort_per_gpu_hour"]),
                    price={k: float(v) for k, v in p["price"].items()},
                    note=p.get("note", ""),
                )
                for p in data["pools"]
            ],
            job_profiles={
                k: JobProfile(
                    name=k,
                    gpu_hours=tuple(v["gpu_hours"]),
                    gpus=tuple(v["gpus"]),
                    sla_slack=float(v["sla_slack"]),
                    budget_per_gpu_hour=float(v["budget_per_gpu_hour"]),
                    weights={wk: float(wv) for wk, wv in v["weights"].items()},
                )
                for k, v in data["job_profiles"].items()
            },
            constraints={k: float(v) for k, v in data["constraints"].items()},
            rework_budget={k: float(v) for k, v in data["rework_budget"].items()},
            path=path,
            raw=data,
        )
        pack.validate()
        return pack

    def validate(self) -> None:
        errors: list[str] = []
        if len(self.green_share_by_hour) != 24:
            errors.append("green_share_by_hour 必须为 24 个点")
        if not self.pools:
            errors.append("至少需要一个资源池")
        ids = [p.id for p in self.pools]
        if len(ids) != len(set(ids)):
            errors.append("资源池 id 重复")
        for p in self.pools:
            if set(p.price) != set(SEGMENTS):
                errors.append(f"{p.id} 电价缺少峰/平/谷任一档")
            if p.gpus <= 0 or p.pue < 1.0:
                errors.append(f"{p.id} 的 gpus/pue 取值不合理")
        for name, prof in self.job_profiles.items():
            if abs(prof.weight_sum() - 1.0) > 1e-6:
                errors.append(f"作业类型 {name} 的目标权重之和为 {prof.weight_sum()}，应为 1")
            if prof.gpu_hours[0] > prof.gpu_hours[1]:
                errors.append(f"作业类型 {name} 的 gpu_hours 区间反向")
        covered = set(self.tariff["valley_hours"]) | set(self.tariff["flat_hours"]) | set(
            self.tariff["peak_hours"]
        )
        if covered != set(range(24)):
            errors.append("分时电价时段未覆盖 0-23 时或存在重叠")
        if errors:
            raise ValueError("知识包校验失败：\n- " + "\n- ".join(errors))


def segment_of(hour: int, tariff: dict[str, Any]) -> str:
    hour %= 24
    if hour in tariff["valley_hours"]:
        return "valley"
    if hour in tariff["flat_hours"]:
        return "flat"
    return "peak"
