#!/usr/bin/env python3
"""Durable, isolated memory backends for the chronological kata evaluation.

The file backend is the reproducible fallback and the fake used by selftests.  It
keeps the same lifecycle, provenance and Task relations as xmemory.  The xmemory
backend keeps a local audit shadow, but reads and structured writes go through a
dedicated xmemory instance supplied for exactly one mode/seed stream.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SLICE_OBJECTS = {
    "api-contracts": "ApiContract",
    "invariants": "Invariant",
    "data-ownership": "DataOwnership",
    "config-flags": "ConfigFlag",
    "gotchas": "Gotcha",
}
PREFIX_SLICES = {"ac": "api-contracts", "iv": "invariants", "do": "data-ownership",
                 "cf": "config-flags", "gt": "gotchas"}
FACT_ID_RE = re.compile(r"fact:[a-z]{2}-\d{4}")


class MemoryError(RuntimeError):
    pass


@dataclass
class ReadResult:
    facts: list[dict[str, Any]]
    exact_text: str
    metrics: dict[str, Any]


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tokens(chars: int) -> int:
    return math.ceil(chars / 4) if chars else 0


def parse_snapshot(path: Path) -> list[dict[str, Any]]:
    """Turn the frozen Markdown view into stable, typed active records."""
    text = path.read_text(encoding="utf-8")
    matches = list(re.finditer(r"^### (fact:[a-z]{2}-\d{4}) — (.+)$", text, re.M))
    facts = []
    for i, match in enumerate(matches):
        fact_id, title = match.groups()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        content = text[match.start():end].strip()
        prefix = fact_id.split(":", 1)[1].split("-", 1)[0]
        slice_name = PREFIX_SLICES.get(prefix)
        if not slice_name:
            raise MemoryError(f"unknown C0 fact prefix: {fact_id}")
        facts.append({
            "fact_id": fact_id,
            "object_type": SLICE_OBJECTS[slice_name],
            "slice": slice_name,
            "title": title,
            "statement": title,
            "content": content,
            "evidence": re.findall(r"`([^`]+:\d[^`]*)`", content),
            "confidence": "high",
            "status": "active",
            "provenance": "observed",
            "auto_approved": True,
            "human_notes": [],
            "question": None,
            "source": "extraction",
            "superseded_by": None,
            "status_reason": None,
            "created_at": "2026-09-01T00:00:00+04:00",
        })
    if not facts:
        raise MemoryError(f"no facts parsed from {path}")
    return facts


def new_state(snapshot: Path, mode: str, seed: int, backend: str) -> dict[str, Any]:
    raw = snapshot.read_bytes()
    return {
        "format_version": 1,
        "backend": backend,
        "fallback": backend == "file",
        "mode": mode,
        "seed": seed,
        "c0_sha256": hashlib.sha256(raw).hexdigest(),
        "state_version": 0,
        "schema_version": 1,
        "schema": {
            "ApiContract": ["fact_id", "router_endpoint", "method", "path",
                            "authentication_boundary", "response_behavior"],
            "Invariant": ["fact_id", "rule", "protected_entity", "enforcement_mechanism",
                          "failure_behavior", "scope"],
            "DataOwnership": ["fact_id", "model_table", "owning_repository", "group_scope",
                              "read_write_behavior", "migration_requirement"],
            "ConfigFlag": ["fact_id", "setting_name", "flag_type", "flag_default",
                           "read_location", "validation_readiness", "documentation_evidence"],
            "Gotcha": ["fact_id", "trigger", "outcome", "lesson"],
            "Task": ["task_id", "title", "at", "used_facts", "produced_facts", "decisions"],
        },
        "schema_history": [],
        "facts": {f["fact_id"]: f for f in parse_snapshot(snapshot)},
        "tasks": {},
        "journal": [],
        "evolution_checkpoints": [],
        "created_at": utcnow(),
    }


def render_facts(facts: list[dict[str, Any]], backend: str, state_version: int) -> str:
    payload = {
        "backend": backend,
        "state_version": state_version,
        "facts": [{
            "fact_id": f["fact_id"],
            "slice": f["slice"],
            "status": f["status"],
            "content": f.get("content") or f.get("statement", ""),
            "human_notes": f.get("human_notes", []),
        } for f in facts],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def validate_batch(batch: dict[str, Any], injected_ids: list[str], task_id: str) -> dict[str, Any]:
    if not isinstance(batch, dict):
        raise MemoryError("mutation batch must be a JSON object")
    mutations = batch.get("mutations", [])
    if not isinstance(mutations, list):
        raise MemoryError("mutations must be a list")
    normalized = []
    for item in mutations:
        if not isinstance(item, dict) or item.get("op") not in {"create", "update", "stale", "noop"}:
            raise MemoryError("each mutation needs op=create|update|stale|noop")
        fact_id = item.get("fact_id") or (item.get("fact") or {}).get("fact_id")
        if not fact_id or not FACT_ID_RE.fullmatch(fact_id):
            raise MemoryError(f"mutation has invalid fact_id: {fact_id!r}")
        normalized.append(item)
    task = batch.get("task") or {}
    if task.get("task_id") != task_id:
        raise MemoryError(f"Task.task_id must be {task_id}")
    used = task.get("used_facts", [])
    produced = task.get("produced_facts", [])
    if not isinstance(used, list) or not isinstance(produced, list):
        raise MemoryError("Task used_facts/produced_facts must be lists")
    unknown_used = sorted(set(used) - set(injected_ids))
    if unknown_used:
        raise MemoryError(f"Task references facts not injected: {unknown_used}")
    changed_ids = {i.get("fact_id") or (i.get("fact") or {}).get("fact_id")
                   for i in normalized if i.get("op") != "noop"}
    if not set(produced).issubset(changed_ids):
        raise MemoryError("produced_facts must refer to create/update/stale mutations")
    decisions = task.get("decisions", [])
    if (not isinstance(decisions, list)
            or any(not isinstance(d, dict) or d.get("fact_id") not in injected_ids
                   for d in decisions)):
        raise MemoryError("Task decisions must be a list referencing only injected fact_ids")
    return {"mutations": normalized, "task": task}


class FileMemoryBackend:
    """JSON-backed semantic fallback with append-only journal and atomic commits."""

    name = "file"

    def __init__(self, state_dir: Path, snapshot: Path, mode: str, seed: int):
        self.state_dir = state_dir
        self.state_file = state_dir / "state.json"
        self.snapshot = snapshot
        self.mode = mode
        self.seed = seed

    def prepare(self, reset: bool = False) -> dict[str, Any]:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        if reset or not self.state_file.exists():
            self._save(new_state(self.snapshot, self.mode, self.seed, self.name))
        state = self._load()
        if state["mode"] != self.mode or state["seed"] != self.seed:
            raise MemoryError("memory state belongs to another mode/seed")
        expected = hashlib.sha256(self.snapshot.read_bytes()).hexdigest()
        if state["c0_sha256"] != expected:
            raise MemoryError("memory state was not cloned from configured C0")
        return state

    def _load(self) -> dict[str, Any]:
        if not self.state_file.exists():
            raise MemoryError(f"memory state missing: {self.state_file}")
        return json.loads(self.state_file.read_text(encoding="utf-8"))

    def _save(self, state: dict[str, Any]) -> None:
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, self.state_file)

    def read(self, task_id: str, slices: list[str], query: str, top_k: int = 20) -> ReadResult:
        started = time.monotonic()
        state = self._load()  # deliberately reopen: this is the restart/durability boundary
        candidates = [f for f in state["facts"].values()
                      if f.get("status") == "active" and f.get("slice") in slices]
        words = {w for w in re.findall(r"[a-zA-Z_]{4,}", query.lower())}
        candidates.sort(key=lambda f: (
            -sum(w in (f.get("content") or f.get("statement", "")).lower() for w in words),
            f["fact_id"],
        ))
        facts = candidates[:top_k]
        exact = render_facts(facts, self.name, state["state_version"])
        chars = len(exact)
        return ReadResult(facts, exact, {
            "backend": self.name,
            "fallback": True,
            "state_version": state["state_version"],
            "fact_ids": [f["fact_id"] for f in facts],
            "fact_statuses": {f["fact_id"]: f.get("status") for f in facts},
            "facts_count": len(facts),
            "chars": chars,
            "estimated_tokens": _tokens(chars),
            "wall_sec": round(time.monotonic() - started, 4),
            "provider_calls": 0, "provider_usage": None,
        })

    def apply(self, batch: dict[str, Any], injected_ids: list[str], task_id: str) -> dict[str, Any]:
        started = time.monotonic()
        batch = validate_batch(batch, injected_ids, task_id)
        state = self._load()
        before = state["state_version"]
        counts = {"create": 0, "update": 0, "stale": 0, "noop": 0}
        gotchas = 0
        produced = []
        for item in batch["mutations"]:
            op = item["op"]
            fact_id = item.get("fact_id") or (item.get("fact") or {})["fact_id"]
            counts[op] += 1
            if op == "noop":
                continue
            if op == "create":
                if fact_id in state["facts"]:
                    raise MemoryError(f"create collides with existing {fact_id}")
                fact = dict(item.get("fact") or {})
                fact.setdefault("fact_id", fact_id)
                prefix = fact_id.split(":")[1].split("-")[0]
                if prefix not in PREFIX_SLICES:
                    raise MemoryError(f"unknown fact prefix: {fact_id}")
                fact.setdefault("slice", PREFIX_SLICES[prefix])
                fact.setdefault("object_type", SLICE_OBJECTS[fact["slice"]])
                fact.setdefault("status", "candidate")
                fact.setdefault("confidence", "low")
                fact.setdefault("provenance", "inferred")
                fact.setdefault("source", "task")
                fact.setdefault("evidence", [])
                fact.setdefault("created_at", utcnow())
                if not fact.get("evidence"):
                    raise MemoryError(f"new fact has no evidence: {fact_id}")
                fact.setdefault("content", fact.get("statement", ""))
                state["facts"][fact_id] = fact
                gotchas += fact.get("slice") == "gotchas"
            else:
                if fact_id not in state["facts"]:
                    raise MemoryError(f"{op} targets missing {fact_id}")
                values = dict(item.get("values") or {})
                if op == "stale":
                    values["status"] = "stale"
                    if not values.get("status_reason"):
                        raise MemoryError(f"stale needs status_reason: {fact_id}")
                state["facts"][fact_id].update(values)
            produced.append(fact_id)
        state["tasks"][task_id] = {
            **batch["task"], "at": utcnow(), "used_facts": batch["task"].get("used_facts", []),
            "produced_facts": batch["task"].get("produced_facts", []),
        }
        state["state_version"] += 1
        state["journal"].append({
            "kind": "task_write", "task_id": task_id, "at": utcnow(),
            "version_before": before, "version_after": state["state_version"],
            "counts": counts, "used_facts": state["tasks"][task_id]["used_facts"],
            "produced_facts": state["tasks"][task_id]["produced_facts"],
        })
        self._save(state)
        return {
            "backend": self.name, "fallback": True, "ok": True,
            "state_version_before": before, "state_version_after": state["state_version"],
            "mutations": counts, "gotchas": gotchas, "schema_changes": 0,
            "used_facts": state["tasks"][task_id]["used_facts"],
            "produced_facts": state["tasks"][task_id]["produced_facts"],
            "wall_sec": round(time.monotonic() - started, 4), "provider_usage": None,
            "provider_calls": 0,
        }

    def evolve(self, report: dict[str, Any]) -> dict[str, Any]:
        state = self._load()
        before = state["state_version"]
        schema_before = state["schema_version"]
        mutations = report.get("mutations", [])
        # Evolution may only curate existing records; future coding facts cannot be created here.
        for item in mutations:
            if item.get("op") not in {"update", "stale", "noop"}:
                raise MemoryError("evolution cannot create solution facts")
            fact_id = item.get("fact_id")
            if item["op"] == "noop":
                continue
            if fact_id not in state["facts"]:
                raise MemoryError(f"evolution targets missing {fact_id}")
            values = dict(item.get("values") or {})
            if item["op"] == "stale":
                values["status"] = "stale"
                if not values.get("status_reason"):
                    raise MemoryError("evolution stale needs status_reason")
            state["facts"][fact_id].update(values)
        schema_changes = report.get("schema_changes", [])
        if schema_changes:
            state["schema_version"] += 1
            state["schema_history"].append({"at": utcnow(), "changes": schema_changes,
                                            "source": "evolution"})
        state["state_version"] += 1
        checkpoint = {
            "checkpoint": "after-a3-before-a4", "at": utcnow(),
            "state_version_before": before, "state_version_after": state["state_version"],
            "schema_version_before": schema_before, "schema_version_after": state["schema_version"],
            "mutations": mutations, "schema_changes": schema_changes,
            "report": report.get("report", ""),
        }
        state["evolution_checkpoints"].append(checkpoint)
        state["journal"].append({"kind": "evolve", **checkpoint})
        self._save(state)
        return checkpoint


class XMemoryBackend(FileMemoryBackend):
    """xmemory adapter with a local audit shadow.

    Provisioning is intentionally explicit: every stream gets a different pre-created
    instance id.  `provision_xmemory.py` creates those instances from one C0 schema and
    structured-mutation seed; no shared live instance is accepted.
    """

    name = "xmemory"

    def __init__(self, state_dir: Path, snapshot: Path, mode: str, seed: int,
                 instance_id: str, xmemcli: str = "xmemcli"):
        super().__init__(state_dir, snapshot, mode, seed)
        self.instance_id = instance_id
        self.xmemcli = xmemcli

    def prepare(self, reset: bool = False) -> dict[str, Any]:
        if reset and self.state_file.exists():
            raise MemoryError(
                "cannot reset a remote xmemory instance in place; provision a fresh unique "
                "instance id and use a new output/state directory")
        state = super().prepare(reset=reset)
        if not self.instance_id:
            raise MemoryError("xmemory backend requires a unique instance_id for this mode/seed")
        recorded = state.get("xmemory_instance_id")
        if recorded and recorded != self.instance_id:
            raise MemoryError("xmemory audit shadow points at a different instance")
        state["backend"] = self.name
        state["fallback"] = False
        state["xmemory_instance_id"] = self.instance_id
        self._save(state)
        return state

    def _cmd(self, args: list[str], timeout: int = 180) -> tuple[str, float]:
        started = time.monotonic()
        p = subprocess.run([self.xmemcli, "--json", "--instance-id", self.instance_id, *args],
                           text=True, capture_output=True, timeout=timeout)
        wall = time.monotonic() - started
        if p.returncode:
            raise MemoryError(f"xmemcli {' '.join(args[:2])} failed: {p.stderr[-1200:]}")
        return p.stdout, wall

    def read(self, task_id: str, slices: list[str], query: str, top_k: int = 20) -> ReadResult:
        object_types = [SLICE_OBJECTS[s] for s in slices]
        prompt = (f"Return at most {top_k} active technical facts relevant to task {task_id}. "
                  f"Only object types {object_types}. Include exact fact_id, statement, evidence, "
                  f"human_notes and status. Task: {query}")
        output, wall = self._cmd(["read", prompt, "--read-mode", "raw"])
        provider_ids = list(dict.fromkeys(FACT_ID_RE.findall(output)))
        # The provider chooses IDs; the audit shadow enforces active/slice boundaries and renders
        # deterministic exact content so a stale row can never be injected accidentally.
        state = self._load()
        facts = [state["facts"][i] for i in provider_ids if i in state["facts"]
                 and state["facts"][i].get("status") == "active"
                 and state["facts"][i].get("slice") in slices][:top_k]
        ids = [f["fact_id"] for f in facts]
        exact = render_facts(facts, self.name, state["state_version"])
        chars = len(exact)
        return ReadResult(facts, exact, {
            "backend": self.name, "fallback": False, "instance_id": self.instance_id,
            "state_version": state["state_version"], "fact_ids": ids,
            "provider_fact_ids": provider_ids,
            "provider_response_sha256": hashlib.sha256(output.encode()).hexdigest(),
            "provider_response_chars": len(output),
            "fact_statuses": {f["fact_id"]: f.get("status") for f in facts},
            "facts_count": len(ids), "chars": chars, "estimated_tokens": _tokens(chars),
            "wall_sec": round(wall, 4), "provider_usage": None,
            "provider_calls": 1,
        })

    def apply(self, batch: dict[str, Any], injected_ids: list[str], task_id: str) -> dict[str, Any]:
        normalized = validate_batch(batch, injected_ids, task_id)
        payload = to_xmemory_mutations(normalized)
        started = time.monotonic()
        if payload["structured_mutations"]:
            # xmemcli does not expose structured writes; use the same authenticated API path
            # through the small provision helper, which reads credentials without logging them.
            helper = Path(__file__).with_name("provision_xmemory.py")
            p = subprocess.run([os.environ.get("PYTHON", "python3"), str(helper), "write",
                                "--instance", self.instance_id], input=json.dumps(payload),
                               text=True, capture_output=True, timeout=180)
            if p.returncode:
                raise MemoryError(f"xmemory structured write failed: {p.stderr[-1200:]}")
        result = super().apply(normalized, injected_ids, task_id)
        result.update({"backend": self.name, "fallback": False,
                       "instance_id": self.instance_id,
                       "provider_calls": 1 if payload["structured_mutations"] else 0,
                       "wall_sec": round(time.monotonic() - started, 4)})
        return result

    def review_schema_suggestions(self) -> dict[str, Any]:
        output, wall = self._cmd(["schema", "suggestions", "review"], timeout=360)
        try:
            payload = json.loads(output)
        except json.JSONDecodeError:
            payload = {"raw": output}
        return {"payload": payload, "wall_sec": round(wall, 4)}

    def evolve(self, report: dict[str, Any]) -> dict[str, Any]:
        mutations = []
        for item in report.get("mutations", []):
            if item.get("op") == "noop":
                continue
            fact_id = item.get("fact_id", "")
            prefix = fact_id.split(":")[1].split("-")[0] if ":" in fact_id else ""
            slice_name = PREFIX_SLICES.get(prefix)
            if not slice_name:
                raise MemoryError(f"unknown evolution fact id: {fact_id}")
            values = dict(item.get("values") or {})
            if item.get("op") == "stale":
                values["status"] = "stale"
            for key in ("evidence", "human_notes"):
                if isinstance(values.get(key), (list, dict)):
                    values[key] = json.dumps(values[key], ensure_ascii=False)
            mutations.append({"object_mutation": {"object_type": SLICE_OBJECTS[slice_name],
                                                    "update": {"key": {"fact_id": fact_id},
                                                               "values": values}}})
        if mutations:
            helper = Path(__file__).with_name("provision_xmemory.py")
            p = subprocess.run([os.environ.get("PYTHON", "python3"), str(helper), "write",
                                "--instance", self.instance_id],
                               input=json.dumps({"structured_mutations": mutations}), text=True,
                               capture_output=True, timeout=180)
            if p.returncode:
                raise MemoryError(f"xmemory evolution write failed: {p.stderr[-1200:]}")

        decisions = report.get("schema_suggestion_decisions", [])
        proposal = report.get("proposal_version")
        schema_applied = False
        if decisions:
            if not proposal:
                raise MemoryError("schema decisions require proposal_version")
            args = ["schema", "suggestions", "decide", "--proposal-version", proposal]
            for decision in decisions:
                action = decision.get("decision")
                if action not in {"accept", "reject", "defer"}:
                    raise MemoryError(f"invalid schema decision: {action}")
                args += [f"--{action}", decision["fingerprint"]]
            self._cmd(args, timeout=180)
        if report.get("apply_schema_suggestions"):
            if not proposal or not report.get("confirm_destructive_preview_reviewed"):
                raise MemoryError("schema apply requires proposal_version and explicit preview confirmation")
            self._cmd(["schema", "suggestions", "apply", "--proposal-version", proposal,
                       "--confirm-destructive"], timeout=360)
            schema_applied = True
        local_report = dict(report)
        local_report["schema_changes"] = (report.get("schema_changes", [])
                                          if schema_applied else [])
        checkpoint = super().evolve(local_report)
        checkpoint["xmemory_schema_applied"] = schema_applied
        return checkpoint


def to_xmemory_mutations(batch: dict[str, Any]) -> dict[str, Any]:
    """Translate the runner protocol to xmemory structured mutations."""
    out: list[dict[str, Any]] = []
    for item in batch.get("mutations", []):
        if item["op"] == "noop":
            continue
        fact_id = item.get("fact_id") or item.get("fact", {}).get("fact_id")
        slice_name = (item.get("fact") or {}).get("slice") or PREFIX_SLICES.get(
            fact_id.split(":")[1].split("-")[0])
        object_type = SLICE_OBJECTS[slice_name]
        action = "create" if item["op"] == "create" else "update"
        values = dict(item.get("fact") if action == "create" else item.get("values") or {})
        values.pop("fact_id", None)
        values.pop("slice", None)
        values.pop("object_type", None)
        for key in ("evidence", "human_notes"):
            if isinstance(values.get(key), (list, dict)):
                values[key] = json.dumps(values[key], ensure_ascii=False)
        if item["op"] == "stale":
            values["status"] = "stale"
        out.append({"object_mutation": {"object_type": object_type, action: {
            "key": {"fact_id": fact_id}, "values": values}}})
    task = batch.get("task") or {}
    if task:
        out.append({"object_mutation": {"object_type": "Task", "create": {
            "key": {"task_id": task["task_id"]},
            "values": {"title": task.get("title", task["task_id"]), "at": utcnow()}}}})
        for verb, ids in (("used", task.get("used_facts", [])),
                          ("produced", task.get("produced_facts", []))):
            for fact_id in ids:
                object_type = SLICE_OBJECTS[PREFIX_SLICES[fact_id.split(":")[1].split("-")[0]]]
                snake = {"ApiContract": "api_contract", "Invariant": "invariant",
                         "DataOwnership": "data_ownership", "ConfigFlag": "config_flag",
                         "Gotcha": "gotcha"}[object_type]
                relation = f"task_{verb}_{snake}"
                out.append({"relation_mutation": {"relation_type": relation, "create": {
                    "endpoints": [
                        {"object_name": "task", "key": {"task_id": task["task_id"]}},
                        {"object_name": snake, "key": {"fact_id": fact_id}},
                    ]}}})
    return {"structured_mutations": out}


def open_backend(cfg: dict[str, Any], state_dir: Path, snapshot: Path,
                 mode: str, seed: int) -> FileMemoryBackend:
    memory = cfg.get("memory", {})
    backend = memory.get("backend", "file")
    if backend == "file":
        return FileMemoryBackend(state_dir, snapshot, mode, seed)
    if backend == "xmemory":
        key = f"{mode}.seed{seed}"
        instances = memory.get("xmemory_instances", {})
        instance_id = instances.get(key, "")
        return XMemoryBackend(state_dir, snapshot, mode, seed, instance_id,
                              memory.get("xmemcli", "xmemcli"))
    raise MemoryError(f"unknown memory.backend={backend}")
