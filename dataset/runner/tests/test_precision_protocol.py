from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


RUNNER = Path(__file__).resolve().parents[1]
ROOT = RUNNER.parents[1]
sys.path.insert(0, str(RUNNER))

from memory_backend import new_state, precision_select  # noqa: E402
from run import Task, prepare_precision_batch, retrieval_leakage_check  # noqa: E402
from sweep import paired_eligibility  # noqa: E402
from test_protection import build_manifest, verify  # noqa: E402


SNAPSHOT = ROOT / "dataset" / "facts" / "snapshot-c0.md"


class PrecisionRetrievalTest(unittest.TestCase):
    def test_selects_only_declared_c0_and_validated_same_cluster_product_fact(self):
        state = new_state(SNAPSHOT, "memory-on", 1, "file")
        state["facts"]["fact:do-9001"] = {
            "fact_id": "fact:do-9001", "slice": "data-ownership",
            "statement": "Repository count subqueries need explicit correlation",
            "content": "Repository count subqueries need explicit correlation.",
            "status": "active", "source": "task-validated:a1:data-repository-invariants",
        }
        state["facts"]["fact:gt-9002"] = {
            "fact_id": "fact:gt-9002", "slice": "gotchas",
            "statement": "pytest sandbox timeout", "content": "pytest sandbox timeout",
            "status": "active", "source": "task-validated:a1:data-repository-invariants",
        }
        selected, log = precision_select(
            state, ["data-ownership"], ["fact:do-0005", "fact:do-0006"],
            "data-repository-invariants", 5, 0.75, 1)
        self.assertEqual([fact["fact_id"] for fact in selected],
                         ["fact:do-0005", "fact:do-0006", "fact:do-9001"])
        self.assertEqual([item["origin"] for item in log["selected"]],
                         ["c0", "c0", "learned"])
        rejected = {item["fact_id"]: item["reason"] for item in log["rejected"]}
        self.assertEqual(rejected["fact:gt-9002"], "tooling_or_sandbox_excluded")


class PairedEligibilityTest(unittest.TestCase):
    def test_entire_task_seed_is_removed_when_one_mode_is_ineligible(self):
        modes = ["memory-off", "memory-on", "memory-on+evolve"]
        rows = [{"task": "a1", "seed": 1, "mode": mode, "valid_run": True,
                 "analytical_eligible": mode != "memory-on",
                 "analytical_ineligible_reasons": (["regression_red"]
                                                     if mode == "memory-on" else [])}
                for mode in modes]
        result = paired_eligibility(rows, modes)[("a1", 1)]
        self.assertFalse(result["paired_eligible"])
        self.assertEqual(result["reasons"], ["memory-on:regression_red"])

    def test_complete_green_triple_is_paired(self):
        modes = ["memory-off", "memory-on", "memory-on+evolve"]
        rows = [{"task": "a4", "seed": 1, "mode": mode, "valid_run": True,
                 "analytical_eligible": True, "analytical_ineligible_reasons": []}
                for mode in modes]
        self.assertTrue(paired_eligibility(rows, modes)[("a4", 1)]["paired_eligible"])


class LearnedFactPromotionTest(unittest.TestCase):
    def task(self) -> Task:
        return Task("a1", "solution", "base", "title", "prompt", ["hidden.py"],
                    transfer_cluster="data-repository-invariants")

    def test_promotes_only_product_fact_with_diff_evidence_after_green_checks(self):
        batch = {"mutations": [{"op": "create", "fact": {
            "fact_id": "fact:do-9001", "slice": "data-ownership",
            "statement": "Repository owns normalized values",
            "content": "Repository owns normalized values",
            "evidence": ["mealie/repos/example.py:12"], "status": "candidate",
            "confidence": "low", "provenance": "inferred", "source": "task"}}],
            "task": {"task_id": "a1", "used_facts": [],
                     "produced_facts": ["fact:do-9001"], "decisions": []}}
        normalized, report = prepare_precision_batch(
            batch, self.task(), ["mealie/repos/example.py"], True, set(), True)
        fact = normalized["mutations"][0]["fact"]
        self.assertEqual(fact["status"], "active")
        self.assertEqual(fact["source"],
                         "task-validated:a1:data-repository-invariants")
        self.assertTrue(report["facts"][0]["confirmed_for_transfer"])

    def test_rejects_mutation_of_c0(self):
        batch = {"mutations": [{"op": "stale", "fact_id": "fact:do-0005",
                                "values": {"status_reason": "changed"}}],
                 "task": {"task_id": "a1", "used_facts": [],
                          "produced_facts": ["fact:do-0005"], "decisions": []}}
        with self.assertRaisesRegex(Exception, "immutable"):
            prepare_precision_batch(batch, self.task(), [], True, {"fact:do-0005"}, True)

    def test_evidence_path_must_exactly_match_touched_product_source(self):
        batch = {"mutations": [{"op": "create", "fact": {
            "fact_id": "fact:do-9002", "slice": "data-ownership",
            "statement": "Repository rule", "content": "Repository rule",
            "evidence": ["mealie/repos/example.py.bak:12"], "status": "candidate",
            "confidence": "low", "provenance": "inferred", "source": "task"}}],
            "task": {"task_id": "a1", "used_facts": [],
                     "produced_facts": ["fact:do-9002"], "decisions": []}}
        normalized, report = prepare_precision_batch(
            batch, self.task(), ["mealie/repos/example.py"], True, set(), True)
        self.assertEqual(normalized["mutations"][0]["fact"]["status"], "candidate")
        self.assertFalse(report["facts"][0]["evidence_in_diff"])


