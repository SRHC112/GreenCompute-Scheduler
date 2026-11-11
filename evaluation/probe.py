"""临时探针：看不同种子下各组的指标分布，用来校准评测集与阈值。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gcs.demo import POLICIES  # noqa: E402
from gcs.knowledge import KnowledgePack  # noqa: E402
from gcs.orchestrator import Orchestrator  # noqa: E402
from gcs.simulator import make_jobs  # noqa: E402


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 40
    pack = KnowledgePack.load(ROOT / "knowledge" / "ningxia_cluster.yaml")
    print(f"n={n}")
    print(f"{'seed':>10} {'group':<22} {'sla':>6} {'wait':>5} {'cost':>5} {'green':>6} {'gw':>6} "
          f"{'rnd':>4} {'score':>5}  失败维度")
    for seed in (20261111, 7, 42):
        jobs = make_jobs(pack, n=n, seed=seed)
        for key in ("full", "fcfs", "sjf", "generic", "no_reflect", "no_green"):
            art = Orchestrator(pack, policy=POLICIES[key]).run(jobs)
            s = art.summary()
            d = art.rubric_history[-1]
            failed = "、".join(x.name for x in d.dims if not x.passed) or "-"
            print(f"{seed:>10} {s['policy']:<22} {s['sla_rate']:>6.1%} {s['avg_wait_min']:>5.0f} "
                  f"{s['cost_per_gpu_hour']:>5.2f} {s['avg_green_share']:>6.1%} "
                  f"{art.metrics.green_window_ratio:>6.1%} "
                  f"{s['rounds']:>4} {s['final_score']:>5}  {failed}")


if __name__ == "__main__":
    main()
