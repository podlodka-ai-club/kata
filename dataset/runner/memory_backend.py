#!/usr/bin/env python3
"""Durable, isolated memory backends for the chronological kata evaluation.

The file backend is the reproducible fallback and the fake used by selftests. The
xmemory backend keeps all semantic state in cloud objects/relations plus a typed
MemoryState manifest. Every coding/curator session gets a newly cloned instance;
local lineage artifacts contain remote ids/hashes only, never fact state.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
import uuid
import zlib
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
COMMON_REMOTE_FACT_FIELDS = {
    "statement", "content", "evidence", "confidence", "status", "provenance",
    "auto_approved", "human_notes", "question", "source", "superseded_by", "status_reason",
}
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


def state_sha256(state: dict[str, Any]) -> str:
    """Stable digest of the canonical state stored inside xmemory."""
    payload = json.dumps(state, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False).encode()
    return hashlib.sha256(payload).hexdigest()


SNAPSHOT_ENCODING = "zlib+base64:"
MANIFEST_CHUNK_PREFIX = "chunked:v1:"
MANIFEST_CHUNK_CHARS = 6000


def encode_state_snapshot(state: dict[str, Any]) -> str:
    """Keep the canonical manifest below xmemory's per-string size boundary."""
    raw = json.dumps(state, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False).encode()
    return SNAPSHOT_ENCODING + base64.b64encode(zlib.compress(raw, level=9)).decode("ascii")


def decode_state_snapshot(value: str) -> dict[str, Any]:
    """Decode current compact manifests and the raw-JSON C0 format."""
    if value.startswith(SNAPSHOT_ENCODING):
        encoded = value[len(SNAPSHOT_ENCODING):]
        try:
            raw = zlib.decompress(base64.b64decode(encoded, validate=True))
        except (ValueError, zlib.error) as error:
            raise MemoryError("invalid compressed MemoryState snapshot") from error
        return json.loads(raw)
    return json.loads(value)


def state_object(state: dict[str, Any]) -> dict[str, Any]:
    """Common manifest metadata; the encoded snapshot may be chunked remotely."""
    return {
        "stream_id": "stream",
        "state_version": state["state_version"],
        "schema_version": state["schema_version"],
        "mode": state["mode"],
        "seed": state["seed"],
        "c0_sha256": state["c0_sha256"],
        "snapshot_json": encode_state_snapshot(state),
        "snapshot_sha256": state_sha256(state),
        "parent_instance_id": (state.get("lineage") or {}).get("parent_instance_id", ""),
        "session_id": (state.get("lineage") or {}).get("session_id", ""),
        "session_kind": (state.get("lineage") or {}).get("session_kind", ""),
    }


