"""七个角色：各有角色卡、工具白名单与失败出口。

边界纪律（答辩时最容易被追问的点）：
- Agent 只做“读知识包 → 生成结构化决策”，数值一律可复算；
- 模型只产出归因叙述，不产出数字，避免 LLM 幻觉污染指标；
- 每个角色只拿到自己需要的工具，工具网关记审计日志（见 orchestrator.audit）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .knowledge import KnowledgePack
from .llm import ModelGateway
from .simulator import Cluster, Job, JobResult, RunMetrics, execute


@dataclass(frozen=True)
class RoleCard:
    """一张角色卡：定义某个 Agent 的身份、职责、可用工具与失败时的出口策略。"""
    id: str
    name: str
    duty: str
    tools: tuple[str, ...]
    inputs: str
    outputs: str
    failure_exit: str


ROLE_CARDS: dict[str, RoleCard] = {
    "A1": RoleCard("A1", "Orchestrator", "任务解析、DAG 拆解、并行判定、预算分配",
                   ("parse_task", "build_dag", "alloc_budget"),
                   "自然语言/结构化作业请求", "任务清单 + DAG + 预算",
                   "请求不完整 → 生成澄清项，挂起等待人工"),
    "A2": RoleCard("A2", "Profiler", "资源画像、运行时长与排队时长估计",
                   ("read_knowledge", "load_history", "estimate_duration"),
                   "任务清单 + 知识包", "逐作业的时长、置信度、方法标注",
                   "置信度过低 → 标 conservative，交由 Verifier 判定证据强度"),
    "A3": RoleCard("A3", "Scheduler", "多目标权衡、约束冲突处理、决策依据生成",
                   ("read_knowledge", "score_candidates", "query_cluster"),
                   "作业画像 + 集群状态 + 权重", "作业→池→起始时刻 + 对照依据",
                   "无可行解 → 输出“不可调度作业”并升级人工"),
    "A4": RoleCard("A4", "Executor", "提交、监控、迁移与重试",
                   ("submit_job", "poll_status", "migrate_job"),
                   "调度方案", "逐作业执行结果与异常",
                   "连续失败 3 次 → 熔断，停止自动重试"),
    "A5": RoleCard("A5", "Verifier", "确定性校验（L1-L4）+ 语义校验（L5 Judge）",
                   ("schema_check", "recompute", "constraint_check", "judge"),
                   "调度方案 + 执行结果", "缺陷清单（含责任角色与节点）",
                   "缺陷无法归因 → 标 unresolved，禁止自动判定通过"),
    "A6": RoleCard("A6", "Reflector", "rubric 评分、归因、生成定向返工指令",
                   ("score_rubric", "attribute", "rework_command"),
                   "缺陷清单 + 执行结果", "分数 + 返工 JSON",
                   "返工预算耗尽 → 升级人工，保留现场"),
    "A7": RoleCard("A7", "Reporter", "报告撰写、成本与碳排归档",
                   ("render_report", "export_csv", "archive"),
                   "全流程轨迹", "运行报告 + 指标表",
                   "指标缺失 → 明确标注“未采到”，不补造数字"),
}


# ---------------------------------------------------------------- A2 Profiler
class Profiler:
    """把“知识包里的画像”翻译成“这批作业要跑多久”。"""

    def __init__(self, pack: KnowledgePack):
        """构造画像估计器：绑定知识包，维护按作业类型积累的历史时长比例。"""
        self.pack = pack
        self.history: dict[str, list[float]] = {}

    def estimate(self, jobs: list[Job], method: str = "profile_mean") -> list[dict]:
        """把知识包里的画像（gpu_hours 区间）翻译为每个作业的估计时长、置信度与出处。

        置信度与画像的离散度成反比；历史数据若存在则按实际/估计比率做校准。
        每条估计都带证据来源，供 A5 验证器追溯口径。
        """
        out = []
        for job in jobs:
            prof = self.pack.job_profiles[job.type]
            mid = sum(prof.gpu_hours) / 2
            base = mid * 60 / job.gpus
            spread = (prof.gpu_hours[1] - prof.gpu_hours[0]) / mid
            confidence = max(0.35, 1 - spread / 2)
            est = base
            if method == "conservative":
                est = base * 1.35
                confidence = min(0.95, confidence + 0.2)
            if self.history.get(job.type):
                ratio = sum(self.history[job.type]) / len(self.history[job.type])
                est *= max(1.0, ratio)
                method = f"{method}+history_calibrated"
            out.append({
                "job_id": job.id, "type": job.type,
                "est_min": int(round(est)),
                "confidence": round(confidence, 3),
                "method": method,
                "evidence": f"knowledge:{self.pack.version}/profile:{job.type}",
            })
        return out

    def learn(self, results: list[JobResult], jobs: list[Job]) -> None:
        """把实测超时比例写回经验库，这是“经验沉淀”的最短闭环。"""
        by_id = {j.id: j for j in jobs}
        for r in results:
            job = by_id.get(r.job_id)
            if job and job.duration_est:
                self.history.setdefault(r.type, []).append(job.duration_actual / job.duration_est)


# ---------------------------------------------------------------- A3 Scheduler
class Scheduler:
    """显式目标函数寻优：在候选（池, 起始时刻）上取 J 最小者。"""

    def __init__(self, pack: KnowledgePack, llm: ModelGateway | None = None):
        """构造调度器：绑定知识包，可选注入模型网关（用于生成决策叙述，不产出数字）。"""
        self.pack = pack
        self.llm = llm

    def plan(self, jobs: list[Job], estimates: list[dict], cluster: Cluster,
             weights_override: dict[str, dict] | None = None,
             only: set[str] | None = None,
             candidate_mode: str = "full") -> dict[str, dict]:
        """按优先级与截止时间逐作业寻最优（池, 起始时刻），返回作业 → 调度方案的映射。

        约束越紧的作业先排（推理类优先），避免被长作业挤掉；
        先做硬约束过滤（完成时刻不越过截止时间），再按目标函数打分。
        """
        est = {e["job_id"]: e for e in estimates}
        plan: dict[str, dict] = {}
        targets = [j for j in jobs if (only is None or j.id in only)]
        # 约束越紧的作业先排（推理类优先），避免被长作业挤掉
        targets.sort(key=lambda j: (-j.priority, j.deadline_min))
        for job in targets:
            w = (weights_override or {}).get(job.id) or self.pack.weights_for(job.type)
            duration = max(15, est[job.id]["est_min"])
            chosen = self._best(job, duration, cluster, w, candidate_mode)
            if chosen is None:
                continue
            cluster.place(chosen["pool_id"], chosen["start_min"], duration, job.gpus, job.id)
            plan[job.id] = chosen
        return plan

    def _best(self, job: Job, duration: int, cluster: Cluster, w: dict,
              candidate_mode: str = "full") -> dict | None:
        """在候选（池, 起始时刻）上按目标函数打分并选最优者。

        硬约束优先：若存在“不迟到”的候选，只在其中选；否则退化为“最早完成”。返回的候选
        带 `feasible`、`alternatives`（取前 4 名）与 `rationale`（L5 Judge 可解析的依据）。
        """
        best, scored = None, []
        feasible: list[dict] = []
        for start in self._candidate_starts(job, duration, cluster, candidate_mode):
            for pool in self.pack.pools:
                if not cluster.fits(pool.id, start, duration, job.gpus):
                    continue
                cand = self._score(job, duration, pool.id, start, cluster, w)
                scored.append(cand)
                # 硬约束优先：预计完成时刻不得越过截止时间
                if start + duration <= job.deadline_min:
                    feasible.append(cand)
        if feasible:
            for cand in feasible:
                if best is None or cand["score"] < best["score"]:
                    best = cand
        elif scored:
            # 无可行解时退化为“最早完成”，把迟到压到最小，而不是继续按目标函数挑
            best = min(scored, key=lambda c: c["start_min"] + c["duration_min"])
        if not best:
            return None
        best["feasible"] = bool(feasible)
        scored.sort(key=lambda c: c["score"])
        best["alternatives"] = [
            {"pool_id": c["pool_id"], "start_min": c["start_min"], "score": round(c["score"], 4)}
            for c in scored[1:4]
        ]
        best["rationale"] = self._rationale(job, best, scored)
        return best

    def _candidate_starts(self, job: Job, duration: int, cluster: Cluster,
                          mode: str = "full") -> list[int]:
        """生成候选启动时刻：通用模式只取各池最早可排点，完整模式再加谷段/峰段边界锚点。"""
        starts: set[int] = set()
        for pool in self.pack.pools:
            s = cluster.earliest_start(pool.id, job.arrive_min, duration, job.gpus)
            if s is not None:
                starts.add(s)
        if mode != "full":
            # 无领域知识的通用 Agent：只看“最早能排在哪”，不做电价/绿电窗口对齐
            return sorted(starts)
        # 主动对齐谷段与峰段边界：这正是“电价与绿电窗口利用”的来源
        valley = self.pack.tariff["valley_hours"]
        peak = self.pack.tariff["peak_hours"]
        anchors = [h * 60 for h in valley] + [(h + 1) * 60 for h in peak]
        for a in sorted(anchors):
            t = a if a >= job.arrive_min else a + 24 * 60
            if t + duration <= cluster.horizon_min:
                starts.add(t)
        return sorted(starts)

    def _score(self, job: Job, duration: int, pool_id: str, start: int,
               cluster: Cluster, w: dict) -> dict:
        """对单个（池, 起始时刻）候选计算加权目标值与各分项，供择优与取舍说明使用。"""
        pool = self.pack.pool(pool_id)
        energy = job.gpus * pool.gpu_power_kw * pool.pue * duration / 60
        price, segment = self.pack.price_at(pool_id, start)
        cost = energy * price + job.gpus * duration / 60 * pool.amort_per_gpu_hour
        green = self.pack.green_at(start + duration // 2)
        carbon = energy * (1 - green) * self.pack.grid_carbon_factor
        # P1-2.6：碳排分项不再复用 (1-green)。基准取「同一作业落在能耗最高的
        # 资源池、且完全无绿电」时的碳排量，于是
        #   carbon_norm = (1-green) × (本池 功耗×PUE / 最高 功耗×PUE) ∈ [0,1]
        # 既保留绿电时段的贡献，又把「选高 PUE 池 → 能耗高 → 碳排高」的
        # 链路真正接进目标函数（原实现两个分项同值，资源池能耗差异被抹平）。
        max_power = max(p.gpu_power_kw * p.pue for p in self.pack.pools)
        carbon_ref = (job.gpus * duration / 60 * max_power
                      * self.pack.grid_carbon_factor)
        carbon_norm = carbon / max(1e-6, carbon_ref)
        wait = max(0, start - job.arrive_min)
        window = max(1, job.deadline_min - job.arrive_min)
        congestion = self._congestion(cluster, pool_id, start, duration)
        j = (w["wait"] * min(1.5, wait / window)
             + w["cost"] * (cost / max(1e-6, job.budget))
             + w["green"] * (1 - green)
             + w["carbon"] * carbon_norm
             + w["reliability"] * congestion)
        return {
            "job_id": job.id, "pool_id": pool_id, "start_min": start,
            "duration_min": duration, "cost_est": round(cost, 2),
            "energy_kwh": round(energy, 2), "green_share": round(green, 3),
            "carbon_kg": round(carbon, 3), "carbon_norm": round(carbon_norm, 4),
            "segment": segment,
            "congestion": round(congestion, 3), "score": round(j, 5),
        }

    @staticmethod
    def _congestion(cluster: Cluster, pool_id: str, start: int, duration: int) -> float:
        """目标时间窗内的占用率（0~1），作为“可靠性”分项，避免都往同一个空池挤。"""
        cap = cluster.pack.pool(pool_id).gpus
        used = sum(g for s, e, g, _ in cluster.alloc[pool_id]
                   if s < start + duration and e > start)
        return min(1.0, (used + 1) / cap)

    @staticmethod
    def _rationale(job: Job, best: dict, scored: list[dict]) -> dict:
        """生成“因何排除、因何选定”的对照说明，是 L5 语义校验与事后复核的输入。

        只写可复核的事实（谁更便宜、差多少钱、绿电差几个百分点），不做主观判断。
        """
        cheaper = [c for c in scored if c["cost_est"] < best["cost_est"]]
        greener = [c for c in scored if c["green_share"] > best["green_share"]]
        return {
            "选定": f"{best['pool_id']} @ {best['start_min']}min（{best['segment']}段，"
                    f"绿电 {best['green_share']:.0%}，预计 ¥{best['cost_est']}）",
            "排除更便宜": (f"{cheaper[0]['pool_id']} 成本低 ¥{best['cost_est'] - cheaper[0]['cost_est']:.1f}，"
                        f"但等待 {cheaper[0]['start_min'] - best['start_min']}min、绿电低 "
                        f"{(best['green_share'] - cheaper[0]['green_share']) * 100:.0f} 个百分点")
                        if cheaper else "无更便宜候选",
            "排除更绿": (f"{greener[0]['pool_id']} 绿电高 {greener[0]['green_share'] - best['green_share']:.0%}，"
                       f"但等待增加 {greener[0]['start_min'] - best['start_min']}min") if greener else "无更绿候选",
            "作业类型": job.type,
            "综合得分": best["score"],
        }


# ---------------------------------------------------------------- A4 Executor
class Executor:
    """把方案提交到模拟集群并回收真实结果；只上报异常，不做通过与否的判定。"""

    def __init__(self, pack: KnowledgePack):
        """构造执行器：绑定知识包，维护本次运行的重试计数。"""
        self.pack = pack
        self.retries = 0

    def run(self, jobs: list[Job], plan: dict[str, dict], cluster: Cluster
            ) -> tuple[list[JobResult], RunMetrics]:
        """把调度方案落地到集群执行，返回逐作业结果与汇总指标。

        执行侧只做“发现异常”，不做判定（判定权在 A5 Verifier），避免“自证其清”。
        重试计数 = 未达 SLA 的作业数。
        """
        results, metrics = execute(self.pack, cluster, jobs, plan)
        # 执行侧只做“发现异常”，不做判定：判定权在 A5，避免自证
        self.retries = sum(1 for r in results if not r.sla_ok)
        return results, metrics


# ---------------------------------------------------------------- A7 Reporter
class Reporter:
    """把运行指标与缺陷叙述成不超过 120 字的复盘文本，只描述事实，不编造数字。"""

    def __init__(self, llm: ModelGateway | None = None):
        """可选绑定模型网关：在线时由模型润色叙述，离线时走确定性模板。"""
        self.llm = llm

    def narrative(self, metrics: RunMetrics, defects: list[dict], rounds: int) -> str:
        """生成本批次调度结果的结构化叙述；离线时走确定性模板，在线时让模型润色但不捏造指标。"""
        deterministic = (f"本批次 {metrics.jobs} 个作业，SLA 达成 {metrics.sla_rate:.1%}，"
                         f"预算达标 {metrics.budget_rate:.1%}，"
                         f"单位算力成本 ¥{metrics.cost_per_gpu_hour:.2f}/GPU·h，"
                         f"平均绿电占比 {metrics.avg_green_share:.1%}，"
                         f"碳排 {metrics.total_carbon_kg:.1f} kg，共 {rounds} 轮收敛。")
        if self.llm is None:
            return deterministic
        # 在线：模型写叙述；离线：走网关的确定性模板（同样记账 token，便于展示模型层的调用轨迹）
        prompt = (f"{deterministic}\n"
                  f"缺陷数：{len(defects)}　返工轮次：{rounds}\n"
                  "请用不超过 120 字总结本批次调度结果，只描述事实，不要编造数字。")
        return self.llm.chat("你是算力调度复盘报告撰写助手，只依据给定数据结构化叙述。", prompt)
