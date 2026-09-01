#!/usr/bin/env python3
"""Run the isolated curator checkpoint after a3 and before a4."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path

from memory_backend import MemoryError, open_backend


ROOT = Path(__file__).resolve().parents[2]


def prompt() -> str:
    return """You are the memory curator, not a coding agent. This is the checkpoint after a3
and before a4. You cannot see a repository, future tasks, solution commits, or hidden tests.
Review only memory_state.json: candidates, gotchas, questions, contradictions and duplicate facts.
Code evidence recorded in facts wins over memory prose. You may update or stale existing facts;
you must not create coding-solution facts. Propose only additive, domain-typed schema changes.
Write evolution_report.json with this exact shape:
{"mutations":[{"op":"update|stale|noop","fact_id":"fact:...","values":{...}}],
 "schema_changes":[{"kind":"additive","description":"..."}],
 "proposal_version":"optional xmemory proposal version",
 "schema_suggestion_decisions":[{"fingerprint":"...","decision":"accept|reject|defer"}],
 "apply_schema_suggestions":false,
 "confirm_destructive_preview_reviewed":false,
 "report":"short curator report"}.
An empty list is valid. Do not edit any other file and do not solve a coding task."""


def parse_usage(output: str) -> tuple[dict, bool]:
    try:
        payload = json.loads(output)
        usage = payload.get("usage", {})
        return ({
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "cache_read_tokens": usage.get("cache_read_input_tokens"),
            "cache_creation_tokens": usage.get("cache_creation_input_tokens"),
            "total_cost_usd": payload.get("total_cost_usd"),
            "num_turns": payload.get("num_turns"),
        }, True)
    except Exception:
        return ({}, False)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="dataset/runner/config.toml")
    ap.add_argument("--mode", default="memory-on+evolve")
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--memory-state", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--agent", choices=["claude", "fake"], default=None)
    args = ap.parse_args()

    cfg = tomllib.loads((ROOT / args.config).read_text(encoding="utf-8"))
    snapshot = (ROOT / cfg["memory"]["snapshot"]).resolve()
    backend = open_backend(cfg, (ROOT / args.memory_state).resolve(), snapshot, args.mode, args.seed)
    backend.prepare(reset=False)
    state = backend._load()  # audit shadow is the curator input for both adapters
    schema_review = None
    if hasattr(backend, "review_schema_suggestions"):
        schema_review = backend.review_schema_suggestions()
        state["xmemory_schema_suggestions"] = schema_review
    out = (ROOT / args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    before = state["state_version"]
    kind = args.agent or ("fake" if cfg["agent"].get("kind") in {"null", "oracle"}
                          else cfg["agent"].get("kind", "claude"))
    usage, usage_parsed, rc, wall = {}, True, 0, 0.0

    if kind == "fake":
        report = {"mutations": [], "schema_changes": [],
                  "report": "fake curator checkpoint: no pending changes"}
    else:
        wt = Path(tempfile.mkdtemp(prefix="kata-evolution-"))
        try:
            (wt / "memory_state.json").write_text(
                json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
            settings = {"sandbox": {"enabled": True, "failIfUnavailable": True,
                                     "allowUnsandboxedCommands": False,
                                     "filesystem": {"denyRead": ["~/"], "allowRead": ["."]}}}
            (wt / ".claude").mkdir()
            (wt / ".claude" / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
            model = cfg["agent"]["model"]
            effort = cfg["agent"]["effort"]
            cmd = [part.replace("{prompt}", prompt()).replace("{model}", model)
                   .replace("{effort}", effort) for part in cfg["agent"]["cmd"]]
            started = time.monotonic()
            result = subprocess.run(cmd, cwd=wt, text=True, capture_output=True,
                                    timeout=cfg["agent"].get("timeout_sec", 3600),
                                    env={**os.environ, **cfg["repo"].get("env", {})})
            wall = time.monotonic() - started
            rc = result.returncode
            (out / "curator_stdout.log").write_text(result.stdout, encoding="utf-8")
            (out / "curator_stderr.log").write_text(result.stderr, encoding="utf-8")
            usage, usage_parsed = parse_usage(result.stdout)
            report_path = wt / "evolution_report.json"
            if rc or not report_path.exists():
                raise MemoryError(f"curator failed rc={rc} or did not write evolution_report.json")
            report = json.loads(report_path.read_text(encoding="utf-8"))
        finally:
            shutil.rmtree(wt, ignore_errors=True)

    try:
        backend_started = time.monotonic()
        checkpoint = backend.evolve(report)
        backend_wall = time.monotonic() - backend_started
    except (MemoryError, json.JSONDecodeError) as exc:
        print(f"evolution rejected: {exc}", file=sys.stderr)
        return 2
    artifact = {
        "mode": args.mode, "seed": args.seed, "agent": kind,
        "rc": rc, "usage": usage, "usage_parsed": usage_parsed,
        "wall_sec": round(wall, 3), "state_version_before": before,
        "backend_wall_sec": round(backend_wall, 3),
        "schema_review": schema_review,
        "state_version_after": checkpoint["state_version_after"],
        "checkpoint": checkpoint, "report": report,
    }
    (out / "evolution.json").write_text(
        json.dumps(artifact, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[evolve] state {before} -> {checkpoint['state_version_after']}; {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
