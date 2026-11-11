"""临时探针：打印每轮的缺陷明细，定位是哪条约束在卡分。"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gcs.demo import POLICIES  # noqa: E402
from gcs.knowledge import KnowledgePack  # noqa: E402
from gcs.orchestrator import Orchestrator  # noqa: E402
from gcs.simulator import make_jobs  # noqa: E402


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    pack = KnowledgePack.load(ROOT / "knowledge" / "ningxia_cluster.yaml")
    seed = int(sys.argv[1]) if len(sys.argv) > 1 else 42
    key = sys.argv[2] if len(sys.argv) > 2 else "full"
    jobs = make_jobs(pack, n=80, seed=seed)
    art = Orchestrator(pack, policy=POLICIES[key]).run(jobs)
    for i, rep in enumerate(art.defect_history):
        c = Counter(d["code"] for d in rep["defects"])
        print(f"--- 第 {i} 轮：{dict(c)}")
        for d in rep["defects"][:6]:
            print("    ", d["code"], "|", d["detail"], "|", d["evidence"])
    print("最终指标:", art.metrics.as_dict())


if __name__ == "__main__":
    main()
