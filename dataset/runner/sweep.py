#!/usr/bin/env python3
"""
Прогон матрицы: задачи × режимы × повторы. Собирает результаты в таблицу.

    # самопроверка каркаса — обязательна перед первым настоящим прогоном
    python dataset/runner/sweep.py --selftest

    # full three-mode matrix (paid; only after canary)
    python dataset/runner/sweep.py --seeds 2

    # одна задача, быстро
    python dataset/runner/sweep.py --tasks a3 --seeds 1

Порядок режимов чередуется между сидами и задачами — этого требует протокол эвала,
иначе систематический эффект «второй прогон всегда теплее» ляжет на один режим.
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import tempfile
import tomllib
from statistics import median
from pathlib import Path

from memory_backend import (MemoryError, SubprocessXMemoryTransport,
                            cleanup_xmemory_tail, state_sha256)
from run import drop_workspace, ensure_clone, load_tasks, make_workspace
from test_protection import TestProtectionError, adversarial_preflight

try:
    import yaml
except ImportError:
    sys.exit("нужен pyyaml: pip install pyyaml")

ROOT = Path(__file__).resolve().parents[2]
RUN = [sys.executable, str(Path(__file__).with_name("run.py"))]
EVOLVE = [sys.executable, str(Path(__file__).with_name("evolve.py"))]
MODES = ["memory-off", "memory-on", "memory-on+evolve"]
CANARY_TASKS = ["a1", "a4", "a6"]
CANARY_SEED = 1
PREFLIGHT_ATTEMPTS = {
    "modify_blocked", "delete_blocked", "chmod_blocked", "file_rename_blocked",
    "addition_under_tests_blocked", "tree_rename_blocked", "addition_allowed",
    "seatbelt_arbitrary_python_blocked",
}


def task_ids(tasks_path: Path) -> list[str]:
    doc = yaml.safe_load(tasks_path.read_text(encoding="utf-8"))
    return [t["id"] for t in doc.get("tasks", [])]


def one(task: str, mode: str, seed: int, extra: list[str], out: str = "runs",
        memory_state: str | None = None, reset_memory: bool = False) -> int:
    cmd = RUN + ["--task", task, "--mode", mode, "--seed", str(seed), "--out", out] + extra
    if memory_state:
        cmd += ["--memory-state", memory_state]
    if reset_memory:
        cmd.append("--reset-memory")
    print(f"\n=== {task} · {mode} · seed{seed} " + "=" * 30)
    return subprocess.run(cmd, cwd=ROOT).returncode


def cleanup_tail(state: str, cfg: dict, out: str) -> bool:
    """Delete only this runner stream's live tail; never prune organisation-wide instances."""
    if not cfg.get("memory", {}).get("delete_stream_tail", False):
        return True
    state_dir = (ROOT / state).resolve()
    try:
        result = cleanup_xmemory_tail(
            state_dir, cfg.get("memory", {}).get("xmemcli", "xmemcli"))
    except MemoryError as exc:
        print(f"[retention] stream-tail cleanup failed: {exc}", file=sys.stderr)
        return False
    artifact = state_dir / "cleanup.json"
    artifact.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"[retention] deleted runner-owned stream tail {result['instance_id']}")
    return True


def collect(out_dir: Path, expected: set[tuple[str, str, int]] | None = None) -> list[dict]:
    """Только настоящие прогоны текущей матрицы.

    Псевдоагенты null и oracle живут в отдельном каталоге, но фильтр по полю
    agent оставлен на случай ручных запусков: одна забытая oracle-строка
    превращается в «победу memory-off» в сводке.
    """
    rows = []
    for m in sorted(out_dir.rglob("metrics.json")):
        r = json.loads(m.read_text(encoding="utf-8"))
        if r.get("agent") in ("null", "oracle"):
            continue
        key = (r["task"], r["mode"], r["seed"])
        if expected is not None and key not in expected:
            continue
        rows.append(r)
    return rows


def paired_eligibility(rows: list[dict], modes: list[str]) -> dict[tuple[str, int], dict]:
    """A task/seed enters primary only when every requested mode is valid+eligible."""
    grouped: dict[tuple[str, int], dict[str, list[dict]]] = {}
    for row in rows:
        grouped.setdefault((row["task"], row["seed"]), {}).setdefault(row["mode"], []).append(row)
    result = {}
    for key, by_mode in grouped.items():
        reasons = []
        for mode in modes:
            cells = by_mode.get(mode, [])
            if len(cells) != 1:
                reasons.append(f"{mode}:missing_or_duplicate")
                continue
            cell = cells[0]
            if not cell.get("valid_run"):
                reasons.append(f"{mode}:technical_invalid")
            if not cell.get("analytical_eligible"):
                detail = ",".join(cell.get("analytical_ineligible_reasons") or []) or "ineligible"
                reasons.append(f"{mode}:{detail}")
        result[key] = {"paired_eligible": not reasons, "reasons": reasons}
    return result


def dirty_experiment_entries(out_dir: Path) -> list[str]:
    """Return output entries which prove this is not a fresh experiment directory.

    The free protection preflight is deliberately allowed to precede the paid run in
    the same directory. Everything else requires the declared resume policy.
    """
    if not out_dir.exists():
        return []
    dirty = []
    for path in out_dir.iterdir():
        if path.name == "_preflight":
            continue
        dirty.append(path.relative_to(out_dir).as_posix())
    return sorted(dirty)


