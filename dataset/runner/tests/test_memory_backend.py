from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import sys

RUNNER = Path(__file__).resolve().parents[1]
ROOT = RUNNER.parents[1]
sys.path.insert(0, str(RUNNER))

from memory_backend import FileMemoryBackend, MemoryError, to_xmemory_mutations  # noqa: E402
from run import analytical_eligibility, feature_lift  # noqa: E402


SNAPSHOT = ROOT / "dataset" / "facts" / "snapshot-c0.md"


def batch(task: str, injected: list[str], create_id: str = "fact:gt-9001") -> dict:
    return {
        "mutations": [{
            "op": "create",
            "fact": {
                "fact_id": create_id,
                "slice": "gotchas",
                "statement": "Run repository regression tests after changing a shared mixin",
                "content": "A shared mixin change regressed unrelated repository tests.",
                "evidence": ["pytest_regression.log:1"],
                "status": "candidate",
                "confidence": "low",
                "provenance": "inferred",
                "source": "task",
            },
        }],
        "task": {"task_id": task, "title": task, "used_facts": injected[:1],
                 "produced_facts": [create_id]},
    }


def active_batch(task: str, injected: list[str]) -> dict:
    doc = batch(task, injected, "fact:ac-9001")
    fact = doc["mutations"][0]["fact"]
    fact.update({"slice": "api-contracts", "status": "active", "confidence": "high",
                 "provenance": "observed", "statement": "Shared mixin errors use HTTP conflicts",
                 "content": "Shared mixin errors use HTTP conflicts after verification."})
    return doc


class MemoryStateMachineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def backend(self, name: str, mode: str = "memory-on", seed: int = 1):
        return FileMemoryBackend(self.root / name, SNAPSHOT, mode, seed)

    def test_mode_and_seed_isolation_from_identical_c0(self):
        on = self.backend("on", "memory-on", 1)
        evolve = self.backend("evolve", "memory-on+evolve", 1)
        seed2 = self.backend("seed2", "memory-on", 2)
        states = [b.prepare() for b in (on, evolve, seed2)]
        self.assertEqual(len({s["c0_sha256"] for s in states}), 1)
        read = on.read("a1", ["data-ownership"], "normalization")
        on.apply(batch("a1", read.metrics["fact_ids"]), read.metrics["fact_ids"], "a1")
        self.assertIn("fact:gt-9001", on._load()["facts"])
        self.assertNotIn("fact:gt-9001", evolve._load()["facts"])
        self.assertNotIn("fact:gt-9001", seed2._load()["facts"])

    def test_restart_durability_and_chronological_read_after_write(self):
        first = self.backend("stream")
        first.prepare()
        read1 = first.read("a1", ["data-ownership"], "repository")
        first.apply(active_batch("a1", read1.metrics["fact_ids"]), read1.metrics["fact_ids"], "a1")
        restarted = self.backend("stream")
        state = restarted.prepare()
        self.assertEqual(state["state_version"], 1)
        read2 = restarted.read("a2", ["api-contracts"], "shared mixin conflict")
        self.assertIn("fact:ac-9001", read2.metrics["fact_ids"])
        self.assertEqual(state["tasks"]["a1"]["produced_facts"], ["fact:ac-9001"])

    def test_task_relevant_retrieval_never_dumps_other_slices(self):
        backend = self.backend("relevant")
        backend.prepare()
        result = backend.read("a3", ["api-contracts", "invariants"], "IntegrityError HTTP")
        self.assertTrue(result.facts)
        self.assertTrue(all(f["slice"] in {"api-contracts", "invariants"} for f in result.facts))
        self.assertNotIn("fact:cf-0001", result.metrics["fact_ids"])
        self.assertLess(len(result.exact_text), len(SNAPSHOT.read_text(encoding="utf-8")))

    def test_evolution_checkpoint_is_between_versions_and_cannot_create(self):
        backend = self.backend("evolution", "memory-on+evolve")
        backend.prepare()
        read = backend.read("a3", ["api-contracts"], "conflict")
        backend.apply({"mutations": [], "task": {"task_id": "a3", "title": "a3",
                                                   "used_facts": read.metrics["fact_ids"][:1],
                                                   "produced_facts": []}},
                      read.metrics["fact_ids"], "a3")
        checkpoint = backend.evolve({"mutations": [], "schema_changes": [], "report": "deduped"})
        self.assertEqual(checkpoint["checkpoint"], "after-a3-before-a4")
        self.assertEqual((checkpoint["state_version_before"], checkpoint["state_version_after"]), (1, 2))
        with self.assertRaises(MemoryError):
            backend.evolve({"mutations": [{"op": "create", "fact_id": "fact:gt-9999"}]})

    def test_task_may_only_claim_injected_facts(self):
        backend = self.backend("provenance")
        backend.prepare()
        read = backend.read("a1", ["data-ownership"], "normalization")
        bad = batch("a1", ["fact:ac-0001"])
        with self.assertRaises(MemoryError):
            backend.apply(bad, read.metrics["fact_ids"], "a1")

    def test_xmemory_translation_keeps_task_relations_typed(self):
        translated = to_xmemory_mutations(batch("a1", ["fact:do-0001"]))
        relation_types = [m["relation_mutation"]["relation_type"]
                          for m in translated["structured_mutations"] if "relation_mutation" in m]
        self.assertIn("task_used_data_ownership", relation_types)
        self.assertIn("task_produced_gotcha", relation_types)


