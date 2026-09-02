from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path


RUNNER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RUNNER))

from cloud_preflight import SENTINEL, durability_batch  # noqa: E402
from evolve import curator_session_id  # noqa: E402
from memory_backend import validate_batch  # noqa: E402
from sweep import (CANARY_TASKS, MODES, canary_gate, dirty_experiment_entries,
                   completed_stream_retention, evolution_attachment_task,
                   should_abort_stream, valid_protection_preflight)  # noqa: E402


def preflight() -> dict:
    attempts = {
        "modify_blocked": True,
        "delete_blocked": True,
        "chmod_blocked": True,
        "file_rename_blocked": True,
        "addition_under_tests_blocked": True,
        "tree_rename_blocked": True,
        "addition_allowed": True,
        "seatbelt_arbitrary_python_blocked": True,
    }
    return {"ok": True, "base_manifest_sha256": "manifest",
            "lock_held": {"ok": True, "immutable_flags_held": True,
                          "protected_hash_mode_equal": True},
            "pristine": {"ok": True, "hash_mode_equal": True,
                         "base_manifest_sha256": "manifest"},
            "attempts": attempts}


class CanaryFixture:
    def __init__(self, root: Path):
        self.root = root
        self.cfg = {"memory": {"c0_instance_id": "c0"},
                    "experiment": {"curator_after_task": "a4"}}
        self.ids = {
            "memory-on": {"a1": "on-a1", "a4": "on-a4", "a6": "on-a6"},
            "memory-on+evolve": {
                "a1": "ev-a1", "a4": "ev-a4",
                "evolve-after-a4": "ev-curator", "a6": "ev-a6",
            },
        }
        self.rows = [self.row(task, mode) for task in CANARY_TASKS for mode in MODES]
        self.write_retention_artifacts()

    @staticmethod
    def retention(deleted: str, child: str) -> dict:
        return {"ok": True, "instance_id": deleted,
                "deleted_instance_id": deleted, "after_verified_clone": child}

    def row(self, task: str, mode: str) -> dict:
        retrieval = {}
        if mode != "memory-off":
            child = self.ids[mode][task]
            retrieval = {"instance_id": child, "precision": 1.0, "coverage": 1.0}
            previous = {
                ("memory-on", "a4"): "on-a1",
                ("memory-on", "a6"): "on-a4",
                ("memory-on+evolve", "a4"): "ev-a1",
                ("memory-on+evolve", "a6"): "ev-curator",
            }.get((mode, task))
            if previous:
                retrieval["retention"] = self.retention(previous, child)
        return {
            "task": task, "mode": mode, "seed": 1,
            "valid_run": True, "analytical_eligible": True,
            "analytical_ineligible_reasons": [],
            "test_protection": {"proof": {"ok": True}},
            "regression": {"green": True},
            "attribution": {"complete": True, "links": []},
            "retrieval": retrieval,
            "retrieval_leakage_check": {"ok": True},
        }

    def write_retention_artifacts(self) -> None:
        for mode, by_session in self.ids.items():
            sessions = []
            parent = "c0"
            for session_id, instance_id in by_session.items():
                sessions.append({
                    "session_id": session_id,
                    "parent_instance_id": parent,
                    "instance_id": instance_id,
                    "verification": "verified",
                    "state_version": len(sessions),
                    "state_sha256": f"sha-{instance_id}",
                    "deleted_at": "2026-09-02T00:00:00Z",
                    "delete": {"ok": True, "instance_id": instance_id},
                })
                parent = instance_id
            state = self.root / "_memory" / mode / "seed1"
            state.mkdir(parents=True)
            (state / "lineage.json").write_text(
                json.dumps({"format_version": 1, "mode": mode, "seed": 1,
                            "c0_instance_id": "c0", "sessions": sessions}),
                encoding="utf-8")
            (state / "cleanup.json").write_text(
                json.dumps({"ok": True, "instance_id": sessions[-1]["instance_id"]}),
                encoding="utf-8")
        evolution = self.root / "_evolution" / "memory-on+evolve" / "seed1"
        evolution.mkdir(parents=True)
        (evolution / "evolution.json").write_text(json.dumps({
            "instance_id": "ev-curator",
            "retention": self.retention("ev-a4", "ev-curator"),
        }), encoding="utf-8")

    def gate(self, rows: list[dict] | None = None, tasks: list[str] | None = None,
             modes: list[str] | None = None, receipt: dict | None = None) -> dict:
        return canary_gate(rows if rows is not None else self.rows, self.root,
                           tasks if tasks is not None else CANARY_TASKS,
                           modes if modes is not None else MODES,
                           self.cfg, "same-c0", "same-c0",
                           preflight() if receipt is None else receipt)


class ExactCanaryGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fixture = CanaryFixture(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_exact_green_a1_a4_a6_three_mode_seed1_shape_passes(self):
        gate = self.fixture.gate()
        self.assertTrue(gate["passed"], gate)
        self.assertEqual(gate["actual_children"], 7)
        self.assertEqual(gate["delete_receipts"], 7)

    def test_subset_or_wrong_order_cannot_pass_shape(self):
        subset = [row for row in self.fixture.rows if row["mode"] == "memory-on"]
        self.assertFalse(self.fixture.gate(subset, modes=["memory-on"])["checks"][
            "canary_shape_exact"])
        self.assertFalse(self.fixture.gate(tasks=["a4", "a1", "a6"])["checks"][
            "canary_shape_exact"])

    def test_claimed_used_fact_requires_complete_trace(self):
        rows = copy.deepcopy(self.fixture.rows)
        rows[1]["attribution"]["links"] = [{
            "fact_id": "fact:ac-0001", "task_marked_used": True,
            "trace_complete": False,
        }]
        self.assertFalse(self.fixture.gate(rows)["checks"]["metrics_attribution_complete"])

    def test_delete_receipt_requires_ok_and_matching_instance(self):
        lineage = (self.fixture.root / "_memory" / "memory-on" /
                   "seed1" / "lineage.json")
        payload = json.loads(lineage.read_text())
        payload["sessions"][0]["delete"] = {"ok": False, "instance_id": "wrong"}
        lineage.write_text(json.dumps(payload))
        gate = self.fixture.gate()
        self.assertFalse(gate["checks"]["expected_children_with_delete_receipts"])
        self.assertTrue(any("delete_not_ok" in issue for issue in gate["retention_issues"]))
        self.assertTrue(any("delete_instance_mismatch" in issue
                            for issue in gate["retention_issues"]))

    def test_parent_chain_and_verified_clone_are_required(self):
        lineage = (self.fixture.root / "_memory" / "memory-on" /
                   "seed1" / "lineage.json")
        payload = json.loads(lineage.read_text())
        payload["sessions"][1]["parent_instance_id"] = "unrelated"
        lineage.write_text(json.dumps(payload))
        rows = copy.deepcopy(self.fixture.rows)
        target = next(row for row in rows
                      if row["task"] == "a4" and row["mode"] == "memory-on")
        target["retrieval"]["retention"].pop("after_verified_clone")
        gate = self.fixture.gate(rows)
        self.assertFalse(gate["checks"]["expected_children_with_delete_receipts"])
        self.assertTrue(any("parent_chain_broken" in issue
                            for issue in gate["retention_issues"]))
        self.assertTrue(any("after_verified_clone_mismatch" in issue
                            for issue in gate["retention_issues"]))


class RunnerHardeningTest(unittest.TestCase):
    def test_cloud_preflight_batch_satisfies_claimed_use_trace_contract(self):
        batch = durability_batch(["fact:do-0005", "fact:do-0006"])
        validated = validate_batch(
            batch, ["fact:do-0005", "fact:do-0006"], "a1")
        self.assertEqual(validated["task"]["produced_facts"], [SENTINEL])
        self.assertEqual(len(validated["task"]["decisions"]), 2)

    def test_preflight_is_fail_closed(self):
        report = preflight()
        self.assertTrue(valid_protection_preflight(report))
        report["attempts"]["delete_blocked"] = False
        self.assertFalse(valid_protection_preflight(report))
        self.assertFalse(valid_protection_preflight(None))

    def test_dirty_output_refused_but_preflight_only_is_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "_preflight").mkdir()
            (root / "_preflight" / "test_protection.json").write_text("{}")
            self.assertEqual(dirty_experiment_entries(root), [])
            partial = root / "a1" / "memory-on" / "seed1"
            partial.mkdir(parents=True)
            (partial / "metrics.json").write_text("{}")
            self.assertEqual(dirty_experiment_entries(root), ["a1"])

    def test_technical_failure_aborts_affected_memory_stream(self):
        self.assertTrue(should_abort_stream("memory-on", 2, False))
        self.assertTrue(should_abort_stream("memory-on+evolve", 2, False))
        self.assertFalse(should_abort_stream("memory-off", 2, False))
        self.assertTrue(should_abort_stream("memory-off", 2, True))
        self.assertFalse(should_abort_stream("memory-on", 0, True))

    def test_curator_session_id_follows_configured_boundary(self):
        self.assertEqual(curator_session_id("a4"), "evolve-after-a4")
        self.assertEqual(curator_session_id("a3"), "evolve-after-a3")

    def test_curator_cost_attaches_once_to_first_post_boundary_task(self):
        evolution = {"checkpoint": {"checkpoint": "after-a3-before-next"}}
        self.assertEqual(evolution_attachment_task(
            evolution, ["a1", "a2", "a3", "a4", "a5", "a6"]), "a4")
        self.assertIsNone(evolution_attachment_task(
            {"checkpoint": {"checkpoint": "after-a6-before-next"}}, ["a1", "a6"]))

    def test_completed_resume_stream_requires_verified_retention_and_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = CanaryFixture(Path(tmp))
            rows = [row for row in fixture.rows if row["mode"] == "memory-on"]
            report = completed_stream_retention(
                rows, Path(tmp), fixture.cfg, "memory-on", 1, CANARY_TASKS, "a4")
            self.assertTrue(report["ok"], report)
            lineage_path = Path(tmp) / "_memory" / "memory-on" / "seed1" / "lineage.json"
            lineage = json.loads(lineage_path.read_text())
            lineage["sessions"][1]["verification"] = "pending"
            lineage_path.write_text(json.dumps(lineage))
            report = completed_stream_retention(
                rows, Path(tmp), fixture.cfg, "memory-on", 1, CANARY_TASKS, "a4")
            self.assertFalse(report["ok"])
            self.assertTrue(any("clone_not_verified" in issue for issue in report["issues"]))


if __name__ == "__main__":
    unittest.main()