def valid_protection_preflight(report: dict | None) -> bool:
    """Fail closed unless the complete adversarial receipt says every probe passed."""
    if not isinstance(report, dict) or report.get("ok") is not True:
        return False
    lock = report.get("lock_held") or {}
    pristine = report.get("pristine") or {}
    if (lock.get("ok") is not True
            or lock.get("immutable_flags_held") is not True
            or lock.get("protected_hash_mode_equal") is not True):
        return False
    if pristine.get("ok") is not True or pristine.get("hash_mode_equal") is not True:
        return False
    manifest = report.get("base_manifest_sha256")
    if not manifest or pristine.get("base_manifest_sha256") != manifest:
        return False
    attempts = report.get("attempts")
    return (isinstance(attempts, dict)
            and PREFLIGHT_ATTEMPTS.issubset(attempts)
            and all(attempts.get(name) is True for name in PREFLIGHT_ATTEMPTS))


def load_protection_preflight(path: Path) -> dict | None:
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not valid_protection_preflight(report):
        return None
    return report


def attribution_trace_complete(row: dict) -> bool:
    """Every claimed-used fact must carry a trace to an actual diff path."""
    attribution = row.get("attribution") or {}
    links = attribution.get("links")
    if attribution.get("complete") is not True or not isinstance(links, list):
        return False
    return all(item.get("task_marked_used") is not True
               or item.get("trace_complete") is True for item in links)


def should_abort_stream(mode: str, returncode: int, fail_fast: bool) -> bool:
    """A technical memory failure invalidates all later chronological cells."""
    return returncode != 0 and (mode != "memory-off" or fail_fast)


