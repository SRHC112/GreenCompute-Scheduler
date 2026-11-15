# -*- coding: utf-8 -*-
"""2026 未来杯 GreenCompute-Scheduler：F1~F6 缺陷回归测试（2026-11-15）。

只新增本文件，不改动既有测试。覆盖：
- F1：run_eval.py --jobs 取值 1-300，越界 rc=2 + 中文提示，无裸 traceback
- F2：产物（summary.md / eval_report.html / summary.json）声明的作业数等于本次实际 --jobs，非写死 80
- F3：trace_fit.py 知识包加载失败 → 结构化中文错误 + rc=2，无裸 traceback
- F4：run_eval.py --outdir 不可写/非目录 → 中文提示 + rc=2，无裸 traceback
- F6：离线降级的 rubric 判定来源可见，llm 与 verifier 口径一致
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "evaluation"))

import run_eval  # noqa: E402
import trace_fit as trace_fit_module  # noqa: E402

from gcs.knowledge import KnowledgeError, KnowledgePack  # noqa: E402
from gcs.llm import ModelGateway  # noqa: E402
from gcs.verifier import Verifier  # noqa: E402

PACK = KnowledgePack.load(ROOT / "knowledge" / "ningxia_cluster.yaml")
RUN_EVAL = ROOT / "evaluation" / "run_eval.py"


def _run_eval_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(RUN_EVAL), *map(str, args)],
        capture_output=True, encoding="utf-8", errors="replace",
    )


class TestJobsRangeF1(unittest.TestCase):
    def test_jobs_0_rc2(self):
        proc = _run_eval_cli("--jobs", "0", "--outdir", tempfile.mkdtemp())
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("--jobs 必须 >= 1", proc.stderr)
        self.assertNotIn("Traceback (most recent call last)", proc.stderr)

    def test_jobs_301_rc2(self):
        proc = _run_eval_cli("--jobs", "301", "--outdir", tempfile.mkdtemp())
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("--jobs 必须 <= 300", proc.stderr)
        self.assertNotIn("Traceback (most recent call last)", proc.stderr)

    def test_jobs_1_min_valid_rc0(self):
        proc = _run_eval_cli("--jobs", "1", "--serial", "--outdir", tempfile.mkdtemp())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("Traceback (most recent call last)", proc.stderr)

    def test_products_declare_actual_jobs_not_80(self):
        """--jobs 6 跑完后，三处产物里声明的作业数必须是 6，不是模块默认值 80。"""
        tmp = Path(tempfile.mkdtemp())
        try:
            rows = run_eval.run_all(tmp, jobs_n=6, workers=1)
            self.assertTrue(all(r["jobs"] == 6 for r in rows), [r["jobs"] for r in rows])
            stats = run_eval.aggregate(rows)
            run_eval.write_summary(rows, stats, tmp / "summary.md")
            run_eval.write_html(rows, stats, tmp / "eval_report.html")
            run_eval.write_summary_json(rows, tmp / "summary.json")
            md = (tmp / "summary.md").read_text(encoding="utf-8")
            html = (tmp / "eval_report.html").read_text(encoding="utf-8")
            self.assertIn("作业规模 6 个/次", md, md)
            self.assertIn("作业 6 个/次", html, html)
            self.assertNotIn("80 个/次", md, md)
            self.assertNotIn("80 个/次", html, html)
            sj = json.loads((tmp / "summary.json").read_text(encoding="utf-8"))
            for item in sj.values():
                self.assertEqual(item["jobs"], 6, item)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_outdir_unwritable_rc2(self):
        tmp = Path(tempfile.mkdtemp())
        existing_file = tmp / "already_a_file.txt"
        existing_file.write_text("x", encoding="utf-8")
        try:
            proc = _run_eval_cli("--jobs", "6", "--outdir", str(existing_file))
            self.assertEqual(proc.returncode, 2, proc.stderr)
            self.assertNotIn("Traceback (most recent call last)", proc.stderr)
            self.assertIn("不可写或非目录", proc.stderr)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_failure_stderr_no_traceback(self):
        """--jobs 0 / 301 / 非法目录三种失败场景的 stderr 均不得泄漏 traceback。"""
        for args in (("--jobs", "0"), ("--jobs", "301")):
            tmp = tempfile.mkdtemp()
            proc = _run_eval_cli(*args, "--outdir", tmp)
            self.assertEqual(proc.returncode, 2, proc.stderr)
            self.assertNotIn("Traceback (most recent call last)", proc.stderr)


class TestTraceFitKnowledgeErrorF3(unittest.TestCase):
    def test_knowledge_load_failure_rc2_no_traceback(self):
        with mock.patch.object(
                trace_fit_module.KnowledgePack, "load",
                side_effect=KnowledgeError("知识包未找到：/no/such/knowledge.yaml")):
            with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
                rc = trace_fit_module.main()
        self.assertEqual(rc, 2)
        self.assertIn("[ERROR]", err.getvalue())
        self.assertNotIn("Traceback (most recent call last)", err.getvalue())


class TestOfflineRubricSourceVisibleF6(unittest.TestCase):
    OFFLINE_ENV = {"GCS_LLM_BASE_URL": "", "GCS_LLM_API_KEY": "", "GCS_LLM_MODEL": ""}
    ONLINE_ENV = {"GCS_LLM_BASE_URL": "https://127.0.0.1:1/",
                  "GCS_LLM_API_KEY": "x", "GCS_LLM_MODEL": "m"}

    def test_llm_stats_offline_rubric_source(self):
        with mock.patch.dict(os.environ, self.OFFLINE_ENV, clear=False):
            llm = ModelGateway()
        self.assertFalse(llm.online)
        self.assertEqual(llm.stats()["rubric_source"], "offline-mock")
        self.assertEqual(llm.stats()["mode"], "offline-mock")

    def test_llm_stats_online_rubric_source(self):
        with mock.patch.dict(os.environ, self.ONLINE_ENV, clear=False):
            llm = ModelGateway()
        self.assertTrue(llm.online)
        self.assertEqual(llm.stats()["rubric_source"], "llm")

    def test_verifier_rubric_source_matches_llm_mode(self):
        v_off = Verifier(PACK)
        self.assertEqual(v_off.rubric_source, "offline-mock")
        with mock.patch.dict(os.environ, self.ONLINE_ENV, clear=False):
            v_on = Verifier(PACK, llm=ModelGateway())
        self.assertEqual(v_on.rubric_source, "llm")


if __name__ == "__main__":
    unittest.main()
