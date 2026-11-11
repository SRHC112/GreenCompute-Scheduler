"""算力模拟器（L0）：事件驱动、可复现、可注入故障。

为什么自研而不是接真实集群：
1. 评委要能一键复现，真实集群既不可得也不可复现；
2. 对照实验要求“同一份输入、同一份故障”，只有模拟器能保证；
3. 故障注入（画像失真、卡不可用）是自省闭环的触发条件，必须可控。

模拟器只负责“按计划执行并给出真实结果”，不含任何调度逻辑——
调度决策由 Agent 产生，这是“Agent 智能化而非硬编码”的边界。
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from .knowledge import KnowledgePack

HORIZON_MIN = 48 * 60
STEP_MIN = 15


@dataclass
class Job:
    id: str
    type: str
    gpus: int
    duration_est: int          # Profiler 的估计值（分钟）
    duration_actual: int       # 模拟器真值（分钟），返工不可见
    arrive_min: int
    deadline_min: int
    budget: float
    priority: int = 1

    @property
    def gpu_hours(self) -> float:
        return self.gpus * self.duration_actual / 60.0


@dataclass
class JobResult:
    job_id: str
    type: str
    pool_id: str
    start_min: int
    end_min: int
    wait_min: int
    cost: float
    power_cost: float
    amort_cost: float
    energy_kwh: float
    green_share: float
    carbon_kg: float
    segment: str
    sla_ok: bool
    budget_ok: bool
    green_ok: bool

    @property
    def ok(self) -> bool:
        return self.sla_ok and self.budget_ok and self.green_ok

    def as_dict(self) -> dict:
        d = dict(self.__dict__)
        for k in ("cost", "power_cost", "amort_cost", "energy_kwh", "green_share", "carbon_kg"):
            d[k] = round(d[k], 4)
        return d


@dataclass
class RunMetrics:
    jobs: int = 0
    sla_rate: float = 0.0
    budget_rate: float = 0.0
    green_ok_rate: float = 0.0
    avg_wait_min: float = 0.0
    p95_wait_min: float = 0.0
    total_cost: float = 0.0
    cost_per_gpu_hour: float = 0.0
    total_energy_kwh: float = 0.0
    avg_green_share: float = 0.0
    green_window_ratio: float = 0.0   # 绿电出力 ≥75% 的时段所承载的 GPU 小时占比
    total_carbon_kg: float = 0.0
    utilization: float = 0.0

    def as_dict(self) -> dict:
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in self.__dict__.items()}


class Cluster:
    """资源池占用表。以 (start, end, gpus) 区间记录，支持回滚与定向重排。"""

    def __init__(self, pack: KnowledgePack):
        self.pack = pack
        self.alloc: dict[str, list[tuple[int, int, int, str]]] = {p.id: [] for p in pack.pools}

    def fits(self, pool_id: str, start: int, duration: int, gpus: int) -> bool:
        cap = self.pack.pool(pool_id).gpus
        end = start + duration
        if start < 0 or end > HORIZON_MIN:
            return False
        used = [(s, e, g) for s, e, g, _ in self.alloc[pool_id] if s < end and e > start]
        # 计算区间内的峰值占用：切分所有端点后逐段检查
        points = sorted({start, end} | {s for s, _, _ in used} | {e for _, e, _ in used})
        for a, b in zip(points, points[1:]):
            mid = (a + b) / 2
            load = sum(g for s, e, g in used if s <= mid < e)
            if load + gpus > cap:
                return False
        return True

    def place(self, pool_id: str, start: int, duration: int, gpus: int, job_id: str) -> None:
        self.alloc[pool_id].append((start, start + duration, gpus, job_id))

    def relocate_free(self, job_ids: set[str]) -> None:
        """定向返工：只释放责任作业的占位，其余作业原地不动。"""
        for pool_id, items in self.alloc.items():
            self.alloc[pool_id] = [x for x in items if x[3] not in job_ids]

    def earliest_start(self, pool_id: str, not_before: int, duration: int, gpus: int) -> int | None:
        t = max(0, not_before)
        while t + duration <= HORIZON_MIN:
            if self.fits(pool_id, t, duration, gpus):
                return t
            t += STEP_MIN
        return None

    def utilization(self) -> float:
        total_cap = sum(p.gpus for p in self.pack.pools) * HORIZON_MIN
        used = sum((e - s) * g for items in self.alloc.values() for s, e, g, _ in items)
        return used / total_cap if total_cap else 0.0


def make_jobs(pack: KnowledgePack, n: int = 40, seed: int = 20261111,
              fault_ratio: float = 0.2) -> list[Job]:
    """按知识包的作业画像生成作业流。fault_ratio 控制在真实运行中会超时的比例。"""
    rng = random.Random(seed)
    types = list(pack.job_profiles)
    jobs: list[Job] = []
    for i in range(n):
        jtype = types[i % len(types)] if i < len(types) * 2 else rng.choice(types)
        prof = pack.job_profiles[jtype]
        gpus = rng.randrange(prof.gpus[0], prof.gpus[1] + 1, 2) or prof.gpus[0]
        want_gpu_hours = rng.uniform(*prof.gpu_hours)
        duration = max(15, int(want_gpu_hours * 60 / gpus))
        # GPU 小时以实际排定的时长为准，预算也随之推导，避免“画像值”和“执行值”口径不一致
        gpu_hours = gpus * duration / 60
        arrive = rng.randrange(0, 1200, 15)
        budget = round(gpu_hours * prof.budget_per_gpu_hour, 2)
        # 故障真值由 (seed, 作业序号) 决定，与调度决策无关，保证返工无法“改真值”
        frng = random.Random(seed * 1000 + i)
        overrun = 1.0 + frng.uniform(0.25, 0.6) if frng.random() < fault_ratio else 1.0
        actual = int(duration * overrun)
        # SLA 窗口按“真实时长”给：能不能达成，取决于调度排得够不够早，
        # 而不是取决于作业本身超出了自己都无法满足的窗口
        slack = prof.sla_slack * rng.uniform(0.92, 1.05)
        deadline = arrive + int(actual * slack) + 30
        jobs.append(Job(
            id=f"J{i + 1:03d}", type=jtype, gpus=gpus,
            duration_est=duration,
            duration_actual=actual,
            arrive_min=arrive, deadline_min=deadline, budget=budget,
            priority=3 if jtype == "infer" else (2 if jtype == "train" else 1),
        ))
    return jobs


def execute(pack: KnowledgePack, cluster: Cluster, jobs: list[Job],
            plan: dict[str, dict]) -> tuple[list[JobResult], RunMetrics]:
    """按计划执行并核算。plan: job_id -> {pool_id, start_min}"""
    results: list[JobResult] = []
    min_green = pack.constraints["min_green_share"]
    for job in jobs:
        assign = plan.get(job.id)
        if not assign:
            continue
        pool = pack.pool(assign["pool_id"])
        start = int(assign["start_min"])
        end = start + job.duration_actual
        hours = job.duration_actual / 60.0
        energy = job.gpus * pool.gpu_power_kw * pool.pue * hours
        price, segment = pack.price_at(pool.id, start)
        power_cost = energy * price
        amort_cost = job.gpus * hours * pool.amort_per_gpu_hour
        cost = power_cost + amort_cost
        # 绿电占比按作业时间窗逐小时加权
        g_sum, w_sum = 0.0, 0.0
        t = start
        while t < end:
            step = min(60 - t % 60, end - t)
            g_sum += pack.green_at(t) * step
            w_sum += step
            t += step
        green = g_sum / w_sum if w_sum else pack.green_at(start)
        results.append(JobResult(
            job_id=job.id, type=job.type, pool_id=pool.id, start_min=start, end_min=end,
            wait_min=start - job.arrive_min, cost=cost,
            power_cost=power_cost, amort_cost=amort_cost, energy_kwh=energy,
            green_share=green, carbon_kg=energy * (1 - green) * pack.grid_carbon_factor,
            segment=segment,
            sla_ok=end <= job.deadline_min,
            budget_ok=cost <= job.budget * (1 + pack.constraints.get("max_budget_overrun", 0.0)) + 1e-9,
            green_ok=green >= min_green,
        ))
    return results, aggregate(results, cluster, jobs)


def aggregate(results: list[JobResult], cluster: Cluster, jobs: list[Job]) -> RunMetrics:
    n = len(results)
    if not n:
        return RunMetrics()
    waits = sorted(r.wait_min for r in results)
    total_gpu_hours = sum(j.gpu_hours for j in jobs if j.id in {r.job_id for r in results})
    m = RunMetrics(
        jobs=n,
        sla_rate=sum(r.sla_ok for r in results) / n,
        budget_rate=sum(r.budget_ok for r in results) / n,
        green_ok_rate=sum(r.green_ok for r in results) / n,
        avg_wait_min=sum(r.wait_min for r in results) / n,
        p95_wait_min=waits[min(n - 1, math.ceil(0.95 * n))],
        total_cost=sum(r.cost for r in results),
        cost_per_gpu_hour=sum(r.cost for r in results) / total_gpu_hours if total_gpu_hours else 0.0,
        total_energy_kwh=sum(r.energy_kwh for r in results),
        avg_green_share=sum(r.green_share for r in results) / n,
        green_window_ratio=(sum(j.gpu_hours for j in jobs if j.id in
                                {r.job_id for r in results if r.green_share >= 0.75})
                            / total_gpu_hours) if total_gpu_hours else 0.0,
        total_carbon_kg=sum(r.carbon_kg for r in results),
        utilization=cluster.utilization(),
    )
    return m
