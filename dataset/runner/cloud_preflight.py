#!/usr/bin/env python3
"""Real xmemory read→write→clone→read durability preflight with scoped cleanup."""

from __future__ import annotations

import argparse
import json
import tomllib
from pathlib import Path

from memory_backend import (SubprocessXMemoryTransport, cleanup_xmemory_tail,
                            open_backend, state_sha256)


ROOT = Path(__file__).resolve().parents[2]
SENTINEL = "fact:do-9901"


def durability_batch(used_facts: list[str]) -> dict:
    """Build the same trace-complete write batch used by the real cloud preflight."""
    return {
        "mutations": [{"op": "create", "fact": {
            "fact_id": SENTINEL, "slice": "data-ownership",
            "statement": "Validated durability sentinel stays in the same transfer cluster",
            "content": "Validated durability sentinel stays in the same transfer cluster.",
            "evidence": ["dataset/runner/cloud_preflight.py:1"],
            "status": "active", "confidence": "high", "provenance": "observed",
            "source": "task-validated:a1:data-repository-invariants",
        }}],
        "task": {"task_id": "a1", "title": "cloud durability preflight",
                 "used_facts": used_facts, "produced_facts": [SENTINEL],
                 "decisions": [{
                     "fact_id": fact_id,
                     "decision": "Seed a same-cluster fact to verify cloud durability",
                     "diff_paths": ["dataset/runner/cloud_preflight.py"],
                 } for fact_id in used_facts]},
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="dataset/runner/config.toml")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    cfg = tomllib.loads((ROOT / args.config).read_text(encoding="utf-8"))
    if cfg.get("memory", {}).get("backend") != "xmemory":
        raise SystemExit("cloud preflight requires memory.backend=xmemory")
    out = (ROOT / args.out).resolve()
    if out.exists():
        raise SystemExit(f"cloud preflight output already exists: {out}")
    out.mkdir(parents=True)
    state_dir = out / "_memory" / "precision-durability" / "seed0"
    snapshot = (ROOT / cfg["memory"]["snapshot"]).resolve()
    transport = SubprocessXMemoryTransport(cfg["memory"].get("xmemcli", "xmemcli"))
    c0_id = cfg["memory"]["c0_instance_id"]
    c0_before = state_sha256(transport.load_state(c0_id))
    cleanup = None
    report = {"ok": False, "c0_digest_before": c0_before, "sentinel": SENTINEL}
    try:
        first = open_backend(cfg, state_dir, snapshot, "memory-on", 0,
                             session_id="durability-write")
        first.prepare(reset=True)
        read1 = first.read(
            "a1", ["data-ownership"], "normalization storage migration", 5,
            expected_fact_ids=["fact:do-0005", "fact:do-0006"],
            transfer_cluster="data-repository-invariants",
            relevance_threshold=float(cfg["memory"].get("relevance_threshold", 0.75)),
            learned_top_k=int(cfg["memory"].get("learned_top_k", 1)))
        used_facts = read1.metrics["fact_ids"][:1]
        batch = durability_batch(used_facts)
        write = first.apply(batch, read1.metrics["fact_ids"], "a1")
        second = open_backend(cfg, state_dir, snapshot, "memory-on", 0,
                              session_id="durability-read")
        second.prepare(reset=False)
        read2 = second.read(
            "a6", ["data-ownership"], "repository ownership merge", 5,
            expected_fact_ids=["fact:do-0001", "fact:do-0002", "fact:do-0003"],
            transfer_cluster="data-repository-invariants",
            relevance_threshold=float(cfg["memory"].get("relevance_threshold", 0.75)),
            learned_top_k=int(cfg["memory"].get("learned_top_k", 1)))
        if SENTINEL not in read2.metrics["fact_ids"]:
            raise RuntimeError("validated write did not survive clone/read boundary")
        report.update({
            "write_child": first.instance_id, "read_child": second.instance_id,
            "write_state_after": write.get("state_version_after"),
            "read_state_version": read2.metrics.get("state_version"),
            "first_read_fact_ids": read1.metrics.get("fact_ids"),
            "second_read_fact_ids": read2.metrics.get("fact_ids"),
            "clone_verified": second.parent_instance_id == first.instance_id,
            "durable_sentinel_read": True,
        })
    finally:
        if (state_dir / "lineage.json").exists():
            cleanup = cleanup_xmemory_tail(
                state_dir, cfg["memory"].get("xmemcli", "xmemcli"))
            (state_dir / "cleanup.json").write_text(
                json.dumps(cleanup, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        c0_after = state_sha256(transport.load_state(c0_id))
        report["c0_digest_after"] = c0_after
        report["c0_unchanged"] = c0_before == c0_after
        report["tail_cleanup"] = cleanup
        lineage = (json.loads((state_dir / "lineage.json").read_text())
                   if (state_dir / "lineage.json").exists() else {"sessions": []})
        report["children"] = len(lineage.get("sessions", []))
        report["delete_receipts"] = sum(
            bool(item.get("deleted_at")
                 and (item.get("delete") or {}).get("ok") is True
                 and (item.get("delete") or {}).get("instance_id") == item.get("instance_id"))
            for item in lineage.get("sessions", []))
        report["ok"] = bool(report.get("durable_sentinel_read")
                            and report.get("clone_verified") and report["c0_unchanged"]
                            and report["children"] == 2 and report["delete_receipts"] == 2)
        (out / "cloud_preflight.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"ok": report["ok"], "children": report["children"],
                      "delete_receipts": report["delete_receipts"],
                      "c0_unchanged": report["c0_unchanged"]}))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
