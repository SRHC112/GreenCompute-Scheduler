"""领域知识包：把“宁夏枢纽怎么算钱、什么时候绿电多、什么作业要多少卡”变成可校验的数据。

设计要点：
1. 所有调度相关的先验信息都来自这里，代码里不留任何“经验常数”。
2. 每次修改必须升级 version，便于对照实验锁定同一版本。
3. 校验失败直接抛异常（L1 结构校验的第一道闸），不允许带病运行。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import math

import yaml

SEGMENTS = ("valley", "flat", "peak")
WEIGHT_KEYS = frozenset({"wait", "cost", "green", "carbon", "reliability"})

KNOWLEDGE_HINT = ("请确认知识包文件存在且为合法 YAML；可用 --knowledge 指定其它路径，"
                  "或重新获取 knowledge/ningxia_cluster.yaml。")

SCHEMA_VERSION_CURRENT = 2
# 知识包 schema 版本的必填键集。v1 为老文档可省略 schema_version 时的基准集；
# v2 在 v1 基础上新增 constraints.max_budget_overrun 与 rework_budget.max_tokens/max_seconds。
# 缺 schema_version 的旧包按 v1 处理；只有键集齐全（含 v2 新增键）才会通过校验。
_SCHEMA_V1_KEYS: dict[str, tuple[str, ...]] = {
    "constraints": ("min_green_share",),
    "rework_budget": ("max_rounds", "pass_score"),
}
_SCHEMA_V2_NEW_KEYS: dict[str, tuple[str, ...]] = {
    "constraints": ("max_budget_overrun",),
    "rework_budget": ("max_tokens", "max_seconds"),
}
_SCHEMA_V2_FLATTENED: frozenset[str] = frozenset(
    f"{sec}.{key}" for sec, keys in _SCHEMA_V2_NEW_KEYS.items() for key in keys
)

# 顶层必填字段：缺任一者给出可操作的中文提示，而非裸 KeyError
TOP_LEVEL_REQUIRED: tuple[str, ...] = (
    "version", "region", "tariff", "green_share_by_hour",
    "grid_carbon_factor", "pools", "job_profiles",
    "constraints", "rework_budget",
)


def _schema_required_keys(schema_version: int) -> dict[str, tuple[str, ...]]:
    """按声明版本返回当前生效的必填键集（v1 基准 + v>=2 时的 v2 新增键）。"""
    required: dict[str, tuple[str, ...]] = dict(_SCHEMA_V1_KEYS)
    if schema_version >= 2:
        for section, keys in _SCHEMA_V2_NEW_KEYS.items():
            required[section] = tuple(required[section]) + keys
    return required


class KnowledgeError(RuntimeError):
    """知识包加载失败（文件缺失 / YAML 语法错误 / 结构不合法）。

    CLI 入口统一捕获本异常并输出中文提示 + 退出码 2，避免把裸 traceback 和
    内部绝对路径暴露给使用者（P3-1）；库内仍保留原始异常链（``__cause__``）。
    """


def _require_mapping(value: Any, where: str) -> None:
    """结构前置断言：字段必须是映射（键值对）。

    背景：``price`` / ``constraints`` / ``weights`` 等字段若被写成列表或标量，
    下游的 ``.items()`` / ``.get()`` 会抛 AttributeError；不给前置断言就会以裸
    traceback 暴露给使用者。这里改成带字段名的可读错误。
    """
    if value is None:
        raise ValueError(f"知识包缺少 {where} 段（必须是映射键值对，未在文件中出现）")
    if not isinstance(value, dict):
        raise ValueError(f"{where} 必须是映射（键值对），实际为 {type(value).__name__}")


def _as_number(value: Any, where: str) -> float:
    """把知识包读取到的值转为浮点；在转换阶段拒绝布尔值。

    pass_score: true / max_rounds: true 会被 float()/int() 静默强转为 1.0，
    改变实验口径，故 bool 守卫必须置于数值转换之前。
    """
    if isinstance(value, bool):
        raise ValueError(f"{where} 必须是数值，不能是布尔值（收到 {value!r}）")
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{where} 必须是数值（收到 {value!r}）") from None


def _as_int(value: Any, where: str) -> int:
    """把知识包读取到的值转为整数；同 _as_number，优先拒绝布尔值。"""
    if isinstance(value, bool):
        raise ValueError(f"{where} 必须是整数，不能是布尔值（收到 {value!r}）")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{where} 必须是整数（收到 {value!r}）") from None


def load_knowledge(path: str | Path) -> "KnowledgePack":
    """CLI 友好加载入口：把各类加载失败统一收敛成 :class:`KnowledgeError`。"""
    try:
        return KnowledgePack.load(path)
    except (OSError, ValueError, KeyError, TypeError, IndexError,
            AttributeError, yaml.YAMLError) as exc:
        raise KnowledgeError(f"{type(exc).__name__}: {exc}") from exc


@dataclass(frozen=True)
class Pool:
    """一个算力资源池：池 ID、所在城市、GPU 型号、卡数、PUE、单卡功率、分摊摊销价、分时电价。"""
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
        """按小时取分时电价（谷/平/峰）并返回（单价，时段名）。"""
        segment = segment_of(hour, tariff)
        return self.price[segment], segment


@dataclass(frozen=True)
class JobProfile:
    """作业画像：名称、GPU 耗时区间、GPU 卡数区间、SLA 余量、每卡每小时预算、各维度权重。"""
    name: str
    gpu_hours: tuple[float, float]
    gpus: tuple[int, int]
    sla_slack: float
    budget_per_gpu_hour: float
    weights: dict[str, float]

    def weight_sum(self) -> float:
        """各维度权重之和，应约为 1.0。"""
        return round(sum(self.weights.values()), 6)


@dataclass
class KnowledgePack:
    """领域知识包的顶层容器：版本号、分时电价、每小时绿电占比、网格碳因子、
    资源池列表、作业画像、硬约束与返工预算。"""

    version: str
    region: str
    tariff: dict[str, Any]
    green_share_by_hour: list[float]
    grid_carbon_factor: float
    pools: list[Pool]
    job_profiles: dict[str, JobProfile]
    constraints: dict[str, float]
    rework_budget: dict[str, float]
    schema_version: int = 1
    path: Path | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    # ---------- 查询接口 ----------
    def pool(self, pool_id: str) -> Pool:
        """按 ID 取资源池，不存在抛 KeyError。"""
        for p in self.pools:
            if p.id == pool_id:
                return p
        raise KeyError(f"知识包中不存在资源池 {pool_id}")

    def green_at(self, minute: int) -> float:
        """按分钟取该时段的绿电占比（24 个分时值循环使用）。"""
        return self.green_share_by_hour[(minute // 60) % 24]

    def price_at(self, pool_id: str, minute: int) -> tuple[float, str]:
        """按资源池与分钟取分时电价：返回（单价，时段名）。"""
        return self.pool(pool_id).price_at((minute // 60) % 24, self.tariff)

    def weights_for(self, job_type: str) -> dict[str, float]:
        """按作业类型取目标函数权重副本（wait/cost/green/carbon/reliability，和为 1）。"""
        return dict(self.job_profiles[job_type].weights)

    def fingerprint(self) -> str:
        """知识包指纹：对照实验用来证明“各组用的是同一份知识包”。"""
        return f"{self.version}/{len(self.pools)}pools/{len(self.job_profiles)}profiles"

    @staticmethod
    def _missing_required_keys(data: dict[str, Any], schema_version: int) -> list[str]:
        """按 schema 版本返回缺失的必填键列表（含整段缺失）。不做取值域判断。"""
        required = _schema_required_keys(schema_version)
        missing: list[str] = []
        for section, keys in required.items():
            mapping = data.get(section)
            if not isinstance(mapping, dict):
                missing.append(f"{section}（整段缺失或非映射）")
                continue
            for key in keys:
                if key not in mapping:
                    missing.append(f"{section}.{key}")
        return missing

    # 旧包（缺 schema_version，视为 v1）若只有 v1 基准键而缺 v2 新增键，按“版本过低”诊断；
    # 若连 v1 基准键都缺，按“缺少必填键”诊断。两者都列出缺失键与所在段，避免裸 KeyError。
    @staticmethod
    def _schema_missing_hint(data: dict[str, Any], raw_schema: int) -> list[str] | None:
        missing = KnowledgePack._missing_required_keys(data, SCHEMA_VERSION_CURRENT)
        if not missing:
            return None
        v2_new_missing = [k for k in missing if k in _SCHEMA_V2_FLATTENED]
        lines: list[str] = []
        if v2_new_missing and raw_schema < SCHEMA_VERSION_CURRENT:
            by_section: dict[str, list[str]] = {}
            for item in v2_new_missing:
                sec, key = item.split(".", 1)
                by_section.setdefault(sec, []).append(key)
            lines.append(
                f"知识包声明 schema_version={raw_schema}（或未声明，视为 v1），"
                f"低于当前 v{SCHEMA_VERSION_CURRENT}；缺少以下 v2 新增必填键：")
            for sec, keys in by_section.items():
                lines.append(f"  - {sec}: 段补齐（缺）{keys}，再声明 schema_version: 2")
        else:
            lines.append(f"缺少必填键：{missing}；缺失会导致取值域校验失效，请按上述段补齐。")
        return lines

    # ---------- 加载与校验 ----------
    @classmethod
    def load(cls, path: str | Path) -> "KnowledgePack":
        """从 YAML 加载知识包并做结构校验；校验失败抛 ValueError，不允许带病运行。"""
        path = Path(path)
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        # 结构前置断言：先确认根节点与关键子结构是映射/列表，再做字段级转换。
        # 否则「price 写成列表」这类畸形会在下游变成 AttributeError 裸栈。
        if not isinstance(data, dict):
            raise ValueError(
                f"知识包根节点必须是映射（键值对），实际为 {type(data).__name__}")
        missing_top = [k for k in TOP_LEVEL_REQUIRED if k not in data]
        if missing_top:
            raise ValueError(
                f"知识包 {path} 缺少顶层必填字段：{missing_top}；"
                "按旧文档编写的包请补齐上述字段（缺 schema_version 时视为 v1，"
                "再按 v2 提示补齐新增必填键）。")
        raw_schema = data.get("schema_version", 1)
        if isinstance(raw_schema, bool) or not isinstance(raw_schema, int) \
                or not 1 <= raw_schema <= SCHEMA_VERSION_CURRENT:
            raise ValueError(
                f"schema_version 必须是 1-{SCHEMA_VERSION_CURRENT} 的整数（收到 {raw_schema!r}）")
        pools_raw = data.get("pools")
        scheduling = data.get("scheduling")
        if scheduling is not None:
            _require_mapping(scheduling, "scheduling")
            horizon = scheduling.get("horizon_min")
            if (not isinstance(horizon, int) or isinstance(horizon, bool)
                    or not 0 < horizon <= 7 * 24 * 60):
                raise ValueError(
                    f"scheduling.horizon_min 必须是 1-10080 的整数（收到 {horizon!r}）")
        _require_mapping(data.get("rework_budget"), "rework_budget")
        _require_mapping(data.get("job_profiles"), "job_profiles")
        pools_raw = data.get("pools")
        if not isinstance(pools_raw, list):
            raise ValueError(f"pools 必须是列表，实际为 {type(pools_raw).__name__}")
        for i, pool_raw in enumerate(pools_raw):
            if not isinstance(pool_raw, dict):
                raise ValueError(
                    f"pools[{i}] 必须是映射，实际为 {type(pool_raw).__name__}")
            _require_mapping(pool_raw.get("price"), f"pools[{i}].price")
        for name, prof_raw in (data.get("job_profiles") or {}).items():
            if not isinstance(prof_raw, dict):
                raise ValueError(
                    f"job_profiles[{name}] 必须是映射，实际为 {type(prof_raw).__name__}")
            _require_mapping(prof_raw.get("weights"), f"job_profiles[{name}].weights")
            for key in ("gpu_hours", "gpus"):
                seq = prof_raw.get(key)
                if not isinstance(seq, (list, tuple)) or len(seq) != 2:
                    raise ValueError(
            f"job_profiles[{name}].{key} 必须是长度为 2 的区间，"
            f"实际为 {seq!r}")
        # 缺键友好诊断（结构断言之后、数值转换之前）：旧包缺 v2 新增键时给出版本过低
        # + 可操作提示（文件/所在段），避免下游裸 KeyError；全集缺失则列出必填键。
        hint = cls._schema_missing_hint(data, raw_schema)
        if hint is not None:
            raise ValueError("知识包 " + str(path) + ":\n- " + "\n- ".join(hint))
        pack = cls(
            version=str(data["version"]),
            region=str(data["region"]),
            tariff=data["tariff"],
            green_share_by_hour=[
                _as_number(x, f"green_share_by_hour[{i}]")
                for i, x in enumerate(data["green_share_by_hour"])],
            grid_carbon_factor=_as_number(data["grid_carbon_factor"], "grid_carbon_factor"),
            pools=[
                Pool(
                    id=p["id"],
                    city=p["city"],
                    gpu=p["gpu"],
                    gpus=_as_int(p["gpus"], f"pools[{p.get('id', '?')}].gpus"),
                    pue=_as_number(p["pue"], f"pools[{p.get('id', '?')}].pue"),
                    gpu_power_kw=_as_number(p["gpu_power_kw"],
                                            f"pools[{p.get('id', '?')}].gpu_power_kw"),
                    amort_per_gpu_hour=_as_number(p["amort_per_gpu_hour"],
                                                  f"pools[{p.get('id', '?')}].amort_per_gpu_hour"),
                    price={k: _as_number(v, f"pools[{p.get('id', '?')}].price.{k}")
                           for k, v in p["price"].items()},
                    note=p.get("note", ""),
                )
                for p in data["pools"]
            ],
            job_profiles={
                k: JobProfile(
                    name=k,
                    # 显式标注二元组：generate 式推导会得到 tuple[float, ...]，
                    # 与 JobProfile 声明的 tuple[float, float] 不符（mypy arg-type）
                    gpu_hours=cast("tuple[float, float]",
                                   tuple(_as_number(x, f"job_profiles[{k}].gpu_hours[]")
                                         for x in v["gpu_hours"])),
                    gpus=cast("tuple[int, int]",
                              tuple(_as_int(x, f"job_profiles[{k}].gpus[]") for x in v["gpus"])),
                    sla_slack=_as_number(v["sla_slack"], f"job_profiles[{k}].sla_slack"),
                    budget_per_gpu_hour=_as_number(v["budget_per_gpu_hour"],
                                                   f"job_profiles[{k}].budget_per_gpu_hour"),
                    weights={wk: _as_number(wv, f"job_profiles[{k}].weights.{wk}")
                             for wk, wv in v["weights"].items()},
                )
                for k, v in data["job_profiles"].items()
            },
            constraints={k: _as_number(v, f"constraints.{k}")
                         for k, v in data["constraints"].items()},
            rework_budget={k: _as_number(v, f"rework_budget.{k}")
                           for k, v in data["rework_budget"].items()},
            schema_version=raw_schema,
            path=path,
            raw=data,
        )
        pack.validate()
        return pack

    def validate(self) -> None:
        """结构校验：24 小时绿电占比、资源池非空且 id 不重复、电价覆盖峰平谷、
        各作业类型权重之和为 1、分时电价覆盖 0-23 时且不重叠。失败抛 ValueError。"""
        errors: list[str] = []
        if len(self.green_share_by_hour) != 24:
            errors.append("green_share_by_hour 必须为 24 个点")
        for i, v in enumerate(self.green_share_by_hour):
            if not math.isfinite(v) or not 0.0 <= v <= 1.0:
                errors.append(
                    f"green_share_by_hour[{i}] = {v} 超出 [0, 1]"
                    "（绿电消费占比是比例；NaN/Inf 亦非法）")
        if not self.pools:
            errors.append("至少需要一个资源池")
        ids = [p.id for p in self.pools]
        if len(ids) != len(set(ids)):
            errors.append("资源池 id 重复")
        for p in self.pools:
            if set(p.price) != set(SEGMENTS):
                errors.append(f"{p.id} 电价缺少峰/平/谷任一档")
            else:
                for seg, val in p.price.items():
                    if not math.isfinite(val) or val <= 0:
                        errors.append(
                            f"{p.id} 的 {seg} 档电价必须为正（收到 {val}）")
                if not (p.price["peak"] >= p.price["flat"] >= p.price["valley"]):
                    errors.append(
                        f"{p.id} 电价档位倒挂：应满足 peak ≥ flat ≥ valley"
                        f"（收到 peak={p.price['peak']} flat={p.price['flat']}"
                        f" valley={p.price['valley']}）")
            if p.gpus <= 0 or p.pue < 1.0:
                errors.append(f"{p.id} 的 gpus/pue 取值不合理")
            if not math.isfinite(p.gpu_power_kw) or p.gpu_power_kw <= 0:
                errors.append(f"{p.id} 的 gpu_power_kw 必须为正（收到 {p.gpu_power_kw}）")
            if not math.isfinite(p.amort_per_gpu_hour) or p.amort_per_gpu_hour < 0:
                errors.append(
                    f"{p.id} 的 amort_per_gpu_hour 不得为负（收到 {p.amort_per_gpu_hour}）")
        # F2 加固：电网碳排因子为正的物理量，负值会让 carbon_norm 归一化分母被
        # max(1e-6, …) 兜底成 1e-6，使碳排目标项爆量并反向优化，必须在加载期拒绝。
        if self.grid_carbon_factor < 0:
            errors.append(f"grid_carbon_factor 不能为负（收到 {self.grid_carbon_factor}）")
        for name, prof in self.job_profiles.items():
            if abs(prof.weight_sum() - 1.0) > 1e-6:
                errors.append(f"作业类型 {name} 的目标权重之和为 {prof.weight_sum()}，应为 1")
            if prof.gpu_hours[0] > prof.gpu_hours[1]:
                errors.append(f"作业类型 {name} 的 gpu_hours 区间反向")
            if prof.gpus[0] > prof.gpus[1]:
                errors.append(f"作业类型 {name} 的 gpus 区间反向")
            if prof.gpu_hours[0] <= 0:
                errors.append(
                    f"作业类型 {name} 的 gpu_hours 下界必须为正（收到 {prof.gpu_hours[0]}）")
            if prof.gpus[0] < 1:
                errors.append(
                    f"作业类型 {name} 的 gpus 下界必须 ≥ 1（收到 {prof.gpus[0]}）")
            if not math.isfinite(prof.sla_slack) or prof.sla_slack <= 1.0:
                errors.append(
                    f"作业类型 {name} 的 sla_slack 必须 > 1.0（收到 {prof.sla_slack}）")
            if not math.isfinite(prof.budget_per_gpu_hour) or prof.budget_per_gpu_hour <= 0:
                errors.append(
                    f"作业类型 {name} 的 budget_per_gpu_hour 必须为正"
                    f"（收到 {prof.budget_per_gpu_hour}）")
            if set(prof.weights) != WEIGHT_KEYS:
                errors.append(
                    f"作业类型 {name} 的目标权重键集应为 {sorted(WEIGHT_KEYS)}，"
                    f"实际 {sorted(prof.weights)}")
            negative = {k: v for k, v in prof.weights.items() if v < 0}
            if negative:
                errors.append(f"作业类型 {name} 的目标权重不得为负：{negative}")
        # 分时电价：三档列表存在、元素为 0-23 的整数、列表内无重复、两两不重叠、
        # 并集恰好覆盖 0-23。重叠必须在加载期拒绝，否则 segment_of 会按谷→平→峰
        # 的固定优先级隐式择一，静默改变电价口径。
        # 必需键存在性：缺键会在下游变成运行期 KeyError 裸栈，必须加载期拒绝
        for section, required in (("constraints", ("min_green_share", "max_budget_overrun")),
                                  ("rework_budget", ("max_rounds", "pass_score",
                                                     "max_tokens", "max_seconds"))):
            mapping = getattr(self, section, None)
            if not isinstance(mapping, dict):
                continue  # 结构问题已在 load() 的前置断言里报过
            missing_keys = [k for k in required if k not in mapping]
            if missing_keys:
                errors.append(f"{section} 缺少必需键 {missing_keys}"
                              f"（必需键：{list(required)}）")
        constraints = self.constraints if isinstance(self.constraints, dict) else {}
        if "min_green_share" in constraints:
            v = constraints["min_green_share"]
            if not math.isfinite(v) or not 0.0 <= v <= 1.0:
                errors.append(f"constraints.min_green_share 必须在 [0, 1]（收到 {v}）")
        if "max_budget_overrun" in constraints:
            v = constraints["max_budget_overrun"]
            if not math.isfinite(v) or not 0.0 <= v <= 1.0:
                errors.append(f"constraints.max_budget_overrun 必须在 [0, 1]（收到 {v}）")
        budget = self.rework_budget if isinstance(self.rework_budget, dict) else {}
        if "max_rounds" in budget:
            v = budget["max_rounds"]
            # 知识包的数值字段统一按 float 存储，故这里接受「值为整数的数值」
            if (not isinstance(v, (int, float)) or isinstance(v, bool)
                    or not math.isfinite(v) or v < 0 or float(v) != int(v)):
                errors.append(
                    f"rework_budget.max_rounds 必须是非负整数（收到 {v}）")
        if "pass_score" in budget:
            v = budget["pass_score"]
            if not math.isfinite(v) or not 0.0 <= v <= 100.0:
                errors.append(f"rework_budget.pass_score 必须在 [0, 100]（收到 {v}）")
        for key in ("max_tokens", "max_seconds"):
            if key in budget:
                v = budget[key]
                if not math.isfinite(v) or v <= 0:
                    errors.append(f"rework_budget.{key} 必须为正（收到 {v}）")
        tariff = self.tariff
        seg_hours: dict[str, list[int]] = {}
        for seg in SEGMENTS:
            key = f"{seg}_hours"
            values = tariff.get(key) if isinstance(tariff, dict) else None
            if not isinstance(values, (list, tuple)):
                errors.append(f"分时电价缺少或非列表：{key}")
                continue
            bad = [v for v in values
                   if not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= 23]
            if bad:
                errors.append(f"{key} 含非法小时值 {bad}，必须为 0-23 的整数")
                continue
            if len(set(values)) != len(values):
                dup = sorted({v for v in values if values.count(v) > 1})
                errors.append(f"{key} 存在重复小时 {dup}")
            seg_hours[seg] = list(values)
        if len(seg_hours) == len(SEGMENTS):
            sets = {seg: set(v) for seg, v in seg_hours.items()}
            for left, right in (("valley", "flat"), ("valley", "peak"), ("flat", "peak")):
                overlap = sorted(sets[left] & sets[right])
                if overlap:
                    errors.append(
                        f"分时电价时段重叠：小时 {overlap} 同时属于 "
                        f"{left}_hours 与 {right}_hours")
            missing = sorted(set(range(24)) - (sets["valley"] | sets["flat"] | sets["peak"]))
            if missing:
                errors.append(f"分时电价时段未覆盖 0-23 时，缺少小时 {missing}")
        if errors:
            raise ValueError("知识包校验失败：\n- " + "\n- ".join(errors))


def segment_of(hour: int, tariff: dict[str, Any]) -> str:
    """把小时映射到谷/平/峰三个时段之一（hour 可超过 23，自动取模）。

    合法知识包已在 validate() 阶段保证三档列表互斥且覆盖 0-23，
    因此下面的谷→平→峰顺序只是兜底，不应出现「同一小时命中多档」的情况。
    """
    hour %= 24
    if hour in tariff["valley_hours"]:
        return "valley"
    if hour in tariff["flat_hours"]:
        return "flat"
    return "peak"
