"""指标术语单一来源：字段名 → 统一短名 / 表头完整标签 / 完整定义。

评测产物（evaluation/）、交互看板（app/server.py）、单文件看板（gcs/report.py）
与文档表格共用本字典，避免「绿电窗口占比」「起跑时刻」这类简写或旧口径漂移。
定义与 gcs/simulator.py::aggregate 的实现口径严格一致：

- green_window_ratio：整作业执行期逐小时加权的平均绿电占比 >= 75% 的作业，
  其 GPU 小时之和 / 全部已执行作业的 GPU 小时之和。
- avg_green_share：各作业 green_share 的算术平均（作业等权），非电量加权。
- avg_wait_min / p95_wait_min：仅排队等待（进入队列到开始执行），不含执行时长。
"""
from __future__ import annotations

# 统一短名：正文叙述、KPI 主标签、图表标题一律用这一套
TERM = {
    "sla_rate": "SLA 达成率",
    "budget_rate": "预算达标率",
    "green_ok_rate": "绿电达标率",
    "avg_wait_min": "平均排队等待",
    "p95_wait_min": "排队等待 P95",
    "cost_per_gpu_hour": "单位算力成本",
    "avg_green_share": "平均绿电占比",
    "green_window_ratio": "绿电窗口作业占比",
    "total_carbon_kg": "总碳排",
    "rounds": "返工轮次",
    "rubric_score": "rubric 得分",
    "tokens": "累计 token",
}

# 表头完整标签：短名 + 关键口径括注，供 summary.md / CI 表 / HTML 表头使用
LABEL = {
    "sla_rate": "SLA 达成率（作业等权，截止时间达成比例）",
    "budget_rate": "预算达标率（作业等权，预算约束达成比例）",
    "green_ok_rate": "绿电达标率（作业等权，满足知识包绿电约束）",
    "avg_wait_min": "平均排队等待（min，仅队列等待、不含执行）",
    "p95_wait_min": "排队等待 P95（min，nearest-rank、不含执行）",
    "cost_per_gpu_hour": "单位算力成本（元/GPU·h，总费用 ÷ 已完成作业 GPU 小时）",
    "avg_green_share": "平均绿电占比（作业等权，各作业执行期绿电比例的算术平均）",
    "green_window_ratio": "绿电窗口作业占比（整作业期平均绿电占比 ≥75% 的作业，GPU 小时加权）",
    "total_carbon_kg": "总碳排（kg，电量 × (1−绿电占比) × 电网碳因子）",
    "rounds": "返工轮次（含首轮）",
    "rubric_score": "rubric 得分（六维加权，满分 100）",
    "tokens": "累计 token（在线模型调用量；离线降级也会累计，不置 0）",
}

# 完整定义句：文档口径表、看板 title 悬浮说明
DEFINITION = {
    "sla_rate": "SLA 达成率：截止时间达成作业数 / 目标作业数，作业等权。",
    "budget_rate": "预算达标率：预算约束达成作业数 / 目标作业数，作业等权。",
    "green_ok_rate": "绿电达标率：满足知识包 min_green_share 约束的作业占比，作业等权；与 0.75 窗口阈值不是同一口径。",
    "avg_wait_min": "平均排队等待：各作业「进入队列到开始执行」等待时长的算术平均，不含执行时长。",
    "p95_wait_min": "排队等待 P95：等待时长的 nearest-rank 第 ceil(0.95n) 个样本，不含执行时长。",
    "cost_per_gpu_hour": "单位算力成本：已完成作业总费用（电费 + 摊销）/ 这些作业的 GPU 小时之和，单位元/GPU·h。",
    "avg_green_share": "平均绿电占比：各作业 green_share（执行期逐小时加权）的算术平均，作业等权，反映电量结构。",
    "green_window_ratio": "绿电窗口作业占比：整作业执行期平均绿电占比 ≥75% 的作业所承载 GPU 小时 / 全部已执行作业的 GPU 小时，按 GPU 小时加权。",
    "total_carbon_kg": "总碳排：各作业电量 × (1 − 绿电占比) × 电网碳因子之和，单位 kg。",
    "rounds": "返工轮次：自省闭环触发的定向返工轮数（含首轮为 1）。",
    "rubric_score": "rubric 得分：硬约束 30 / 口径 20 / 证据 20 / 覆盖 15 / 结构 10 / 成本 5 六维加权，满分 100；不含绿电绩效维。",
    "tokens": "累计 token：ModelGateway 每次调用按估算/实际累计输入与输出 token；"
             "无论在线或离线降级都会累计，离线不置 0。",
}

# KPI 卡片副标题：紧凑口径（完整定义放在 title 悬浮说明与看板口径区）
NOTE = {
    "sla_rate": "作业等权 · 截止时间",
    "cost_per_gpu_hour": "元 / GPU·h · 费用÷GPU 小时",
    "avg_green_share": "作业等权 · 电量占比",
    "avg_wait_min": "仅排队，不含执行",
    "rounds": "含首轮",
}


def term(field: str) -> str:
    """统一短名；未知字段回退为字段名本身，保证看板不因新增指标崩溃。"""
    return TERM.get(field, field)


def label(field: str) -> str:
    """表头完整标签；未知字段回退为字段名本身。"""
    return LABEL.get(field, field)


def definition(field: str) -> str:
    """完整定义句；未知字段给出如实说明。"""
    return DEFINITION.get(field, f"{field}：口径说明待补。")
