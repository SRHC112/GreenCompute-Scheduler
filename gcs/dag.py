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

NODE_TYPES = ("TASK", "DECISION", "CHECK", "HUMAN")


@dataclass
class Node:
    """DAG 的一个节点：标识、标签、负责角色、类型（TASK/DECISION/CHECK/HUMAN）、
    前置依赖、是否为返工环（loop）、运行状态与明细。"""
    id: str
    label: str
    agent: str
    type: str
    deps: list[str] = field(default_factory=list)
    loop: bool = False
    status: str = "pending"
    detail: str = ""

    def as_dict(self) -> dict:
        """转成前端画 DAG 所需的字段集（依赖、是否返工环、运行状态）。"""
        return dict(id=self.id, label=self.label, agent=self.agent, type=self.type,
                    deps=self.deps, loop=self.loop, status=self.status, detail=self.detail)


class Dag:
    """有向无环图：节点字典 + 插入顺序。循环边（loop）不参与拓扑层级计算，仅在运行时按
    “定向返工”激活。"""

    def __init__(self) -> None:
        """空图；节点按插入顺序记录，保证渲染结果稳定可复现。"""
        self.nodes: dict[str, Node] = {}
        self.order: list[str] = []

    def add(self, node: Node) -> Node:
        """加入节点并校验：类型必须合法，前置依赖必须已存在。"""
        if node.type not in NODE_TYPES:
            raise ValueError(f"非法节点类型 {node.type}")
        for dep in node.deps:
            if dep not in self.nodes:
                raise ValueError(f"节点 {node.id} 依赖了不存在的 {dep}")
        self.nodes[node.id] = node
        self.order.append(node.id)
        return node

    def forward_deps(self, node_id: str) -> list[str]:
        """该节点的非循环前置依赖（排除标记为 loop 的返工边）。"""
        return [n for n in self.nodes[node_id].deps if not self.nodes[n].loop]

    def levels(self) -> list[list[str]]:
        """并行组：同一层内节点无依赖关系，可并发执行。"""
        level: dict[str, int] = {}

        def depth(nid: str, seen: tuple[str, ...] = ()) -> int:
            """递归求节点层级；seen 用于检测环，出现回边直接报错而不是死循环。"""
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
        """节点级拓扑工具：返回（间接）依赖该节点的前向节点集合，不含本身。

        P1-2.2 / FR-12 定性：只沿主链传播，标记为 loop 的返工节点不计入结果，
        与 forward_deps() / levels() 的「返工边不参与拓扑层级」约定一致。

        注意：本方法是**节点级**拓扑工具，不参与编排器的返工判定。FR-12
        「缺陷仅退责任子树」由 orchestrator.run 的**作业级**
        ``only=jobs_to_rework`` + ``plan.update(fresh)`` 实现，二者粒度不同；
        用本方法的返回值替换返工作业集合会演变成整链重跑，违反 FR-12。
        """
        out, frontier = set(), [node_id]
        while frontier:
            cur = frontier.pop()
            for nid, node in self.nodes.items():
                if cur in node.deps and not node.loop and nid not in out:
                    out.add(nid)
                    frontier.append(nid)
        return out

    def to_dict(self) -> dict:
        """序列化为前端可直接消费的字典：nodes + levels（并行层级）。"""
        return {
            "nodes": [n.as_dict() for n in self.nodes.values()],
            "levels": self.levels(),
        }


def build_default() -> Dag:
    """构造默认编排图：一次调度任务的完整生命周期（PARSE→PLAN→SCHEDULE→EXECUTE→VERIFY→REFLECT，
    不通过则定向返工 REWORK，最后 REPORT）。"""
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
