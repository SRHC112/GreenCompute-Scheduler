"""五层验证器：能用规则判定的，绝不交给模型判定。

L1 结构     schema、字段齐全与类型           确定性
L2 数值     可复算、单位与口径一致           确定性
L3 约束     SLA / 成本 / 资源                确定性
L4 可达性   引用与依据快照存在               确定性
L5 语义     结论与依据是否匹配               Judge（离线时退化为规则）

输出不是“通过/不通过”，而是**可归因的缺陷清单**：
每条缺陷都带 owner（责任角色）、node（责任节点）与 evidence，
这是“定向返工”能只退一个节点的前提。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .knowledge import KnowledgePack
from .llm import ModelGateway
from .simulator import Job, JobResult, RunMetrics

REQUIRED_PLAN_FIELDS = ("pool_id", "start_min")


@dataclass
class Defect:
    code: str
    layer: str
    owner: str
    node: str
    detail: str
    evidence: str
    job_id: str | None = None
    blocking: bool = True
    action: str = ""

    def as_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class VerifyReport:
    defects: list[Defect] = field(default_factory=list)
    checked: int = 0

    @property
    def blocking(self) -> list[Defect]:
        return [d for d in self.defects if d.blocking]

    def by_layer(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for d in self.defects:
            out[d.layer] = out.get(d.layer, 0) + 1
        return out

    def as_dict(self) -> dict:
        return {"checked": self.checked, "count": len(self.defects),
                "by_layer": self.by_layer(),
                "defects": [d.as_dict() for d in self.defects]}


class Verifier:
    def __init__(self, pack: KnowledgePack, llm: ModelGateway | None = None):
        self.pack = pack
        self.llm = llm

    def verify(self, jobs: list[Job], plan: dict[str, dict], results: list[JobResult],
               metrics: RunMetrics) -> VerifyReport:
        rep = VerifyReport()
        self._l1_structure(jobs, plan, results, rep)
        self._l2_numbers(jobs, plan, results, rep)
        self._l3_constraints(jobs, results, rep)
        self._l4_reachability(plan, rep)
        self._l5_semantics(jobs, plan, rep)
        rep.checked = len(jobs)
        return rep

    # ---------------- L1 ----------------
    def _l1_structure(self, jobs, plan, results, rep: VerifyReport) -> None:
        scheduled = {r.job_id for r in results}
        for job in jobs:
            if job.id not in plan:
                rep.defects.append(Defect(
                    "L1-未调度", "L1", "A3", "n_schedule",
                    f"作业 {job.id}（{job.type}）没有产生调度方案",
                    f"plan 中缺失 {job.id}", job.id,
                    action="补充候选池并重新求解，或标为不可调度并升级人工"))
                continue
            for f in REQUIRED_PLAN_FIELDS:
                if f not in plan[job.id]:
                    rep.defects.append(Defect(
                        "L1-字段缺失", "L1", "A3", "n_schedule",
                        f"作业 {job.id} 的方案缺少字段 {f}", f"plan[{job.id}]={plan[job.id]}",
                        job.id, action="补齐字段后重新校验"))
            if job.id not in scheduled:
                rep.defects.append(Defect(
                    "L1-结果缺失", "L1", "A4", "n_execute",
                    f"作业 {job.id} 已排程但无执行结果", "results 中无该作业", job.id,
                    action="检查执行器提交链路"))

    # ---------------- L2 ----------------
    def _l2_numbers(self, jobs, plan, results, rep: VerifyReport) -> None:
        by_id = {j.id: j for j in jobs}
        for r in results:
            job = by_id.get(r.job_id)
            if not job:
                continue
            pool = self.pack.pool(r.pool_id)
            price, _ = self.pack.price_at(r.pool_id, r.start_min)
            hours = (r.end_min - r.start_min) / 60
            expect = (job.gpus * pool.gpu_power_kw * pool.pue * hours * price
                      + job.gpus * hours * pool.amort_per_gpu_hour)
            if abs(expect - r.cost) > max(0.5, 0.005 * expect):
                rep.defects.append(Defect(
                    "L2-成本不可复算", "L2", "A4", "n_execute",
                    f"{r.job_id} 成本 {r.cost:.2f} 与复算值 {expect:.2f} 不一致",
                    f"gpus={job.gpus} pue={pool.pue} price={price}", r.job_id,
                    action="以复算值为准并回写"))
            if r.end_min <= r.start_min:
                rep.defects.append(Defect(
                    "L2-时长非法", "L2", "A2", "n_rework_profile",
                    f"{r.job_id} 结束时刻不晚于开始时刻",
                    f"{r.start_min}→{r.end_min}", r.job_id,
                    action="重估运行时长"))

    # ---------------- L3 ----------------
    def _l3_constraints(self, jobs, results, rep: VerifyReport) -> None:
        by_id = {j.id: j for j in jobs}
        for r in results:
            job = by_id.get(r.job_id)
            if not job:
                continue
            if not r.sla_ok:
                # 归因决定返工目标：是“估错了”还是“排错了”，处理方式完全不同
                bias = job.duration_actual / max(1, job.duration_est)
                if bias > 1.2:
                    owner, node = "A2", "n_rework_profile"
                    action = "按保守时长重新估计并重排该作业"
                    evidence = (f"估计 {job.duration_est}min / 实际 {job.duration_actual}min，"
                                f"偏差 {bias - 1:+.0%}")
                else:
                    owner, node = "A3", "n_rework_schedule"
                    action = "迁到更空闲资源池或提前启动"
                    evidence = (f"估计 {job.duration_est}min / 实际 {job.duration_actual}min，"
                                f"排队 {r.wait_min}min，属排程问题")
                rep.defects.append(Defect(
                    "L3-SLA 未达成", "L3", owner, node,
                    f"{r.job_id} 实际完成 {r.end_min}min，超出截止 {job.deadline_min}min",
                    evidence, r.job_id, action=action))
            if not r.budget_ok:
                tol = self.pack.constraints.get("max_budget_overrun", 0.0)
                # 成本超预算有两种根因：估错了（时长低估）或排错了（选贵池/峰段）
                hours_est = job.duration_est / 60
                hours_act = (r.end_min - r.start_min) / 60
                bias = hours_act / max(1e-6, hours_est)
                if bias > 1.2:
                    owner, node = "A2", "n_rework_profile"
                    action = "按实测偏差重估时长，并迁到低 PUE 池"
                    evidence = (f"实际占用 {hours_act:.2f}h 高于估计 {hours_est:.2f}h（{bias - 1:+.0%}），"
                                f"电费 {r.power_cost:.1f} + 折旧 {r.amort_cost:.1f}")
                else:
                    owner, node = "A3", "n_rework_schedule"
                    action = "迁到谷段或低 PUE 池"
                    evidence = (f"电费 {r.power_cost:.1f} + 折旧 {r.amort_cost:.1f}，"
                                f"时长估计无偏差，属选池/选时段问题")
                rep.defects.append(Defect(
                    "L3-成本超预算", "L3", owner, node,
                    f"{r.job_id} 成本 ¥{r.cost:.2f} 超预算 ¥{job.budget:.2f}",
                    evidence, r.job_id, action=action))
            if not r.green_ok:
                rep.defects.append(Defect(
                    "L3-绿电未达标", "L3", "A3", "n_rework_schedule",
                    f"{r.job_id} 绿电占比 {r.green_share:.0%} 低于下限 "
                    f"{self.pack.constraints['min_green_share']:.0%}",
                    f"时间窗 {r.start_min}→{r.end_min}", r.job_id,
                    action="平移到绿电出力窗口"))

    # ---------------- L4 ----------------
    def _l4_reachability(self, plan, rep: VerifyReport) -> None:
        valid_pools = {p.id for p in self.pack.pools}
        for job_id, p in plan.items():
            if p["pool_id"] not in valid_pools:
                rep.defects.append(Defect(
                    "L4-引用不可达", "L4", "A3", "n_schedule",
                    f"{job_id} 引用了知识包中不存在的资源池 {p['pool_id']}",
                    f"知识包版本 {self.pack.version}", job_id,
                    action="以知识包为准重新选择"))
            if not p.get("rationale"):
                rep.defects.append(Defect(
                    "L4-依据缺失", "L4", "A3", "n_schedule",
                    f"{job_id} 的决策没有对照依据，事后无法复核",
                    "rationale 为空", job_id,
                    action="补生成「因 A 排除 X、因 B 选定 Y」的对照说明"))
            if p.get("score") is None:
                rep.defects.append(Defect(
                    "L4-评分缺失", "L4", "A3", "n_schedule",
                    f"{job_id} 缺少目标函数得分，无法复核权衡过程",
                    "score 为空", job_id, blocking=False,
                    action="记录候选得分表"))

    # ---------------- L5 ----------------
    def _l5_semantics(self, jobs, plan, rep: VerifyReport) -> None:
        """语义层：结论与依据是否匹配。有模型时用 Judge，离线时用规则。"""
        for job_id, p in plan.items():
            text = str(p.get("rationale", ""))
            if not text or len(text) < 8:
                continue
            if self.llm and self.llm.online:
                verdict = self.llm.chat(
                    "你是调度决策审核员。判断给定决策依据与选定结果是否自洽，只回答 PASS 或 FAIL。",
                    f"决策：{p['pool_id']} @ {p['start_min']}；依据：{text}")
                ok = "PASS" in verdict.upper()
            else:
                # 规则兜底：依据必须同时给出“选定”与至少一条排除理由
                ok = ("选定" in text) and ("排除" in text)
            if not ok:
                rep.defects.append(Defect(
                    "L5-依据不自洽", "L5", "A3", "n_schedule",
                    f"{job_id} 的结论与依据不匹配，存在“先决定后编理由”的风险",
                    text[:120], job_id, blocking=False,
                    action="重新生成依据，或升级人工复核"))