def _load_json(path: Path, issues: list[str], label: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("root is not an object")
        return value
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        issues.append(f"{label}_unreadable:{exc}")
        return {}


def completed_stream_retention(rows: list[dict], out_dir: Path, cfg: dict,
                               mode: str, seed: int, tasks: list[str],
                               curator_after: str) -> dict:
    """Prove a resumed cloud stream is complete, chronological, and scoped-cleaned."""
    issues: list[str] = []
    state_dir = out_dir / "_memory" / mode / f"seed{seed}"
    lineage = _load_json(state_dir / "lineage.json", issues, "lineage")
    if lineage.get("mode") != mode:
        issues.append("lineage_mode_mismatch")
    if lineage.get("seed") != seed:
        issues.append("lineage_seed_mismatch")
    c0_instance = cfg.get("memory", {}).get("c0_instance_id")
    if lineage.get("c0_instance_id") != c0_instance:
        issues.append("lineage_c0_mismatch")
    sessions = lineage.get("sessions")
    if not isinstance(sessions, list):
        sessions = []
        issues.append("lineage_sessions_invalid")

    expected_session_ids = list(tasks)
    evolution: dict = {}
    if mode == "memory-on+evolve" and curator_after in tasks:
        expected_session_ids.insert(
            expected_session_ids.index(curator_after) + 1,
            f"evolve-after-{curator_after}")
        evolution = _load_json(
            out_dir / "_evolution" / mode / f"seed{seed}" / "evolution.json",
            issues, "evolution")
    actual_session_ids = [item.get("session_id") for item in sessions]
    if actual_session_ids != expected_session_ids:
        issues.append(f"session_order:{actual_session_ids!r}!={expected_session_ids!r}")

    expected_parent = c0_instance
    seen: set[str] = set()
    for index, session in enumerate(sessions):
        instance_id = session.get("instance_id")
        if not instance_id or instance_id in seen:
            issues.append(f"session{index}:missing_or_duplicate_instance_id")
        else:
            seen.add(instance_id)
        if session.get("parent_instance_id") != expected_parent:
            issues.append(f"session{index}:parent_chain_broken")
        expected_parent = instance_id
        if session.get("verification") != "verified":
            issues.append(f"session{index}:clone_not_verified")
        if session.get("state_version") is None or not session.get("state_sha256"):
            issues.append(f"session{index}:state_proof_missing")
        receipt = session.get("delete") or {}
        if not session.get("deleted_at"):
            issues.append(f"session{index}:deleted_at_missing")
        if receipt.get("ok") is not True:
            issues.append(f"session{index}:delete_not_ok")
        if receipt.get("instance_id") != instance_id:
            issues.append(f"session{index}:delete_instance_mismatch")

    row_by_child = {
        (row.get("retrieval") or {}).get("instance_id"): row
        for row in rows if row.get("mode") == mode and row.get("seed") == seed
    }
    retention_by_child = {
        child: (row.get("retrieval") or {}).get("retention") or {}
        for child, row in row_by_child.items() if child
    }
    if evolution.get("instance_id"):
        retention_by_child[evolution["instance_id"]] = evolution.get("retention") or {}
    for current, following in zip(sessions, sessions[1:]):
        current_id = current.get("instance_id")
        following_id = following.get("instance_id")
        receipt = retention_by_child.get(following_id) or {}
        if receipt.get("ok") is not True:
            issues.append(f"{current_id}:retention_not_ok")
        if receipt.get("instance_id") != current_id:
            issues.append(f"{current_id}:retention_instance_mismatch")
        if receipt.get("deleted_instance_id") != current_id:
            issues.append(f"{current_id}:deleted_instance_mismatch")
        if receipt.get("after_verified_clone") != following_id:
            issues.append(f"{current_id}:after_verified_clone_mismatch")

    cleanup = _load_json(state_dir / "cleanup.json", issues, "cleanup")
    tail_id = sessions[-1].get("instance_id") if sessions else None
    if cleanup.get("ok") is not True:
        issues.append("cleanup_not_ok")
    if cleanup.get("instance_id") != tail_id:
        issues.append("cleanup_instance_mismatch")
    return {"ok": not issues, "issues": issues,
            "expected_sessions": expected_session_ids,
            "actual_sessions": actual_session_ids}


def evolution_attachment_task(evolution: dict, ordered_tasks: list[str]) -> str | None:
    """Attach a one-off curator session to the first measured task after its boundary."""
    checkpoint = (evolution.get("checkpoint") or {}).get("checkpoint")
    if not isinstance(checkpoint, str) or not checkpoint.startswith("after-"):
        return None
    after_task = checkpoint.removeprefix("after-").removesuffix("-before-next")
    if after_task not in ordered_tasks:
        return None
    index = ordered_tasks.index(after_task) + 1
    return ordered_tasks[index] if index < len(ordered_tasks) else None


def _retention_evidence(rows: list[dict], out_dir: Path, cfg: dict) -> dict:
    """Validate exact child lineage and scoped delete receipts for the canary."""
    c0_instance = cfg.get("memory", {}).get("c0_instance_id")
    curator_after = cfg.get("experiment", {}).get("curator_after_task")
    issues: list[str] = []
    sessions_by_mode: dict[str, list[dict]] = {}
    all_sessions: list[dict] = []

    if curator_after not in CANARY_TASKS[:-1]:
        issues.append(f"curator_boundary_invalid:{curator_after}")

    for mode in MODES[1:]:
        lineage_path = out_dir / "_memory" / mode / "seed1" / "lineage.json"
        try:
            lineage = json.loads(lineage_path.read_text(encoding="utf-8"))
            sessions = lineage.get("sessions")
            if not isinstance(sessions, list):
                raise ValueError("sessions is not a list")
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            issues.append(f"{mode}:lineage_unreadable:{exc}")
            sessions = []
        sessions_by_mode[mode] = sessions
        all_sessions.extend(sessions)

        expected_session_ids = list(CANARY_TASKS)
        if mode == "memory-on+evolve" and curator_after in CANARY_TASKS[:-1]:
            insert_at = expected_session_ids.index(curator_after) + 1
            expected_session_ids.insert(insert_at, f"evolve-after-{curator_after}")
        actual_session_ids = [item.get("session_id") for item in sessions]
        if actual_session_ids != expected_session_ids:
            issues.append(
                f"{mode}:session_order:{actual_session_ids!r}!={expected_session_ids!r}")

        expected_parent = c0_instance
        seen_instances: set[str] = set()
        for index, session in enumerate(sessions):
            instance_id = session.get("instance_id")
            if not instance_id or instance_id in seen_instances:
                issues.append(f"{mode}:session{index}:missing_or_duplicate_instance_id")
            else:
                seen_instances.add(instance_id)
            if session.get("parent_instance_id") != expected_parent:
                issues.append(f"{mode}:session{index}:parent_chain_broken")
            expected_parent = instance_id
            receipt = session.get("delete") or {}
            if not session.get("deleted_at"):
                issues.append(f"{mode}:session{index}:deleted_at_missing")
            if receipt.get("ok") is not True:
                issues.append(f"{mode}:session{index}:delete_not_ok")
            if receipt.get("instance_id") != instance_id:
                issues.append(f"{mode}:session{index}:delete_instance_mismatch")

    retention_by_child: dict[str, dict] = {}
    for row in rows:
        if row.get("mode") == "memory-off":
            continue
        retrieval = row.get("retrieval") or {}
        instance_id = retrieval.get("instance_id")
        if instance_id:
            retention_by_child[instance_id] = retrieval.get("retention") or {}
    evolution_path = (out_dir / "_evolution" / "memory-on+evolve" /
                      "seed1" / "evolution.json")
    try:
        evolution = json.loads(evolution_path.read_text(encoding="utf-8"))
        if evolution.get("instance_id"):
            retention_by_child[evolution["instance_id"]] = evolution.get("retention") or {}
    except (OSError, json.JSONDecodeError) as exc:
        issues.append(f"memory-on+evolve:evolution_unreadable:{exc}")

    for mode, sessions in sessions_by_mode.items():
        for current, following in zip(sessions, sessions[1:]):
            current_id = current.get("instance_id")
            following_id = following.get("instance_id")
            receipt = retention_by_child.get(following_id) or {}
            if receipt.get("ok") is not True:
                issues.append(f"{mode}:{current_id}:retention_not_ok")
            if receipt.get("instance_id") != current_id:
                issues.append(f"{mode}:{current_id}:retention_instance_mismatch")
            if receipt.get("deleted_instance_id") != current_id:
                issues.append(f"{mode}:{current_id}:deleted_instance_mismatch")
            if receipt.get("after_verified_clone") != following_id:
                issues.append(f"{mode}:{current_id}:after_verified_clone_mismatch")
        if sessions:
            tail_id = sessions[-1].get("instance_id")
            cleanup_path = out_dir / "_memory" / mode / "seed1" / "cleanup.json"
            try:
                cleanup = json.loads(cleanup_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                issues.append(f"{mode}:cleanup_unreadable:{exc}")
                cleanup = {}
            if cleanup.get("ok") is not True:
                issues.append(f"{mode}:cleanup_not_ok")
            if cleanup.get("instance_id") != tail_id:
                issues.append(f"{mode}:cleanup_instance_mismatch")

    valid_receipts = sum(
        bool(item.get("deleted_at")
             and (item.get("delete") or {}).get("ok") is True
             and (item.get("delete") or {}).get("instance_id") == item.get("instance_id"))
        for item in all_sessions)
    return {
        "ok": not issues,
        "issues": issues,
        "actual_children": len(all_sessions),
        "valid_delete_receipts": valid_receipts,
    }


def canary_gate(rows: list[dict], out_dir: Path, tasks: list[str], modes: list[str],
                cfg: dict, c0_before: str | None, c0_after: str | None,
                protection_preflight: dict | None = None) -> dict:
    """Strict no-full gate for the precision a1→a4→a6 canary."""
    expected_keys = {(task, mode, CANARY_SEED) for task in CANARY_TASKS for mode in MODES}
    actual_keys = [(row.get("task"), row.get("mode"), row.get("seed")) for row in rows]
    expected_rows = len(expected_keys)
    pairs = paired_eligibility(rows, MODES)
    memory_rows = [row for row in rows if row["mode"] != "memory-off"]
    precisions = [row.get("retrieval", {}).get("precision") for row in memory_rows]
    precisions = [value for value in precisions if value is not None]
    coverages = [row.get("retrieval", {}).get("coverage") for row in memory_rows]
    coverages = [value for value in coverages if value is not None]
    retention = _retention_evidence(rows, out_dir, cfg)
    expected_children = 7
    exact_shape = (tasks == CANARY_TASKS and modes == MODES
                   and len(actual_keys) == expected_rows
                   and set(actual_keys) == expected_keys)
    checks = {
        "canary_shape_exact": exact_shape,
        "rows_technically_valid": exact_shape
                                  and all(row.get("valid_run") for row in rows),
        "every_task_seed_paired_eligible": set(pairs) == {
            (task, CANARY_SEED) for task in CANARY_TASKS}
                                           and all(item["paired_eligible"] for item in pairs.values()),
        "test_protection_preflight_passed": valid_protection_preflight(protection_preflight),
        "base_tests_pristine": all((row.get("test_protection") or {}).get("proof", {}).get("ok")
                                   for row in rows),
        "regression_green": all(row.get("regression", {}).get("green") for row in rows),
        "metrics_attribution_complete": all(attribution_trace_complete(row) for row in rows),
        "retrieval_precision_materially_higher": bool(precisions)
                                                  and median(precisions) >= 0.75
                                                  and median(precisions) > 0.282,
        "retrieval_coverage_complete": bool(coverages) and min(coverages) == 1.0,
        "retrieval_no_hidden_leakage": all(
            row.get("retrieval_leakage_check", {}).get("ok") for row in memory_rows),
        "expected_children_with_delete_receipts": (
            retention["ok"] and retention["actual_children"] == expected_children
            and retention["valid_delete_receipts"] == expected_children),
        "c0_digest_unchanged": bool(c0_before and c0_after and c0_before == c0_after),
    }
    return {
        "gate": "precision-canary-v1", "passed": all(checks.values()), "checks": checks,
        "expected_rows": expected_rows, "actual_rows": len(rows),
        "paired_cells": {f"{task}/seed{seed}": value for (task, seed), value in pairs.items()},
        "retrieval_precision_median": median(precisions) if precisions else None,
        "retrieval_precision_range": [min(precisions), max(precisions)] if precisions else None,
        "retrieval_coverage_range": [min(coverages), max(coverages)] if coverages else None,
        "expected_children": expected_children, "actual_children": retention["actual_children"],
        "delete_receipts": retention["valid_delete_receipts"],
        "retention_issues": retention["issues"],
        "test_protection_preflight_manifest_sha256": (
            (protection_preflight or {}).get("base_manifest_sha256")),
        "c0_digest_before": c0_before, "c0_digest_after": c0_after,
        "curator_after_task": cfg.get("experiment", {}).get("curator_after_task"),
    }


def write_table(rows: list[dict], out_dir: Path, modes: list[str] | None = None) -> None:
    modes = modes or MODES
    pairs = paired_eligibility(rows, modes)
    cols = ["experiment_id", "protocol_mode", "task", "transfer_cluster", "mode", "seed", "agent", "agent_model", "agent_effort", "valid_run",
            "invalid_reasons", "analytical_eligible", "analytical_ineligible_reasons",
            "paired_eligible", "paired_ineligible_reasons",
            "task_success", "score", "hidden_micro_score", "score_binary", "feature_lift", "feature_passed",
            "feature_total", "agent_rc", "agent_attempts", "agent_transient_retries",
            "hidden_passed", "hidden_total", "hidden_failed",
            "regression_green", "tests_added", "existing_tests_modified", "existing_tests_deleted",
            "base_tests_pristine", "base_tests_manifest_sha256",
            "architecture_score", "architecture_green", "files_changed", "insertions", "deletions",
            "files_read", "time_to_first_relevant_file_sec", "wall_sec", "input_tokens", "output_tokens", "cache_read_tokens",
            "cache_creation_tokens", "total_cost_usd", "num_turns", "agent_touched_tests",
            "retrieval_backend", "retrieval_fallback", "injected_fact_ids", "context_chars",
            "context_tokens", "retrieval_precision", "retrieval_coverage", "irrelevant_facts",
            "retrieval_profile", "retrieval_threshold", "selection_reasons", "injected_origins",
            "retrieval_leakage_free", "attribution_complete", "attribution_traced_used_count",
            "promoted_transfer_facts_count",
            "memory_instance_id", "memory_parent_instance_id", "memory_session_instance_created",
            "memory_clone_wall_sec", "memory_clone_provider_calls",
            "memory_retention_deleted_instance_id", "memory_retention_wall_sec",
            "memory_retention_provider_calls",
            "memory_state_before", "memory_state_after", "memory_creates", "memory_updates",
            "memory_stale", "memory_noop", "memory_gotchas", "memory_schema_changes",
            "memory_read_wall_sec", "memory_write_wall_sec", "memory_read_provider_calls",
            "memory_write_provider_calls", "evolution_checkpoint",
            "curator_wall_sec", "curator_backend_wall_sec", "curator_output_tokens", "harmful_on_worse_off",
            "harmful_stale_fact_used", "harmful_regression_after_retrieval"]
    csv_path = out_dir / "results.csv"
    ordered_tasks = [task for task in task_ids(ROOT / "dataset" / "tasks.yaml")
                     if task in {row["task"] for row in rows}]
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        paired_off = {(r["task"], r["seed"]): r for r in rows if r["mode"] == "memory-off"}
        for r in rows:
            u = r.get("usage") or {}
            grading = r.get("grading") or {}
            retrieval = r.get("retrieval") or {}
            retention = retrieval.get("retention") or {}
            write = r.get("memory_write") or {}
            mutations = write.get("mutations") or {}
            tests = r.get("diff", {}).get("test_changes", {})
            evolve = None
            if r["mode"] == "memory-on+evolve":
                p = out_dir / "_evolution" / r["mode"] / f"seed{r['seed']}" / "evolution.json"
                if p.exists():
                    candidate = json.loads(p.read_text(encoding="utf-8"))
                    if r["task"] == evolution_attachment_task(candidate, ordered_tasks):
                        evolve = candidate
            evolve_usage = (evolve or {}).get("usage") or {}
            off = paired_off.get((r["task"], r["seed"]))
            off_lift = (off or {}).get("grading", {}).get("feature_lift")
            this_lift = grading.get("feature_lift")
            w.writerow({
                "experiment_id": r.get("experiment_id"), "protocol_mode": r.get("protocol_mode"),
                "task": r["task"], "transfer_cluster": r.get("transfer_cluster"),
                "mode": r["mode"], "seed": r["seed"], "agent": r["agent"],
                "agent_model": r.get("agent_model"),
                "agent_effort": r.get("agent_effort"),
                "valid_run": r.get("valid_run"),
                "invalid_reasons": ";".join(r.get("invalid_reasons") or []),
                "analytical_eligible": r.get("analytical_eligible"),
                "analytical_ineligible_reasons": ";".join(r.get("analytical_ineligible_reasons") or []),
                "paired_eligible": pairs.get((r["task"], r["seed"]), {}).get("paired_eligible", False),
                "paired_ineligible_reasons": ";".join(
                    pairs.get((r["task"], r["seed"]), {}).get("reasons", [])),
                "task_success": r.get("task_success"),
                "score": r["score"],
                "hidden_micro_score": r.get("hidden_micro_score", r.get("score")),
                "score_binary": r.get("score_binary"),
                "feature_lift": grading.get("feature_lift"),
                "feature_passed": grading.get("feature_passed"),
                "feature_total": grading.get("feature_total"),
                "agent_rc": r.get("agent_rc"),
                "agent_attempts": r.get("agent_attempts", 1),
                "agent_transient_retries": r.get("agent_transient_retries", 0),
                "hidden_passed": r["hidden"].get("passed"),
                "hidden_total": r["hidden"].get("tests", 0) - r["hidden"].get("skipped", 0),
                "hidden_failed": r["hidden"].get("failed", 0) + r["hidden"].get("errors", 0),
                "regression_green": r["regression"].get("green"),
                "tests_added": len(tests.get("added", [])),
                "existing_tests_modified": len(tests.get("modified_existing", [])),
                "existing_tests_deleted": len(tests.get("deleted_existing", [])),
                "base_tests_pristine": (r.get("test_protection") or {}).get("proof", {}).get("ok"),
                "base_tests_manifest_sha256": (r.get("test_protection") or {}).get(
                    "base_manifest_sha256"),
                "architecture_score": (r.get("architecture") or {}).get("score"),
                "architecture_green": (r.get("architecture") or {}).get("green"),
                "files_changed": r["diff"]["files_changed"],
                "insertions": r["diff"].get("insertions"),
                "deletions": r["diff"].get("deletions"),
                "files_read": (r.get("process") or {}).get("files_read"),
                "time_to_first_relevant_file_sec": (r.get("process") or {}).get("time_to_first_relevant_file_sec"),
                "wall_sec": r["wall_sec"],
                "input_tokens": u.get("input_tokens"),
                "output_tokens": u.get("output_tokens"),
                "cache_read_tokens": u.get("cache_read_tokens"),
                "cache_creation_tokens": u.get("cache_creation_tokens"),
                "total_cost_usd": u.get("total_cost_usd"),
                "num_turns": u.get("num_turns"),
                "agent_touched_tests": r["diff"]["agent_touched_tests"],
                "retrieval_backend": retrieval.get("backend"),
                "retrieval_fallback": retrieval.get("fallback"),
                "injected_fact_ids": ";".join(retrieval.get("fact_ids") or []),
                "context_chars": retrieval.get("chars", 0),
                "context_tokens": retrieval.get("estimated_tokens", 0),
                "retrieval_precision": retrieval.get("precision"),
                "retrieval_coverage": retrieval.get("coverage"),
                "irrelevant_facts": retrieval.get("irrelevant_count"),
                "retrieval_profile": (retrieval.get("selection") or {}).get("profile"),
                "retrieval_threshold": (retrieval.get("selection") or {}).get(
                    "relevance_threshold"),
                "selection_reasons": ";".join(
                    f"{item.get('fact_id')}:{item.get('reason')}"
                    for item in (retrieval.get("selection") or {}).get("selected", [])),
                "injected_origins": ";".join(
                    f"{item.get('fact_id')}:{item.get('origin')}"
                    for item in (retrieval.get("selection") or {}).get("selected", [])),
                "retrieval_leakage_free": (r.get("retrieval_leakage_check") or {}).get("ok"),
                "attribution_complete": (r.get("attribution") or {}).get("complete"),
                "attribution_traced_used_count": sum(
                    1 for item in (r.get("attribution") or {}).get("links", [])
                    if item.get("trace_complete")),
                "promoted_transfer_facts_count": sum(
                    1 for item in (r.get("memory_promotion") or {}).get("facts", [])
                    if item.get("confirmed_for_transfer")),
                "memory_instance_id": retrieval.get("instance_id"),
                "memory_parent_instance_id": retrieval.get("parent_instance_id"),
                "memory_session_instance_created": retrieval.get("session_instance_created"),
                "memory_clone_wall_sec": (retrieval.get("clone") or {}).get("wall_sec"),
                "memory_clone_provider_calls": retrieval.get("clone_provider_calls"),
                "memory_retention_deleted_instance_id": retention.get("deleted_instance_id"),
                "memory_retention_wall_sec": retention.get("wall_sec"),
                "memory_retention_provider_calls": retention.get("provider_calls"),
                "memory_state_before": write.get("state_version_before", retrieval.get("state_version")),
                "memory_state_after": write.get("state_version_after"),
                "memory_creates": mutations.get("create"), "memory_updates": mutations.get("update"),
                "memory_stale": mutations.get("stale"), "memory_noop": mutations.get("noop"),
                "memory_gotchas": write.get("gotchas"), "memory_schema_changes": write.get("schema_changes"),
                "memory_read_wall_sec": retrieval.get("wall_sec"),
                "memory_write_wall_sec": write.get("wall_sec"),
                "memory_read_provider_calls": retrieval.get("provider_calls"),
                "memory_write_provider_calls": write.get("provider_calls"),
                "evolution_checkpoint": (evolve or {}).get("checkpoint", {}).get("checkpoint"),
                "curator_wall_sec": (evolve or {}).get("wall_sec"),
                "curator_backend_wall_sec": (evolve or {}).get("backend_wall_sec"),
                "curator_output_tokens": evolve_usage.get("output_tokens"),
                "harmful_on_worse_off": (r["mode"] != "memory-off" and off_lift is not None
                                          and this_lift is not None and this_lift < off_lift),
                "harmful_stale_fact_used": any(
                    status == "stale" for status in (retrieval.get("fact_statuses") or {}).values()),
                "harmful_regression_after_retrieval": (r["mode"] != "memory-off"
                                                        and not r["regression"].get("green")),
            })
    print(f"\n[таблица] {csv_path}")

    # Невалидные и непарные строки остаются в CSV/diagnostics, но primary их не потребляет.
    print(f"\n{'задача':6} {'режим':18} {'успех':>8} {'eligible':>10} "
          f"{'median lift':>12} {'range':>15}")
    for task in sorted({r["task"] for r in rows}):
        for mode in modes:
            sel = [r for r in rows if r["task"] == task and r["mode"] == mode]
            if not sel:
                continue
            valid = [r for r in sel if r.get("valid_run")]
            success = sum(1 for r in valid if r.get("task_success"))
            eligible = [r for r in sel if r.get("analytical_eligible")
                        and pairs.get((r["task"], r["seed"]), {}).get("paired_eligible")]
            lifts = [r.get("grading", {}).get("feature_lift") for r in eligible]
            lifts = [x for x in lifts if x is not None]
            med = f"{median(lifts):.3f}" if lifts else "—"
            spread = f"{min(lifts):.3f}..{max(lifts):.3f}" if lifts else "—"
            print(f"{task:6} {mode:18} {success}/{len(valid):>6} {len(eligible)}/{len(sel):>8} "
                  f"{med:>12} {spread:>15}")

    print("\n[macro primary] только полностью paired eligible task/seed; median/range по seeds")
    for mode in modes:
        seed_macros = []
        for seed in sorted({r["seed"] for r in rows if r["mode"] == mode}):
            values = [r.get("grading", {}).get("feature_lift") for r in rows
                      if r["mode"] == mode and r["seed"] == seed and r.get("analytical_eligible")
                      and pairs.get((r["task"], r["seed"]), {}).get("paired_eligible")]
            values = [v for v in values if v is not None]
            if values:
                seed_macros.append(sum(values) / len(values))
        if seed_macros:
            print(f"  {mode:18} median={median(seed_macros):.3f} "
                  f"range={min(seed_macros):.3f}..{max(seed_macros):.3f}")
    print("\n[transfer clusters] macro eligible feature_lift")
    for cluster in sorted({r.get("transfer_cluster") for r in rows if r.get("transfer_cluster")}):
        for mode in modes:
            values = [r.get("grading", {}).get("feature_lift") for r in rows
                      if r.get("transfer_cluster") == cluster and r["mode"] == mode
                      and r.get("analytical_eligible")
                      and pairs.get((r["task"], r["seed"]), {}).get("paired_eligible")]
            values = [v for v in values if v is not None]
            if values:
                print(f"  {cluster:28} {mode:18} macro={sum(values)/len(values):.3f}")

    print("\n[raw diagnostics] technically valid rows, never a causal comparison")
    for mode in modes:
        values = [r.get("grading", {}).get("feature_lift") for r in rows
                  if r["mode"] == mode and r.get("valid_run")]
        values = [value for value in values if value is not None]
        if values:
            print(f"  {mode:18} n={len(values)} median={median(values):.3f} "
                  f"range={min(values):.3f}..{max(values):.3f}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks-file", default="dataset/tasks.yaml")
    ap.add_argument("--config", default="dataset/runner/config.toml")
    ap.add_argument("--tasks", nargs="*", help="подмножество id задач")
    ap.add_argument("--modes", nargs="*", choices=MODES, default=MODES,
                    help="режимы; evolving modes always run as chronological streams")
    ap.add_argument("--seeds", type=int, default=2)
    ap.add_argument("--out", default="runs")
    ap.add_argument("--selftest", action="store_true",
                    help="прогнать null и oracle: каркас должен показать красно и зелёно")
    ap.add_argument("--memory-selftest", action="store_true",
                    help="free fake-backend state-machine/isolation/durability tests")
    ap.add_argument("--test-protection-selftest", action="store_true",
                    help="free adversarial physical protection preflight")
    ap.add_argument("--skip-setup", action="store_true")
    ap.add_argument("--fail-fast", action="store_true",
                    help="stop the paid matrix after the first failed/invalid cell, after cleanup")
    ap.add_argument("--resume", action="store_true",
                    help="reuse memory-off cells or complete valid memory streams; partial memory streams are refused")
    ap.add_argument("--curator-after", default=None,
                    help="task boundary for the evolve child (default experiment.curator_after_task or a3)")
    ap.add_argument("--canary-gate", action="store_true",
                    help="enforce the strict precision canary gate and persist canary_gate.json")
    ap.add_argument("--protection-preflight-receipt", default=None,
                    help="successful adversarial preflight JSON (default OUT/_preflight/test_protection.json)")
    args = ap.parse_args()

    if args.memory_selftest:
        cmd = [sys.executable, "-m", "unittest", "discover", "-s",
               "dataset/runner/tests", "-v"]
        return subprocess.run(cmd, cwd=ROOT).returncode

    if args.test_protection_selftest:
        cfg = tomllib.loads((ROOT / args.config).read_text(encoding="utf-8"))
        _meta, loaded = load_tasks(ROOT / args.tasks_file)
        chosen = (args.tasks or list(loaded))[0]
        if chosen not in loaded:
            print(f"[tests] unknown preflight task {chosen}", file=sys.stderr)
            return 3
        clone = ensure_clone(cfg)
        root = Path(tempfile.mkdtemp(prefix="kata-test-protection-preflight-"))
        workspace = root / "workspace"
        artifact_dir = ROOT / args.out / "_preflight"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        try:
            make_workspace(clone, loaded[chosen].base_commit, workspace,
                           cfg["repo"].get("strip_files", []))
            report = adversarial_preflight(workspace)
            (artifact_dir / "test_protection.json").write_text(
                json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            print(f"[tests] adversarial protection preflight: {'ok' if report['ok'] else 'FAILED'}")
            return 0 if report["ok"] else 1
        except TestProtectionError as exc:
            print(f"[tests] adversarial protection preflight failed: {exc}", file=sys.stderr)
            return 1
        finally:
            drop_workspace(workspace)
            root.rmdir()

    ids = args.tasks or task_ids(ROOT / args.tasks_file)
    extra = ["--config", args.config]
    if args.skip_setup:
        extra.append("--skip-setup")

    if args.selftest:
        # Обязательный ритуал перед каждым новым набором задач.
        # null не делает ничего -> скрытые тесты обязаны упасть.
        # oracle кладёт исходники эталонного PR -> обязаны пройти.
        # Если это не так, задача не измеряет ничего, и никакая модель не поможет.
        bad = []
        for task in ids:
            for kind, must_be_green in (("null", False), ("oracle", True)):
                out = f"{args.out}/_selftest/{kind}"
                one(task, "memory-off", 0, extra + ["--agent", kind], out=out)
                m = ROOT / out / task / "memory-off" / "seed0" / "metrics.json"
                metrics = json.loads(m.read_text()) if m.exists() else {}
                green = metrics.get("hidden", {}).get("green")
                regression_green = metrics.get("regression", {}).get("green")
                valid = metrics.get("valid_run")
                ok = (green is must_be_green) and regression_green is True and valid is True
                print(f"[selftest] {task} {kind}: скрытые {'зелёные' if green else 'красные'} "
                      f"регрессия {'зелёная' if regression_green else 'красная'} "
                      f"-> {'ok' if ok else 'ПРОБЛЕМА'}")
                if not ok:
                    bad.append(f"{task}/{kind}")
        if bad:
            print(f"\n[selftest] задачи не измеряют ничего: {', '.join(bad)}")
            return 1
        print("\n[selftest] каркас меряет то, что должен")
        return 0

    modes_requested = args.modes
    cfg = tomllib.loads((ROOT / args.config).read_text(encoding="utf-8"))
    curator_after = args.curator_after or cfg.get("experiment", {}).get(
        "curator_after_task", "a3")
    cfg.setdefault("experiment", {})["curator_after_task"] = curator_after
    if "memory-on+evolve" in modes_requested and curator_after not in ids:
        print(f"[evolve] curator boundary {curator_after} is not in requested tasks {ids}")
        return 3
    if cfg.get("memory", {}).get("backend") == "xmemory":
        c0_instance = cfg["memory"].get("c0_instance_id")
        if not c0_instance:
            print("[memory] xmemory cloud lineage requires memory.c0_instance_id")
            return 3
    out_dir = ROOT / args.out
    dirty_entries = dirty_experiment_entries(out_dir)
    if dirty_entries and not args.resume:
        print(f"[matrix] {out_dir} is not fresh ({', '.join(dirty_entries[:8])}); "
              "use a fresh experiment directory or the declared --resume policy", file=sys.stderr)
        return 3
    canonical = task_ids(ROOT / args.tasks_file)
    if args.canary_gate and (ids != CANARY_TASKS or modes_requested != MODES
                             or args.seeds != CANARY_SEED):
        print("[gate] exact canary shape required: --tasks a1 a4 a6 "
              "--modes memory-off memory-on memory-on+evolve --seeds 1", file=sys.stderr)
        return 3
    protection_preflight = None
    paid_agent = cfg.get("agent", {}).get("kind") not in {"null", "oracle", "fake"}
    if paid_agent or args.canary_gate:
        receipt_path = (Path(args.protection_preflight_receipt)
                        if args.protection_preflight_receipt
                        else out_dir / "_preflight" / "test_protection.json")
        if not receipt_path.is_absolute():
            receipt_path = ROOT / receipt_path
        protection_preflight = load_protection_preflight(receipt_path)
        if protection_preflight is None:
            print(f"[matrix] missing or failed protection preflight receipt: {receipt_path}",
                  file=sys.stderr)
            return 3
    c0_before = None
    if args.canary_gate:
        try:
            c0_state = SubprocessXMemoryTransport(
                cfg["memory"].get("xmemcli", "xmemcli")).load_state(
                    cfg["memory"]["c0_instance_id"])
            c0_before = state_sha256(c0_state)
        except Exception as exc:
            print(f"[gate] cannot establish C0 digest before canary: {exc}", file=sys.stderr)
            return 3
    expected = {(task, mode, seed) for task in ids for mode in modes_requested
                for seed in range(1, args.seeds + 1)}
    existing_rows = collect(ROOT / args.out, expected) if args.resume else []
    existing = {(row["task"], row["mode"], row["seed"]): row for row in existing_rows}
    failed_commands = []
    ids = sorted(ids, key=canonical.index)
    abort_sweep = False
    for seed in range(1, args.seeds + 1):
        # Balance whole streams between repeats. Within a memory stream order is semantic.
        stream_modes = modes_requested if seed % 2 else list(reversed(modes_requested))
        for mode in stream_modes:
            stream_keys = [(task, mode, seed) for task in ids]
            state = f"{args.out}/_memory/{mode}/seed{seed}" if mode != "memory-off" else None
            if (args.resume and mode != "memory-off" and stream_keys
                    and all(existing.get(key, {}).get("valid_run") for key in stream_keys)):
                retained = completed_stream_retention(
                    [existing[key] for key in stream_keys], out_dir, cfg,
                    mode, seed, ids, curator_after)
                if not retained["ok"]:
                    print(f"[resume] refusing completed stream with invalid retention evidence: "
                          f"{mode} seed{seed}: {'; '.join(retained['issues'])}", file=sys.stderr)
                    return 3
                print(f"[resume] complete valid stream retained: {mode} seed{seed}")
                continue
            if (args.resume and mode != "memory-off"
                    and (any(key in existing for key in stream_keys)
                         or (ROOT / state / "lineage.json").exists())):
                print(f"[resume] refusing partial memory stream {mode} seed{seed}; "
                      "preserve its lineage/receipts and restart the whole experiment "
                      "in a fresh output directory", file=sys.stderr)
                return 3
            evolved = False
            stream_failed = False
            for index, task in enumerate(ids):
                key = (task, mode, seed)
                if (args.resume and mode == "memory-off"
                        and existing.get(key, {}).get("valid_run")):
                    print(f"[resume] valid cell retained: {task} · {mode} · seed{seed}")
                    continue
                reset = bool(state and index == 0)
                run_rc = one(task, mode, seed, extra, out=args.out,
                             memory_state=state, reset_memory=reset)
                if run_rc != 0:
                    failed_commands.append((task, mode, seed))
                    stream_failed = True
                    if should_abort_stream(mode, run_rc, args.fail_fast):
                        break
                if mode == "memory-on+evolve" and task == curator_after:
                    evolve_out = f"{args.out}/_evolution/{mode}/seed{seed}"
                    evolve_cmd = EVOLVE + ["--config", args.config, "--mode", mode,
                                           "--seed", str(seed), "--memory-state", state,
                                           "--out", evolve_out, "--after-task", curator_after]
                    print(f"\n=== evolve checkpoint · {mode} · seed{seed} " + "=" * 20)
                    if subprocess.run(evolve_cmd, cwd=ROOT).returncode != 0:
                        failed_commands.append(("evolve", mode, seed))
                        stream_failed = True
                        break  # a6 must never consume a failed/nonexistent curator checkpoint
                    evolved = True
            if mode == "memory-on+evolve" and curator_after in ids and not evolved and not stream_failed:
                failed_commands.append(("evolve-missing", mode, seed))
            if state and not cleanup_tail(state, cfg, args.out):
                failed_commands.append(("cleanup-tail", mode, seed))
                stream_failed = True
            if stream_failed and args.fail_fast:
                abort_sweep = True
                break
        if abort_sweep:
            break

    rows = collect(ROOT / args.out, expected)
    write_table(rows, ROOT / args.out, modes_requested)
    seen = {(r["task"], r["mode"], r["seed"]) for r in rows}
    missing = sorted(expected - seen)
    invalid = sorted((r["task"], r["mode"], r["seed"])
                     for r in rows if not r.get("valid_run"))
    if args.canary_gate:
        try:
            c0_after = state_sha256(SubprocessXMemoryTransport(
                cfg["memory"].get("xmemcli", "xmemcli")).load_state(
                    cfg["memory"]["c0_instance_id"]))
        except Exception as exc:
            print(f"[gate] cannot establish C0 digest after canary: {exc}", file=sys.stderr)
            c0_after = None
        gate = canary_gate(rows, ROOT / args.out, ids, modes_requested, cfg,
                           c0_before, c0_after, protection_preflight)
        (ROOT / args.out / "canary_gate.json").write_text(
            json.dumps(gate, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"\n[canary gate] {'PASS' if gate['passed'] else 'FAIL'}: {gate['checks']}")
        if not gate["passed"]:
            return 5
    if failed_commands or missing or invalid:
        print(f"\n[матрица] НЕПОЛНА: commands={failed_commands}, missing={missing}, invalid={invalid}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
