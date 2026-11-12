"""rubric 评分与定向返工。

关键设计：返工不是“整链重跑”，而是
    缺陷 → 归因到责任节点 → 只重跑该节点的子树 → 其余节点保持不动。
这样单次失败只多耗一个节点的 token，而不是整个链路。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .knowledge import KnowledgePack
from .simulator import Job, RunMetrics
from .verifier import Defect, VerifyReport

RUBRIC = [
    ("硬约束满足", 30, "L3", "违反 SLA / 预算 / 绿电下限"),
    ("数值与口径一致性", 20, "L2", "成本不可复算、单位或口径不一致"),
    ("证据强度", 20, "L4", "决策缺少可追溯依据"),
    ("任务覆盖度", 15, "L1", "存在未调度作业"),
    ("结构合规", 10, "L1", "字段缺失、结果缺失"),
    ("成本效率", 5, "ADV", "单位算力成本偏高（只提示，不阻塞）"),
]

# 缺陷类型 → 责任节点（定向返工的目标）
NODE_OF_CODE = {
    "L1-未调度": "n_schedule",
    "L1-字段缺失": "n_schedule",
    "L1-结果缺失": "n_execute",
    "L2-成本不可复算": "n_execute",
    "L2-时长非法": "n_rework_profile",
    "L3-SLA 未达成": "n_rework_schedule",
    "L3-成本超预算": "n_rework_schedule",
    "L3-绿电未达标": "n_rework_schedule",
    "L4-引用不可达": "n_schedule",
    "L4-依据缺失": "n_schedule",
    "L4-评分缺失": "n_schedule",
    "L5-依据不自洽": "n_schedule",
}


@dataclass
class Dimension:
    """rubric 的一个评分维度：名称、权重、是否通过、未通过时的说明。"""

    name: str
    weight: int
    passed: bool
    reason: str

    def as_dict(self) -> dict:
        """转成看板 rubric 条形图需要的字段。"""
        return dict(name=self.name, weight=self.weight, passed=self.passed, reason=self.reason)


@dataclass
class Decision:
    """一轮自省评分的结果：总分、是否过阈值，以及未通过时的返工指令与目标节点。"""

    score: int
    passed: bool
    threshold: int
    dims: list[Dimension] = field(default_factory=list)
    directives: list[dict] = field(default_factory=list)
    jobs_to_rework: list[str] = field(default_factory=list)
    target_nodes: list[str] = field(default_factory=list)
    strategy: str = "none"
    command: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        """转成看板与 API 需要的完整评分记录，保留归因与返工指令以便事后追溯。"""
        return dict(score=self.score, passed=self.passed, threshold=self.threshold,
                    dims=[d.as_dict() for d in self.dims], directives=self.directives,
                    jobs_to_rework=self.jobs_to_rework, target_nodes=self.target_nodes,
                    strategy=self.strategy, command=self.command)


class Reflector:
    """rubric 评分与定向返工：把缺陷归因到责任节点，只重跑该子树，其余节点复用结果。"""

    def __init__(self, pack: KnowledgePack, threshold: int | None = None):
        """阈值默认取知识包的 pass_score；显式传入用于敏感性分析（同一份数据扫 60/70/80/90/95）。"""
        self.pack = pack
        self.threshold = int(threshold if threshold is not None
                             else pack.rework_budget["pass_score"])

    def score(self, report: VerifyReport, metrics: RunMetrics, jobs: list[Job],
              estimates: list[dict], round_index: int) -> Decision:
        """按六维 rubric 打分；未达阈值时把阻塞缺陷归因到责任节点并生成定向返工指令。"""
        blocking = report.blocking
        by_layer: dict[str, list[Defect]] = {}
        for d in blocking:
            by_layer.setdefault(d.layer, []).append(d)

        dims: list[Dimension] = []
        for name, weight, layer, desc in RUBRIC:
            if layer == "ADV":
                ratio = self._cost_ratio(metrics)
                ok = ratio <= 1.0
                dims.append(Dimension(name, weight, ok,
                                      f"单位算力成本 ¥{metrics.cost_per_gpu_hour:.2f}/GPU·h"))
                continue
            items = by_layer.get(layer, [])
            note = ""
            if layer == "L4":
                # 置信度偏低只降“证据强度”的说明，不作为阻塞项：证据缺失才是缺陷
                weak = [e for e in estimates if e["confidence"] < 0.5]
                if weak:
                    note = f"（另有 {len(weak)} 条估计置信度低于 0.5）"
            ok = not items
            dims.append(Dimension(name, weight, ok,
                                  ("通过" if ok else f"{len(items)} 条：{desc}") + note))

        score = sum(d.weight for d in dims if d.passed)
        decision = Decision(score=score, passed=score >= self.threshold, threshold=self.threshold,
                            dims=dims)
        if decision.passed:
            return decision

        # ---- 归因：把缺陷映射到责任节点，只收夹阻塞项 ----
        directives: list[dict] = []
        jobs_to_rework: set[str] = set()
        for d in blocking:
            node = d.node or NODE_OF_CODE.get(d.code, "n_schedule")
            directives.append({
                "defect_id": f"CHK-{len(directives) + 1:02d}-{d.code}",
                "owner_agent": d.owner,
                "target_node": node,
                "job_id": d.job_id,
                "evidence": d.evidence,
                "action": d.action,
            })
            if d.job_id:
                jobs_to_rework.add(d.job_id)
        nodes = sorted({x["target_node"] for x in directives})
        strategy = ("reprofile_and_reschedule"
                    if any(n == "n_rework_profile" for n in nodes) else "reschedule_only")
        remaining = max(0, int(self.pack.rework_budget["max_rounds"]) - round_index)
        command = {
            "round": round_index,
            "strategy": strategy,
            "target_nodes": nodes,
            "jobs": sorted(jobs_to_rework),
            "budget_remaining": {
                "rounds": remaining,
                "tokens": int(self.pack.rework_budget["max_tokens"]),
                "seconds": int(self.pack.rework_budget["max_seconds"]),
            },
            "note": "只重跑 target_nodes 及其下游，其余节点复用上一轮结果",
        }
        decision.directives = directives
        decision.jobs_to_rework = sorted(jobs_to_rework)
        decision.target_nodes = nodes
        decision.strategy = strategy
        decision.command = command
        return decision

    @staticmethod
    def _cost_ratio(metrics: RunMetrics) -> float:
        """单位成本相对 6.0 元/GPU·h 参考线的倍率，只用于「成本效率」这一非阻塞维度。

        只提示不阻塞：成本高说明可优化，但不代表方案不可用，不该因此触发返工。
        """
        return metrics.cost_per_gpu_hour / 6.0 if metrics.cost_per_gpu_hour else 0.0
