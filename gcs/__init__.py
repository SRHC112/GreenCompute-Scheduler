"""绿算调度 GreenCompute-Scheduler

面向“东数西算”宁夏枢纽的算力作业多智能体编排与能效优化平台。

模块划分（与《技术方案》第 9 章的分层一一对应）：
    knowledge   领域知识包（L1）—— 资源、电价、绿电、作业画像、约束
    simulator   算力模拟器（L0）—— 事件驱动，可控可复现，可注入故障
    llm         模型网关（L0）—— 国产模型 OpenAI 兼容接口 + 离线 Mock
    dag         DAG 构图与状态机（L3）
    agents      七个角色（L2）
    verifier    五层确定性校验（L4）
    reflect     rubric 评分与定向返工（L4）
    orchestrator 编排引擎（L3）
    report      看板与报告导出（L5）
"""

__version__ = "0.1.0"