class AnalyticsTest(unittest.TestCase):
    def test_feature_lift_and_boundaries(self):
        self.assertEqual(feature_lift(25, 23, 27), (0.5, None))
        self.assertEqual(feature_lift(22, 23, 27), (-0.25, None))
        self.assertEqual(feature_lift(5, 5, 5), (None, "non_positive_oracle_gap"))
        self.assertEqual(feature_lift(5, None, 7), (None, "missing_null_or_oracle"))

    def test_analytical_eligibility_is_stricter_than_technical_validity(self):
        eligible, reasons = analytical_eligibility(
            True, {"green": True}, {"agent_changed_existing_tests": False}, True, True, True)
        self.assertTrue(eligible)
        self.assertEqual(reasons, [])
        eligible, reasons = analytical_eligibility(
            True, {"green": False}, {"agent_changed_existing_tests": True}, True, True, True)
        self.assertFalse(eligible)
        self.assertEqual(reasons, ["regression_red", "existing_tests_modified_or_deleted"])


class HookTest(unittest.TestCase):
    def test_session_start_injects_exact_prepared_context_and_logs_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context = root / "context.json"
            exact = '{"facts":[{"fact_id":"fact:ac-0004","content":"conflict mapping"}]}'
            context.write_text(exact, encoding="utf-8")
            run_dir = root / "run"
            env = {**os.environ, "KATA_MEMORY_MODE": "prepared",
                   "KATA_FACTS_CONTEXT": str(context), "KATA_RUN_DIR": str(run_dir)}
            result = subprocess.run(["bash", str(ROOT / "dataset" / "hooks" / "session_start.sh")],
                                    text=True, capture_output=True, env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(exact, result.stdout)
            self.assertEqual((run_dir / "context_injected.txt").read_text(encoding="utf-8"), exact)
            self.assertTrue((run_dir / "hook_session_start.fired").exists())

    def test_stop_hook_requests_one_task_specific_write(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {**os.environ, "KATA_RUN_DIR": str(Path(directory) / ".kata-run"),
                   "KATA_TASK_ID": "a3"}
            command = [sys.executable, str(ROOT / "dataset" / "hooks" / "stop.py")]
            first = subprocess.run(command, input="{}", text=True, capture_output=True, env=env)
            second = subprocess.run(command, input="{}", text=True, capture_output=True, env=env)
            self.assertEqual(first.returncode, 2)
            self.assertIn('"task_id":"a3"', first.stderr)
            self.assertEqual(second.returncode, 0)


if __name__ == "__main__":
    unittest.main()