class RetrievalLeakageTest(unittest.TestCase):
    def task(self) -> Task:
        return Task("a1", "solution", "base", "title", "prompt", ["hidden.py"],
                    transfer_cluster="data-repository-invariants")

    def test_c0_hidden_test_path_is_rejected_before_coding(self):
        task = Task("a2", "solution", "base", "title", "prompt",
                    ["tests/unit_tests/test_config.py"],
                    expected_facts=["fact:cf-0005"])
        fact = {"fact_id": "fact:cf-0005", "source": "extraction",
                "content": "Evidence: tests/unit_tests/test_config.py:10"}
        selection = {"selected": [{"fact_id": "fact:cf-0005", "origin": "c0"}]}
        report = retrieval_leakage_check(task, {"a2": task}, [fact], selection)
        self.assertFalse(report["ok"])
        self.assertEqual(report["violations"], [{
            "fact_id": "fact:cf-0005", "reason": "hidden_path:a2"}])

    def test_agent_test_evidence_never_promotes_product_fact(self):
        batch = {"mutations": [{"op": "create", "fact": {
            "fact_id": "fact:do-9003", "slice": "data-ownership",
            "statement": "Repository rule", "content": "Repository rule",
            "evidence": ["agent_tests/test_rule.py:4"], "status": "candidate",
            "confidence": "low", "provenance": "inferred", "source": "task"}}],
            "task": {"task_id": "a1", "used_facts": [],
                     "produced_facts": ["fact:do-9003"], "decisions": []}}
        normalized, report = prepare_precision_batch(
            batch, self.task(), ["agent_tests/test_rule.py"], True, set(), True)
        self.assertEqual(normalized["mutations"][0]["fact"]["status"], "candidate")
        self.assertFalse(report["facts"][0]["evidence_in_diff"])


class WriteGuardTest(unittest.TestCase):
    def run_guard(self, root: Path, raw_path: str) -> subprocess.CompletedProcess:
        event = {"tool_name": "Write", "tool_input": {"file_path": raw_path}}
        return subprocess.run(
            [sys.executable, str(ROOT / "dataset" / "hooks" / "pre_tool_use.py")],
            input=json.dumps(event), text=True, capture_output=True,
            env={**os.environ, "KATA_WORKSPACE_ROOT": str(root)})

    def test_denies_protected_traversal_and_allows_source_and_agent_tests(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "tests").mkdir()
            (root / "src").mkdir()
            (root / "agent_tests").mkdir()
            self.assertEqual(self.run_guard(root, "tests/test_old.py").returncode, 2)
            self.assertEqual(self.run_guard(root, "src/../tests/test_old.py").returncode, 2)
            self.assertEqual(self.run_guard(root, "src/module.py").returncode, 0)
            self.assertEqual(self.run_guard(root, "agent_tests/test_new.py").returncode, 0)


class TestManifestTest(unittest.TestCase):
    def test_hash_mode_and_git_index_detect_content_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "tests").mkdir()
            test = root / "tests" / "test_old.py"
            test.write_text("assert True\n")
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "add", "tests/test_old.py"], cwd=root, check=True)
            manifest = build_manifest(root)
            self.assertTrue(verify(root, manifest)["ok"])
            test.write_text("assert False\n")
            with self.assertRaisesRegex(Exception, "base tests changed"):
                verify(root, manifest)


if __name__ == "__main__":
    unittest.main()
