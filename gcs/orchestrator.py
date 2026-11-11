"""编排引擎：八状态机 + DAG + 定向返工 + 预算熔断。

状态推进：
    PARSE → PLAN → SCHEDULE → EXECUTE → VERIFY ─通过─→ REPORT
                                   ↑          │
                                   └─ REWORK ←┘（不通过且预算未尽）

同一套引擎通过 Policy 切换成对照组（G-A ~ G-E），保证“同一份数据、同一份故障、
同一个评测脚本”，对照实验才站得住。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .agents import ROLE_CARDS, Executor, Profiler, Reporter, Scheduler
from .dag import Dag, build_default
from .knowledge import KnowledgePack
from .llm import BudgetExceeded, ModelGateway
from .reflect import Decision, Reflector
from .simulator import Cluster, Job, JobResult, RunMetrics
from .verifier import Verifier


@dataclass
class Policy:
    """对照组开关。改的不是代码逻辑，而是“这个团队掌握了多少领域知识”。"""
    name: str = "G-D 本方案"
    weights_mode: str = "knowledge"      # knowledge | uniform | no_green
    candidate_mode: str = "full"         # full | earliest
    max_rounds: int = 2
    rule: str | None = None              # None | fcfs | sjf
    with_evidence: bool = True
    description: str = ""


@dataclass
class Event:
    seq: int
    state: str
    node: str
    agent: str
    detail: str
    status: str = "done"

    def as_dict(self) -> dict:
        return dict(seq=self.seq, state=self.state, node=self.node, agent=self.agent,
                    detail=self.detail, status=self.status)


@dataclass
class Artifact:
    policy: str
    jobs: list[Job]
    plan: dict[str, dict]
    results: list[JobResult]
    metrics: RunMetrics
    dag: Dag
    events: list[Event] = field(default_factory=list)
    rubric_history: list[Decision] = field(default_factory=list)
    defect_history: list[dict] = field(default_factory=list)
    rounds: int = 0
    llm: dict = field(default_factory=dict)
    audit: list[dict] = field(default_factory=list)
    narrative: str = ""
    elapsed_s: float = 0.0

    def summary(self) -> dict:
        m = self.metrics.as_dict()
        return {
            "policy": self.policy,
            "rounds": self.rounds,
            "jobs": m["jobs"],
            "sla_rate": m["sla_rate"],
            "avg_wait_min": m["avg_wait_min"],
            "cost_per_gpu_hour": m["cost_per_gpu_hour"],
            "avg_green_share": m["avg_green_share"],
            "total_carbon_kg": m["total_carbon_kg"],
            "tokens": self.llm.get("tokens_total", 0),
            "final_score": self.rubric_history[-1].score if self.rubric_history else None,
        }

    def to_dict(self) -> dict:
        return {
            "policy": self.policy,
            "rounds": self.rounds,
            "dag": self.dag.to_dict(),
            "plan": self.plan,
            "results": [r.as_dict() for r in self.results],
            "metrics": self.metrics.as_dict(),
            "events": [e.as_dict() for e in self.events],
            "rubric": [d.as_dict() for d in self.rubric_history],
            "defects": self.defect_history,
            "llm": self.llm,
            "audit": self.audit,
            "narrative": self.narrative,
            "elapsed_s": round(self.elapsed_s, 3),
        }


class Orchestrator:
    def __init__(self, pack: KnowledgePack, llm: ModelGateway | None = None,
                 policy: Policy | None = None, dag: Dag | None = None):
        self.pack = pack
        self.llm = llm
        self.policy = policy or Policy()
        self.dag = dag or build_default()
        self.profiler = Profiler(pack)
        self.scheduler = Scheduler(pack, llm)
        self.executor = Executor(pack)
        self.verifier = Verifier(pack, llm)
        self.reflector = Reflector(pack)
        self.reporter = Reporter(llm)
        self.events: list[Event] = []
        self.audit: list[dict] = []

    # ------------------------------------------------------------------
    def _emit(self, state: str, node: str, agent: str, detail: str, status: str = "done") -> None:
        self.events.append(Event(len(self.events) + 1, state, node, agent, detail, status))
        if node in self.dag.nodes:
            self.dag.nodes[node].status = "done" if status == "done" else status
            self.dag.nodes[node].detail = detail
        tools = ROLE_CARDS[agent].tools if agent in ROLE_CARDS else ()
        self.audit.append({"agent": agent, "node": node, "tool": tools[0] if tools else "-",
                           "state": state})

    # ------------------------------------------------------------------
    def run(self, jobs: list[Job], cluster: Cluster | None = None) -> Artifact:
        started = time.time()
        cluster = cluster or Cluster(self.pack)
        self.dag = build_default()
        self.events, self.audit = [], []

        # ---------------- PARSE ----------------
        counts = {t: sum(1 for j in jobs if j.type == t) for t in self.pack.job_profiles}
        self._emit("PARSE", "n_parse", "A1",
                   f"解析 {len(jobs)} 个作业：" + "，".join(f"{k} {v}" for k, v in counts.items() if v))

        # ---------------- PLAN ----------------
        estimates = self.profiler.estimate(jobs)
        avg_conf = sum(e["confidence"] for e in estimates) / max(1, len(estimates))
        self._emit("PLAN", "n_profile", "A2",
                   f"生成 {len(estimates)} 条画像估计，平均置信度 {avg_conf:.2f}，方法 profile_mean")

        plan: dict[str, dict] = {}
        results: list[JobResult] = []
        metrics = RunMetrics()
        decision: Decision | None = None
        rubric_history: list[Decision] = []
        defect_history: list[dict] = []
        rounds = 0

        for rnd in range(self.policy.max_rounds + 1):
            # ---------------- SCHEDULE ----------------
            if self.policy.rule:
                plan = self._rule_plan(jobs, estimates, cluster)
                detail = f"规则策略 {self.policy.rule.upper()} 完成 {len(plan)} 个作业排程"
            else:
                only = None if rnd == 0 else set(decision.jobs_to_rework if decision else [])
                if rnd > 0 and not only:
                    break
                fresh = self.scheduler.plan(
                    jobs, estimates, cluster,
                    weights_override=self._weights(jobs),
                    only=only, candidate_mode=self.policy.candidate_mode)
                if rnd == 0:
                    plan = fresh
                else:
                    # 定向返工只覆盖责任作业，其余作业沿用上一轮方案（不整链重排）
                    plan.update(fresh)
                detail = (f"多目标求解：{len(plan)} 个作业"
                          + (f"，定向重排 {len(only)} 个责任作业" if only else ""))
            self._emit("SCHEDULE" if rnd == 0 else "REWORK",
                       "n_schedule" if rnd == 0 else "n_rework_schedule", "A3", detail)

            for chk in ("n_check_capacity", "n_check_sla", "n_check_cost"):
                self._emit("SCHEDULE" if rnd == 0 else "REWORK", chk, "A5",
                           "并行校验通过" if chk != "n_check_cost" else "成本与绿电预检完成")

            # ---------------- EXECUTE ----------------
            results, metrics = self.executor.run(jobs, plan, cluster)
            self._emit("EXECUTE", "n_execute", "A4",
                       f"提交 {len(results)} 个作业，SLA 达成 {metrics.sla_rate:.1%}，"
                       f"单位成本 ¥{metrics.cost_per_gpu_hour:.2f}/GPU·h")

            # ---------------- VERIFY ----------------
            report = self.verifier.verify(jobs, plan, results, metrics)
            self._emit("VERIFY", "n_verify", "A5",
                       f"五层校验完成，缺陷 {len(report.defects)} 条"
                       f"（阻塞 {len(report.blocking)} 条）")
            defect_history.append(report.as_dict())

            # ---------------- REFLECT ----------------
            decision = self.reflector.score(report, metrics, jobs, estimates, rnd)
            rubric_history.append(decision)
            self._emit("REFLECT", "n_reflect", "A6",
                       f"rubric {decision.score}/{decision.threshold}"
                       f"{'通过' if decision.passed else '未通过'}，"
                       f"归因到 {len(decision.target_nodes)} 个节点")

            rounds = rnd + 1
            if decision.passed or self.policy.rule:
                break
            if rnd >= self.policy.max_rounds or not decision.jobs_to_rework:
                self._emit("REFLECT", "n_reflect", "A6", "返工预算耗尽，升级人工并保留现场", "failed")
                break

            # ---------------- REWORK：只退责任节点 ----------------
            if "n_rework_profile" in decision.target_nodes:
                sub_ids = set(decision.jobs_to_rework)
                sub = [j for j in jobs if j.id in sub_ids]
                # 失败即资产：把本轮实测偏差写进经验库，再用校准后的画像重估
                self.profiler.learn([r for r in results if r.job_id in sub_ids], sub)
                est_map = {e["job_id"]: e for e in estimates}
                for e in self.profiler.estimate(sub, method="profile_mean"):
                    est_map[e["job_id"]] = e
                estimates = list(est_map.values())
                ratio = (self.profiler.history.get(sub[0].type, [1.0])[-1] if sub else 1.0)
                self._emit("REWORK", "n_rework_profile", "A2",
                           f"按实测偏差校准重估 {len(sub)} 个责任作业（约 {ratio:.2f}×，"
                           f"经验已写入画像库）")
            cluster.relocate_free(set(decision.jobs_to_rework))

        # ---------------- REPORT ----------------
        narrative = self.reporter.narrative(metrics, defect_history[-1]["defects"] if defect_history else [], rounds)
        self._emit("REPORT", "n_report", "A7",
                   f"导出指标与轨迹：{metrics.jobs} 个作业，{rounds} 轮收敛")
        self.profiler_learn_safe(results, jobs)

        llm_stats = self.llm.stats() if self.llm else {"mode": "disabled", "tokens_total": 0}
        return Artifact(
            policy=self.policy.name, jobs=jobs, plan=plan, results=results, metrics=metrics,
            dag=self.dag, events=self.events, rubric_history=rubric_history,
            defect_history=defect_history, rounds=rounds, llm=llm_stats,
            audit=self.audit, narrative=narrative, elapsed_s=time.time() - started,
        )

    # ------------------------------------------------------------------
    def _weights(self, jobs: list[Job]) -> dict[str, dict] | None:
        if self.policy.weights_mode == "knowledge":
            return None
        if self.policy.weights_mode == "uniform":
            w = {"wait": 0.4, "cost": 0.4, "green": 0.0, "carbon": 0.0, "reliability": 0.2}
            return {j.id: w for j in jobs}
        if self.policy.weights_mode == "no_green":
            out = {}
            for j in jobs:
                w = dict(self.pack.weights_for(j.type))
                g = w["green"] + w["carbon"]
                w["green"], w["carbon"] = 0.0, 0.0
                w["cost"] += g * 0.6
                w["wait"] += g * 0.4
                out[j.id] = w
            return out
        return None

    def _rule_plan(self, jobs, estimates, cluster) -> dict[str, dict]:
        est = {e["job_id"]: e for e in estimates}
        if self.policy.rule == "fcfs":
            ordered = sorted(jobs, key=lambda j: j.arrive_min)
        else:
            ordered = sorted(jobs, key=lambda j: est[j.id]["est_min"])
        plan: dict[str, dict] = {}
        for job in ordered:
            dur = max(15, est[job.id]["est_min"])
            best = None
            for pool in self.pack.pools:
                s = cluster.earliest_start(pool.id, job.arrive_min, dur, job.gpus)
                if s is not None and (best is None or s < best[1]):
                    best = (pool.id, s)
            if not best:
                continue
            pool_id, start = best
            cluster.place(pool_id, start, dur, job.gpus, job.id)
            price, segment = self.pack.price_at(pool_id, start)
            plan[job.id] = {"job_id": job.id, "pool_id": pool_id, "start_min": start,
                            "duration_min": dur, "segment": segment,
                            "rationale": f"规则策略 {self.policy.rule.upper()} 选定最早的可行资源池",
                            "score": None}
        return plan

    def profiler_learn_safe(self, results: list[JobResult], jobs: list[Job]) -> None:
        try:
            self.profiler.learn(results, jobs)
        except Exception:
            pass
