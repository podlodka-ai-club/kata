#!/usr/bin/env python3
"""
Прогон матрицы: задачи × режимы × повторы. Собирает результаты в таблицу.

    # самопроверка каркаса — обязательна перед первым настоящим прогоном
    python dataset/runner/sweep.py --selftest

    # full three-mode matrix (paid; only after canary)
    python dataset/runner/sweep.py --seeds 3

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
import tomllib
from statistics import median
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("нужен pyyaml: pip install pyyaml")

ROOT = Path(__file__).resolve().parents[2]
RUN = [sys.executable, str(Path(__file__).with_name("run.py"))]
EVOLVE = [sys.executable, str(Path(__file__).with_name("evolve.py"))]
MODES = ["memory-off", "memory-on", "memory-on+evolve"]


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


def write_table(rows: list[dict], out_dir: Path) -> None:
    cols = ["task", "transfer_cluster", "mode", "seed", "agent", "agent_model", "agent_effort", "valid_run",
            "invalid_reasons", "analytical_eligible", "analytical_ineligible_reasons",
            "task_success", "score", "hidden_micro_score", "score_binary", "feature_lift", "feature_passed",
            "feature_total", "agent_rc", "hidden_passed", "hidden_total", "hidden_failed",
            "regression_green", "tests_added", "existing_tests_modified", "existing_tests_deleted",
            "architecture_score", "architecture_green", "files_changed", "insertions", "deletions",
            "files_read", "time_to_first_relevant_file_sec", "wall_sec", "input_tokens", "output_tokens", "cache_read_tokens",
            "cache_creation_tokens", "total_cost_usd", "num_turns", "agent_touched_tests",
            "retrieval_backend", "retrieval_fallback", "injected_fact_ids", "context_chars",
            "context_tokens", "retrieval_precision", "retrieval_coverage", "irrelevant_facts",
            "memory_state_before", "memory_state_after", "memory_creates", "memory_updates",
            "memory_stale", "memory_noop", "memory_gotchas", "memory_schema_changes",
            "memory_read_wall_sec", "memory_write_wall_sec", "memory_read_provider_calls",
            "memory_write_provider_calls", "evolution_checkpoint",
            "evolve_wall_sec", "evolve_backend_wall_sec", "evolve_output_tokens", "harmful_on_worse_off",
            "harmful_stale_fact_used", "harmful_regression_after_retrieval"]
    csv_path = out_dir / "results.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        paired_off = {(r["task"], r["seed"]): r for r in rows if r["mode"] == "memory-off"}
        for r in rows:
            u = r.get("usage") or {}
            grading = r.get("grading") or {}
            retrieval = r.get("retrieval") or {}
            write = r.get("memory_write") or {}
            mutations = write.get("mutations") or {}
            tests = r.get("diff", {}).get("test_changes", {})
            evolve = None
            if r["mode"] == "memory-on+evolve":
                p = out_dir / "_evolution" / r["mode"] / f"seed{r['seed']}" / "evolution.json"
                if p.exists() and r["task"] in {"a4", "a5", "a6"}:
                    evolve = json.loads(p.read_text(encoding="utf-8"))
            evolve_usage = (evolve or {}).get("usage") or {}
            off = paired_off.get((r["task"], r["seed"]))
            off_lift = (off or {}).get("grading", {}).get("feature_lift")
            this_lift = grading.get("feature_lift")
            w.writerow({
                "task": r["task"], "transfer_cluster": r.get("transfer_cluster"),
                "mode": r["mode"], "seed": r["seed"], "agent": r["agent"],
                "agent_model": r.get("agent_model"),
                "agent_effort": r.get("agent_effort"),
                "valid_run": r.get("valid_run"),
                "invalid_reasons": ";".join(r.get("invalid_reasons") or []),
                "analytical_eligible": r.get("analytical_eligible"),
                "analytical_ineligible_reasons": ";".join(r.get("analytical_ineligible_reasons") or []),
                "task_success": r.get("task_success"),
                "score": r["score"],
                "hidden_micro_score": r.get("hidden_micro_score", r.get("score")),
                "score_binary": r.get("score_binary"),
                "feature_lift": grading.get("feature_lift"),
                "feature_passed": grading.get("feature_passed"),
                "feature_total": grading.get("feature_total"),
                "agent_rc": r.get("agent_rc"),
                "hidden_passed": r["hidden"].get("passed"),
                "hidden_total": r["hidden"].get("tests", 0) - r["hidden"].get("skipped", 0),
                "hidden_failed": r["hidden"].get("failed", 0) + r["hidden"].get("errors", 0),
                "regression_green": r["regression"].get("green"),
                "tests_added": len(tests.get("added", [])),
                "existing_tests_modified": len(tests.get("modified_existing", [])),
                "existing_tests_deleted": len(tests.get("deleted_existing", [])),
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
                "evolve_wall_sec": (evolve or {}).get("wall_sec"),
                "evolve_backend_wall_sec": (evolve or {}).get("backend_wall_sec"),
                "evolve_output_tokens": evolve_usage.get("output_tokens"),
                "harmful_on_worse_off": (r["mode"] != "memory-off" and off_lift is not None
                                          and this_lift is not None and this_lift < off_lift),
                "harmful_stale_fact_used": any(
                    status == "stale" for status in (retrieval.get("fact_statuses") or {}).values()),
                "harmful_regression_after_retrieval": (r["mode"] != "memory-off"
                                                        and not r["regression"].get("green")),
            })
    print(f"\n[таблица] {csv_path}")

    # Невалидные строки остаются в CSV для разбора, но в сравнительную сводку не входят.
    print(f"\n{'задача':6} {'режим':18} {'успех':>8} {'eligible':>10} "
          f"{'median lift':>12} {'range':>15}")
    for task in sorted({r["task"] for r in rows}):
        for mode in MODES:
            sel = [r for r in rows if r["task"] == task and r["mode"] == mode]
            if not sel:
                continue
            valid = [r for r in sel if r.get("valid_run")]
            success = sum(1 for r in valid if r.get("task_success"))
            eligible = [r for r in sel if r.get("analytical_eligible")]
            lifts = [r.get("grading", {}).get("feature_lift") for r in eligible]
            lifts = [x for x in lifts if x is not None]
            med = f"{median(lifts):.3f}" if lifts else "—"
            spread = f"{min(lifts):.3f}..{max(lifts):.3f}" if lifts else "—"
            print(f"{task:6} {mode:18} {success}/{len(valid):>6} {len(eligible)}/{len(sel):>8} "
                  f"{med:>12} {spread:>15}")

    print("\n[macro] macro-average по задачам (только analytical_eligible; затем median/range по seeds)")
    for mode in MODES:
        seed_macros = []
        for seed in sorted({r["seed"] for r in rows if r["mode"] == mode}):
            values = [r.get("grading", {}).get("feature_lift") for r in rows
                      if r["mode"] == mode and r["seed"] == seed and r.get("analytical_eligible")]
            values = [v for v in values if v is not None]
            if values:
                seed_macros.append(sum(values) / len(values))
        if seed_macros:
            print(f"  {mode:18} median={median(seed_macros):.3f} "
                  f"range={min(seed_macros):.3f}..{max(seed_macros):.3f}")
    print("\n[transfer clusters] macro eligible feature_lift")
    for cluster in sorted({r.get("transfer_cluster") for r in rows if r.get("transfer_cluster")}):
        for mode in MODES:
            values = [r.get("grading", {}).get("feature_lift") for r in rows
                      if r.get("transfer_cluster") == cluster and r["mode"] == mode
                      and r.get("analytical_eligible")]
            values = [v for v in values if v is not None]
            if values:
                print(f"  {cluster:28} {mode:18} macro={sum(values)/len(values):.3f}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks-file", default="dataset/tasks.yaml")
    ap.add_argument("--config", default="dataset/runner/config.toml")
    ap.add_argument("--tasks", nargs="*", help="подмножество id задач")
    ap.add_argument("--modes", nargs="*", choices=MODES, default=MODES,
                    help="режимы; evolving modes always run as chronological streams")
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--out", default="runs")
    ap.add_argument("--selftest", action="store_true",
                    help="прогнать null и oracle: каркас должен показать красно и зелёно")
    ap.add_argument("--memory-selftest", action="store_true",
                    help="free fake-backend state-machine/isolation/durability tests")
    ap.add_argument("--skip-setup", action="store_true")
    args = ap.parse_args()

    if args.memory_selftest:
        cmd = [sys.executable, "-m", "unittest", "discover", "-s",
               "dataset/runner/tests", "-v"]
        return subprocess.run(cmd, cwd=ROOT).returncode

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
    if cfg.get("memory", {}).get("backend") == "xmemory":
        mapping = cfg["memory"].get("xmemory_instances", {})
        keys = [f"{mode}.seed{seed}" for mode in modes_requested if mode != "memory-off"
                for seed in range(1, args.seeds + 1)]
        missing_instances = [key for key in keys if not mapping.get(key)]
        ids_in_use = [mapping[key] for key in keys if mapping.get(key)]
        if missing_instances or len(ids_in_use) != len(set(ids_in_use)):
            print(f"[memory] xmemory isolation failed: missing={missing_instances}, "
                  f"duplicate_instance_ids={len(ids_in_use) != len(set(ids_in_use))}")
            return 3
    expected = {(task, mode, seed) for task in ids for mode in modes_requested
                for seed in range(1, args.seeds + 1)}
    failed_commands = []
    canonical = task_ids(ROOT / args.tasks_file)
    ids = sorted(ids, key=canonical.index)
    for seed in range(1, args.seeds + 1):
        # Balance whole streams between repeats. Within a memory stream order is semantic.
        stream_modes = modes_requested if seed % 2 else list(reversed(modes_requested))
        for mode in stream_modes:
            state = f"{args.out}/_memory/{mode}/seed{seed}" if mode != "memory-off" else None
            evolved = False
            for index, task in enumerate(ids):
                reset = bool(state and index == 0)
                if one(task, mode, seed, extra, out=args.out,
                       memory_state=state, reset_memory=reset) != 0:
                    failed_commands.append((task, mode, seed))
                if mode == "memory-on+evolve" and task == "a3":
                    evolve_out = f"{args.out}/_evolution/{mode}/seed{seed}"
                    evolve_cmd = EVOLVE + ["--config", args.config, "--mode", mode,
                                           "--seed", str(seed), "--memory-state", state,
                                           "--out", evolve_out]
                    print(f"\n=== evolve checkpoint · {mode} · seed{seed} " + "=" * 20)
                    if subprocess.run(evolve_cmd, cwd=ROOT).returncode != 0:
                        failed_commands.append(("evolve", mode, seed))
                    evolved = True
            if mode == "memory-on+evolve" and "a3" in ids and not evolved:
                failed_commands.append(("evolve-missing", mode, seed))

    rows = collect(ROOT / args.out, expected)
    write_table(rows, ROOT / args.out)
    seen = {(r["task"], r["mode"], r["seed"]) for r in rows}
    missing = sorted(expected - seen)
    invalid = sorted((r["task"], r["mode"], r["seed"])
                     for r in rows if not r.get("valid_run"))
    if failed_commands or missing or invalid:
        print(f"\n[матрица] НЕПОЛНА: commands={failed_commands}, missing={missing}, invalid={invalid}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