def manifest_records(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Return a small root record plus fixed-size MemoryState chunk records.

    `manifest_chunk_count` only grows, so every later write can update existing chunk
    primary keys and create solely the newly required suffix. The field is canonical state,
    making clone verification independent of local bookkeeping.
    """
    count = max(1, int(state.get("manifest_chunk_count", 0)))
    while True:
        state["manifest_chunk_count"] = count
        encoded = encode_state_snapshot(state)
        needed = max(1, math.ceil(len(encoded) / MANIFEST_CHUNK_CHARS))
        if needed <= count:
            break
        count = needed
    chunks = [encoded[index:index + MANIFEST_CHUNK_CHARS]
              for index in range(0, len(encoded), MANIFEST_CHUNK_CHARS)]
    chunks.extend([""] * (count - len(chunks)))
    common = state_object(state)
    root = {**common, "stream_id": "stream",
            "snapshot_json": f"{MANIFEST_CHUNK_PREFIX}{count}"}
    records = [root]
    for index, chunk in enumerate(chunks, 1):
        records.append({**common, "stream_id": f"stream:{index:04d}",
                        "snapshot_json": chunk})
    return records


def manifest_mutations(state: dict[str, Any], root_action: str,
                       previous_chunks: int = 0) -> list[dict[str, Any]]:
    if root_action not in {"create", "update"}:
        raise MemoryError(f"invalid manifest action: {root_action}")
    records = manifest_records(state)
    mutations = []
    for index, record in enumerate(records):
        action = root_action if index == 0 else ("update" if index <= previous_chunks else "create")
        mutations.append({"object_mutation": {"object_type": "MemoryState", action: {
            "key": {"stream_id": record["stream_id"]},
            "values": {key: value for key, value in record.items() if key != "stream_id"},
        }}})
    return mutations


def _json_field(value: Any) -> Any:
    return json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else value


def state_seed_payload(state: dict[str, Any]) -> dict[str, Any]:
    """Materialize a canonical state as typed xmemory objects and relations."""
    mutations: list[dict[str, Any]] = []
    for fact in state["facts"].values():
        values = {key: _json_field(value) for key, value in fact.items()
                  if key not in {"fact_id", "slice", "object_type", "title", "created_at"}}
        mutations.append({"object_mutation": {"object_type": fact["object_type"], "create": {
            "key": {"fact_id": fact["fact_id"]}, "values": values}}})
    for task in state.get("tasks", {}).values():
        values = {"title": task.get("title", task["task_id"]), "at": task.get("at", ""),
                  "used_facts": _json_field(task.get("used_facts", [])),
                  "produced_facts": _json_field(task.get("produced_facts", [])),
                  "decisions": _json_field(task.get("decisions", []))}
        mutations.append({"object_mutation": {"object_type": "Task", "create": {
            "key": {"task_id": task["task_id"]}, "values": values}}})
        for verb, ids in (("used", task.get("used_facts", [])),
                          ("produced", task.get("produced_facts", []))):
            for fact_id in ids:
                fact = state["facts"].get(fact_id)
                if not fact:
                    raise MemoryError(f"Task relation targets missing fact {fact_id}")
                snake = {"ApiContract": "api_contract", "Invariant": "invariant",
                         "DataOwnership": "data_ownership", "ConfigFlag": "config_flag",
                         "Gotcha": "gotcha"}[fact["object_type"]]
                mutations.append({"relation_mutation": {
                    "relation_type": f"task_{verb}_{snake}", "create": {"endpoints": [
                        {"object_name": "task", "key": {"task_id": task["task_id"]}},
                        {"object_name": snake, "key": {"fact_id": fact_id}},
                    ]}}})
    mutations.extend(manifest_mutations(state, "create"))
    return {"structured_mutations": mutations}


def manifest_update_mutations(state: dict[str, Any], previous_chunks: int) -> list[dict[str, Any]]:
    return manifest_mutations(state, "update", previous_chunks)


def apply_batch_to_state(state: dict[str, Any], batch: dict[str, Any],
                         injected_ids: list[str], task_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Pure lifecycle transition shared by file and cloud implementations."""
    batch = validate_batch(batch, injected_ids, task_id)
    state = json.loads(json.dumps(state))
    before = state["state_version"]
    counts = {"create": 0, "update": 0, "stale": 0, "noop": 0}
    gotchas = 0
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
    summary = {"state_version_before": before, "state_version_after": state["state_version"],
               "mutations": counts, "gotchas": gotchas,
               "used_facts": state["tasks"][task_id]["used_facts"],
               "produced_facts": state["tasks"][task_id]["produced_facts"]}
    return state, summary


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

    def load_state(self) -> dict[str, Any]:
        return self._load()

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
            "existing_fact_ids": sorted(state["facts"]),
            "fact_statuses": {f["fact_id"]: f.get("status") for f in facts},
            "facts_count": len(facts),
            "chars": chars,
            "estimated_tokens": _tokens(chars),
            "wall_sec": round(time.monotonic() - started, 4),
            "provider_calls": 0, "provider_usage": None,
        })

    def apply(self, batch: dict[str, Any], injected_ids: list[str], task_id: str) -> dict[str, Any]:
        started = time.monotonic()
        state, summary = apply_batch_to_state(self._load(), batch, injected_ids, task_id)
        self._save(state)
        return {
            "backend": self.name, "fallback": True, "ok": True,
            **summary, "schema_changes": 0,
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


class SubprocessXMemoryTransport:
    """Production transport; the helper owns credentials and HTTP response parsing."""

    def __init__(self, xmemcli: str = "xmemcli"):
        self.xmemcli = xmemcli
        self.helper = Path(__file__).with_name("provision_xmemory.py")
        self.last_load_metrics: dict[str, Any] = {}

    def _helper(self, args: list[str], payload: dict[str, Any] | None = None,
                timeout: int = 420) -> tuple[dict[str, Any], float]:
        started = time.monotonic()
        command = [sys.executable, str(self.helper), *args]
        p = subprocess.run(command, input=json.dumps(payload) if payload is not None else None,
                           text=True, capture_output=True, timeout=timeout)
        if p.returncode:
            raise MemoryError(f"xmemory helper {' '.join(args[:2])} failed: {p.stderr[-1600:]}")
        try:
            return json.loads(p.stdout), time.monotonic() - started
        except json.JSONDecodeError as exc:
            raise MemoryError(f"xmemory helper returned invalid JSON: {p.stdout[-1200:]}") from exc

    def clone(self, parent_id: str, name: str, mode: str, seed: int,
              session_id: str) -> tuple[str, dict[str, Any]]:
        doc, wall = self._helper(["clone", "--parent", parent_id, "--name", name,
                                  "--mode", mode, "--seed", str(seed),
                                  "--session-id", session_id, "--xmemcli", self.xmemcli])
        return doc["instance_id"], {**doc, "wall_sec": round(wall, 4)}

    def load_state(self, instance_id: str) -> dict[str, Any]:
        doc, wall = self._helper(["state", "--instance", instance_id])
        self.last_load_metrics = {"provider_calls": doc.get("provider_calls", 1),
                                  "wall_sec": round(wall, 4)}
        return doc["state"]

    def retrieve(self, instance_id: str, task_id: str, slices: list[str], query: str,
                 top_k: int, fact_ids: list[str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        doc, wall = self._helper(["read-facts", "--instance", instance_id,
                                  "--task", task_id, "--top-k", str(top_k),
                                  "--slices", *slices], {"query": query, "fact_ids": fact_ids})
        return doc["facts"], {**doc.get("metrics", {}), "wall_sec": round(wall, 4)}

    def write(self, instance_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        doc, wall = self._helper(["write", "--instance", instance_id], payload)
        return {**doc, "wall_sec": round(wall, 4)}

    def delete(self, instance_id: str) -> dict[str, Any]:
        doc, wall = self._helper(["delete", "--instance", instance_id])
        return {**doc, "wall_sec": round(wall, 4)}


class XMemoryBackend:
    """Cloud-only xmemory lineage: one fresh child instance per agent session.

    The local lineage file contains only remote ids and hashes. Facts, tasks, lifecycle,
    journal and the canonical clone manifest live inside xmemory's typed MemoryState object.
    """

    name = "xmemory"

    def __init__(self, state_dir: Path, snapshot: Path, mode: str, seed: int,
                 c0_instance_id: str, session_id: str, xmemcli: str = "xmemcli",
                 name_prefix: str = "kata", transport: Any | None = None,
                 delete_parent_after_clone: bool = False):
        self.state_dir = state_dir
        self.lineage_file = state_dir / "lineage.json"
        self.snapshot = snapshot
        self.mode = mode
        self.seed = seed
        self.c0_instance_id = c0_instance_id
        self.session_id = session_id
        self.name_prefix = name_prefix
        self.transport = transport or SubprocessXMemoryTransport(xmemcli)
        self.xmemcli = xmemcli
        self.instance_id = ""
        self.parent_instance_id = ""
        self.clone_metrics: dict[str, Any] = {}
        self.delete_parent_after_clone = delete_parent_after_clone
        self.retention_metrics: dict[str, Any] | None = None

    def _lineage(self) -> dict[str, Any]:
        if not self.lineage_file.exists():
            return {"format_version": 1, "mode": self.mode, "seed": self.seed,
                    "c0_instance_id": self.c0_instance_id, "sessions": []}
        return json.loads(self.lineage_file.read_text(encoding="utf-8"))

    def _save_lineage(self, lineage: dict[str, Any]) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.lineage_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(lineage, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, self.lineage_file)

    def prepare(self, reset: bool = False) -> dict[str, Any]:
        if self.instance_id:
            return self.load_state()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        if reset and self.lineage_file.exists():
            self.lineage_file.unlink()
        lineage = self._lineage()
        if (lineage["mode"], lineage["seed"], lineage["c0_instance_id"]) != (
                self.mode, self.seed, self.c0_instance_id):
            raise MemoryError("xmemory lineage belongs to another mode/seed/C0")
        self.parent_instance_id = (lineage["sessions"][-1]["instance_id"]
                                   if lineage["sessions"] else self.c0_instance_id)
        if not self.parent_instance_id:
            raise MemoryError("xmemory requires memory.c0_instance_id")
        suffix = uuid.uuid4().hex[:8]
        safe_mode = self.mode.replace("+", "-")
        name = f"{self.name_prefix}-{safe_mode}-s{self.seed}-{self.session_id}-{suffix}"
        self.instance_id, self.clone_metrics = self.transport.clone(
            self.parent_instance_id, name, self.mode, self.seed, self.session_id)
        state = self.load_state()
        expected = hashlib.sha256(self.snapshot.read_bytes()).hexdigest()
        if state.get("c0_sha256") != expected:
            raise MemoryError("cloud child was not cloned from configured C0")
        entry = {"session_id": self.session_id, "parent_instance_id": self.parent_instance_id,
                 "instance_id": self.instance_id, "state_version": state["state_version"],
                 "state_sha256": state_sha256(state), "created_at": utcnow()}
        lineage["sessions"].append(entry)
        self._save_lineage(lineage)
        if self.delete_parent_after_clone and self.parent_instance_id != self.c0_instance_id:
            deleted = self.transport.delete(self.parent_instance_id)
            self.retention_metrics = {
                "deleted_instance_id": self.parent_instance_id,
                "after_verified_clone": self.instance_id,
                **deleted,
            }
            for prior in lineage["sessions"]:
                if prior["instance_id"] == self.parent_instance_id:
                    prior["deleted_at"] = utcnow()
                    prior["delete"] = deleted
                    break
            self._save_lineage(lineage)
        return state

    def load_state(self) -> dict[str, Any]:
        if not self.instance_id:
            raise MemoryError("xmemory session instance is not prepared")
        state = self.transport.load_state(self.instance_id)
        if state_sha256(state) != state_object(state)["snapshot_sha256"]:
            raise MemoryError("remote MemoryState digest verification failed")
        return state

    def read(self, task_id: str, slices: list[str], query: str, top_k: int = 20) -> ReadResult:
        state = self.load_state()
        state_load = getattr(self.transport, "last_load_metrics", {})
        candidates = [fact for fact in state["facts"].values()
                      if fact.get("status") == "active" and fact.get("slice") in slices]
        words = {word for word in re.findall(r"[a-zA-Z_]{4,}", query.lower())}
        candidates.sort(key=lambda fact: (
            -sum(word in (fact.get("content") or fact.get("statement", "")).lower()
                 for word in words),
            fact["fact_id"],
        ))
        candidate_ids = [fact["fact_id"] for fact in candidates[:top_k]]
        facts, provider = self.transport.retrieve(
            self.instance_id, task_id, slices, query, top_k, candidate_ids)
        filtered = []
        for fact in facts:
            fact_id = fact.get("fact_id", "")
            prefix = fact_id.split(":")[1].split("-")[0] if ":" in fact_id else ""
            fact.setdefault("slice", PREFIX_SLICES.get(prefix))
            fact.setdefault("object_type", SLICE_OBJECTS.get(fact.get("slice", "")))
            for key in ("evidence", "human_notes"):
                if isinstance(fact.get(key), str):
                    try:
                        fact[key] = json.loads(fact[key])
                    except json.JSONDecodeError:
                        pass
            if fact.get("status") == "active" and fact.get("slice") in slices:
                filtered.append(fact)
        facts = filtered[:top_k]
        ids = [f["fact_id"] for f in facts]
        exact = render_facts(facts, self.name, state["state_version"])
        chars = len(exact)
        return ReadResult(facts, exact, {
            "backend": self.name, "fallback": False, "instance_id": self.instance_id,
            "parent_instance_id": self.parent_instance_id,
            "session_instance_created": True, "clone": self.clone_metrics,
            "retention": self.retention_metrics,
            "state_version": state["state_version"], "fact_ids": ids,
            "existing_fact_ids": sorted(state["facts"]),
            "provider_fact_ids": provider.get("provider_fact_ids", ids),
            "provider_response_sha256": provider.get("provider_response_sha256"),
            "provider_response_chars": provider.get("provider_response_chars"),
            "fact_statuses": {f["fact_id"]: f.get("status") for f in facts},
            "facts_count": len(ids), "chars": chars, "estimated_tokens": _tokens(chars),
            "wall_sec": round(state_load.get("wall_sec", 0.0)
                              + provider.get("wall_sec", 0.0), 4), "provider_usage": None,
            "provider_calls": (state_load.get("provider_calls", 1)
                               + provider.get("provider_calls", 1)),
            "clone_provider_calls": self.clone_metrics.get("provider_calls"),
        })

    def apply(self, batch: dict[str, Any], injected_ids: list[str], task_id: str) -> dict[str, Any]:
        started = time.monotonic()
        normalized = validate_batch(batch, injected_ids, task_id)
        current_state = self.load_state()
        read_before = getattr(self.transport, "last_load_metrics", {})
        previous_chunks = int(current_state.get("manifest_chunk_count", 0))
        next_state, summary = apply_batch_to_state(
            current_state, normalized, injected_ids, task_id)
        payload = to_xmemory_mutations(normalized)
        payload["structured_mutations"].extend(
            manifest_update_mutations(next_state, previous_chunks))
        provider = self.transport.write(self.instance_id, payload)
        verified = self.load_state()
        read_after = getattr(self.transport, "last_load_metrics", {})
        if state_sha256(verified) != state_sha256(next_state):
            raise MemoryError("xmemory write committed but remote manifest does not match")
        return {"backend": self.name, "fallback": False, "ok": True, **summary,
                "schema_changes": 0, "instance_id": self.instance_id,
                "parent_instance_id": self.parent_instance_id,
                "provider_calls": (read_before.get("provider_calls", 1)
                                   + provider.get("provider_calls", 1)
                                   + read_after.get("provider_calls", 1)),
                "wall_sec": round(time.monotonic() - started, 4),
                "remote_state_sha256": state_sha256(verified)}

    def _cmd(self, args: list[str], timeout: int = 180) -> tuple[str, float]:
        started = time.monotonic()
        p = subprocess.run([self.xmemcli, "--json", "--instance-id", self.instance_id, *args],
                           text=True, capture_output=True, timeout=timeout)
        if p.returncode:
            raise MemoryError(f"xmemcli {' '.join(args[:2])} failed: {p.stderr[-1200:]}")
        return p.stdout, time.monotonic() - started

    def review_schema_suggestions(self) -> dict[str, Any]:
        if hasattr(self.transport, "review_schema_suggestions"):
            return self.transport.review_schema_suggestions(self.instance_id)
        output, wall = self._cmd(["schema", "suggestions", "review"], timeout=360)
        try:
            payload = json.loads(output)
        except json.JSONDecodeError:
            payload = {"raw": output}
        return {"payload": payload, "wall_sec": round(wall, 4)}

    def evolve(self, report: dict[str, Any]) -> dict[str, Any]:
        state = self.load_state()
        before = state["state_version"]
        schema_before = state["schema_version"]
        mutations = []
        for item in report.get("mutations", []):
            if item.get("op") not in {"update", "stale", "noop"}:
                raise MemoryError("evolution cannot create solution facts")
            if item.get("op") == "noop":
                continue
            fact_id = item.get("fact_id", "")
            if fact_id not in state["facts"]:
                raise MemoryError(f"evolution targets missing {fact_id}")
            values = dict(item.get("values") or {})
            fact = state["facts"][fact_id]
            for structural in ("fact_id", "object_type", "slice", "created_at"):
                if structural not in values:
                    continue
                if values[structural] != fact.get(structural):
                    raise MemoryError(
                        f"evolution cannot change structural field {structural} for {fact_id}")
                values.pop(structural)
            allowed = COMMON_REMOTE_FACT_FIELDS | set(state["schema"].get(fact["object_type"], []))
            unknown = sorted(set(values) - allowed)
            if unknown:
                raise MemoryError(f"evolution has unknown typed fields for {fact_id}: {unknown}")
            if item.get("op") == "stale":
                values["status"] = "stale"
                if not values.get("status_reason"):
                    raise MemoryError("evolution stale needs status_reason")
            fact.update(values)
            remote_values = {key: _json_field(value) for key, value in values.items()}
            mutations.append({"object_mutation": {
                "object_type": fact["object_type"], "update": {
                    "key": {"fact_id": fact_id}, "values": remote_values}}})

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
                raise MemoryError("schema apply requires proposal version and preview confirmation")
            self._cmd(["schema", "suggestions", "apply", "--proposal-version", proposal,
                       "--confirm-destructive"], timeout=360)
            schema_applied = True
            state["schema_version"] += 1
            state["schema_history"].append({"at": utcnow(),
                                            "changes": report.get("schema_changes", []),
                                            "source": "evolution"})
        state["state_version"] += 1
        checkpoint = {"checkpoint": "after-a3-before-a4", "at": utcnow(),
                      "state_version_before": before,
                      "state_version_after": state["state_version"],
                      "schema_version_before": schema_before,
                      "schema_version_after": state["schema_version"],
                      "mutations": report.get("mutations", []),
                      "schema_changes": report.get("schema_changes", []) if schema_applied else [],
                      "report": report.get("report", ""),
                      "xmemory_schema_applied": schema_applied}
        state["evolution_checkpoints"].append(checkpoint)
        state["journal"].append({"kind": "evolve", **checkpoint})
        previous_chunks = int(state.get("manifest_chunk_count", 0))
        mutations.extend(manifest_update_mutations(state, previous_chunks))
        self.transport.write(self.instance_id, {"structured_mutations": mutations})
        if state_sha256(self.load_state()) != state_sha256(state):
            raise MemoryError("xmemory evolution manifest verification failed")
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
        values.pop("created_at", None)
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
            "values": {"title": task.get("title", task["task_id"]), "at": utcnow(),
                       "used_facts": _json_field(task.get("used_facts", [])),
                       "produced_facts": _json_field(task.get("produced_facts", [])),
                       "decisions": _json_field(task.get("decisions", []))}}}})
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
                 mode: str, seed: int, session_id: str = "session",
                 transport: Any | None = None) -> Any:
    memory = cfg.get("memory", {})
    backend = memory.get("backend", "file")
    if backend == "file":
        return FileMemoryBackend(state_dir, snapshot, mode, seed)
    if backend == "xmemory":
        return XMemoryBackend(
            state_dir, snapshot, mode, seed, memory.get("c0_instance_id", ""), session_id,
            memory.get("xmemcli", "xmemcli"), memory.get("instance_name_prefix", "kata"),
            transport=transport,
            delete_parent_after_clone=bool(memory.get("delete_parent_after_clone", False)))
    raise MemoryError(f"unknown memory.backend={backend}")


def cleanup_xmemory_tail(state_dir: Path, xmemcli: str = "xmemcli",
                         transport: Any | None = None) -> dict[str, Any]:
    """Delete only the live tail recorded by this runner lineage, preserving audit metadata."""
    lineage_file = state_dir / "lineage.json"
    if not lineage_file.exists():
        raise MemoryError(f"xmemory lineage missing: {lineage_file}")
    lineage = json.loads(lineage_file.read_text(encoding="utf-8"))
    sessions = lineage.get("sessions") or []
    if not sessions:
        raise MemoryError("xmemory lineage has no child sessions to clean up")
    tail = sessions[-1]
    if tail.get("deleted_at"):
        return {"ok": True, "instance_id": tail["instance_id"], "already_deleted": True}
    active_transport = transport or SubprocessXMemoryTransport(xmemcli)
    deleted = active_transport.delete(tail["instance_id"])
    tail["deleted_at"] = utcnow()
    tail["delete"] = deleted
    tmp = lineage_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(lineage, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, lineage_file)
    return {"ok": True, "instance_id": tail["instance_id"], **deleted}
