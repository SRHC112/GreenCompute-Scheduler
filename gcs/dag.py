"""DAG 构图与八状态机。

构图规则（与方案书 5.2 一致）：
- 节点四类：TASK / DECISION / CHECK / HUMAN；
- 关键路径上的 DECISION 与 CHECK 强制串行，保证可解释性不被并行打乱；
- 无数据依赖且预算不冲突的节点进入同一并行组（同一 level）；
- 返工边（reflect → profile / schedule）标记为 loop，不计入拓扑层级，
  由编排器在运行时按“定向返工”激活，只重跑责任子树。
"""

from __future__ import annotations

from dataclasses import dataclass, field

STATES = ["PARSE", "PLAN", "SCHEDULE", "EXECUTE", "VERIFY", "REFLECT", "REWORK", "REPORT"]
NODE_TYPES = ("TASK", "DECISION", "CHECK", "HUMAN")
NODE_STATUS = ("pending", "running", "done", "failed", "skipped")


@dataclass
class Node:
    id: str
    label: str
    agent: str
    type: str
    deps: list[str] = field(default_factory=list)
    loop: bool = False
    status: str = "pending"
    detail: str = ""

    def as_dict(self) -> dict:
        return dict(id=self.id, label=self.label, agent=self.agent, type=self.type,
                    deps=self.deps, loop=self.loop, status=self.status, detail=self.detail)


class Dag:
    def __init__(self) -> None:
        self.nodes: dict[str, Node] = {}
        self.order: list[str] = []

    def add(self, node: Node) -> Node:
        if node.type not in NODE_TYPES:
            raise ValueError(f"非法节点类型 {node.type}")
        for dep in node.deps:
            if dep not in self.nodes:
                raise ValueError(f"节点 {node.id} 依赖了不存在的 {dep}")
        self.nodes[node.id] = node
        self.order.append(node.id)
        return node

    def forward_deps(self, node_id: str) -> list[str]:
        return [n for n in self.nodes[node_id].deps if not self.nodes[n].loop]

    def levels(self) -> list[list[str]]:
        """并行组：同一层内节点无依赖关系，可并发执行。"""
        level: dict[str, int] = {}

        def depth(nid: str, seen: tuple[str, ...] = ()) -> int:
            if nid in level:
                return level[nid]
            if nid in seen:
                raise ValueError(f"DAG 存在环：{seen + (nid,)}")
            deps = self.forward_deps(nid)
            d = 0 if not deps else 1 + max(depth(x, seen + (nid,)) for x in deps)
            level[nid] = d
            return d

        for nid in self.order:
            depth(nid)
        groups: dict[int, list[str]] = {}
        for nid, d in level.items():
            groups.setdefault(d, []).append(nid)
        return [groups[k] for k in sorted(groups)]

    def downstream(self, node_id: str) -> set[str]:
        out, frontier = set(), [node_id]
        while frontier:
            cur = frontier.pop()
            for nid, node in self.nodes.items():
                if cur in node.deps and nid not in out:
                    out.add(nid)
                    frontier.append(nid)
        return out

    def to_dict(self) -> dict:
        return {
            "nodes": [n.as_dict() for n in self.nodes.values()],
            "levels": self.levels(),
        }


def build_default() -> Dag:
    """默认编排图：一次调度任务的完整生命周期。"""
    g = Dag()
    g.add(Node("n_parse", "任务解析", "A1", "TASK", []))
    g.add(Node("n_profile", "资源画像与时长估计", "A2", "TASK", ["n_parse"]))
    g.add(Node("n_schedule", "多目标调度决策", "A3", "DECISION", ["n_profile"]))
    g.add(Node("n_check_capacity", "资源可行性校验", "A5", "CHECK", ["n_schedule"]))
    g.add(Node("n_check_sla", "SLA 校验", "A5", "CHECK", ["n_schedule"]))
    g.add(Node("n_check_cost", "成本与绿电校验", "A5", "CHECK", ["n_schedule"]))
    g.add(Node("n_execute", "提交与执行监控", "A4", "TASK",
               ["n_check_capacity", "n_check_sla", "n_check_cost"]))
    g.add(Node("n_verify", "五层结果验证", "A5", "DECISION", ["n_execute"]))
    g.add(Node("n_reflect", "评分与归因", "A6", "DECISION", ["n_verify"]))
    g.add(Node("n_report", "报告与复盘归档", "A7", "TASK", ["n_verify"]))
    # 返工边：只在验证不通过时激活，目标由 Reflector 指定，不是固定回滚到起点
    g.add(Node("n_rework_profile", "定向返工：保守重估", "A2", "TASK",
               ["n_reflect"], loop=True))
    g.add(Node("n_rework_schedule", "定向返工：重排决策", "A3", "TASK",
               ["n_reflect"], loop=True))
    return g
