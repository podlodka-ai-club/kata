#!/usr/bin/env python3
"""Provision/clone cloud-only xmemory sessions and send structured mutations.

Credentials are read by xmemcli from `.xmemrc.json` (or XMEM_* env vars).  The
helper never prints keys. A frozen typed C0 template is provisioned once; every
coding/curator session is a fresh child materialized cloud-to-cloud from its parent.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from memory_backend import (PREFIX_SLICES, SLICE_OBJECTS, decode_state_snapshot, new_state,
                            state_seed_payload, state_sha256)


ROOT = Path(__file__).resolve().parents[2]


def credentials() -> tuple[str, str]:
    api_url = os.environ.get("XMEM_API_URL")
    api_key = os.environ.get("XMEM_API_KEY")
    if api_url and api_key:
        return api_url, api_key
    current = Path.cwd().resolve()
    for directory in (current, *current.parents, Path.home()):
        rc = directory / ".xmemrc.json"
        if rc.exists():
            doc = json.loads(rc.read_text(encoding="utf-8"))
            return doc.get("api_url", "https://api.xmemory.ai"), doc["api_key"]
    raise RuntimeError("xmemory credentials not found; run xmemcli auth login outside the repo")


def request_json(instance: str, operation: str, payload: dict) -> dict:
    api_url, api_key = credentials()
    request = urllib.request.Request(
        f"{api_url.rstrip('/')}/instances/{instance}/{operation}",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        body = error.read().decode(errors="replace")
        raise RuntimeError(f"xmemory HTTP {error.code}: {body[-1200:]}") from error


def delete_instance(instance: str) -> dict:
    """Delete one runner-owned ephemeral instance without ever exposing credentials."""
    api_url, api_key = credentials()
    request = urllib.request.Request(
        f"{api_url.rstrip('/')}/instances/{instance}",
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        method="DELETE",
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as error:
        body = error.read().decode(errors="replace")
        if error.code == 404:
            return {"already_deleted": True}
        raise RuntimeError(f"xmemory delete HTTP {error.code}: {body[-1200:]}") from error


def post(instance: str, payload: dict) -> dict:
    doc = request_json(instance, "write", payload)
    if doc.get("errors"):
        raise RuntimeError(f"xmemory write errors: {doc['errors']}")
    return doc


def unwrap(doc: dict) -> dict:
    if doc.get("errors"):
        raise RuntimeError(f"xmemory response errors: {doc['errors']}")
    items = doc.get("items") or []
    return items[0] if items else doc


def _walk_dicts(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _merged_record(node: dict) -> dict:
    merged = {}
    for key in ("key", "values", "fields", "properties", "attributes", "data"):
        if isinstance(node.get(key), dict):
            merged.update(node[key])
    # Current xresponse objects encode fields as [{name, value:{*_value: ...}}].
    # Keep accepting the older dict-shaped values above for compatibility.
    if isinstance(node.get("fields"), list):
        for field in node["fields"]:
            if not isinstance(field, dict) or not isinstance(field.get("name"), str):
                continue
            value = field.get("value")
            if isinstance(value, dict):
                typed = next((item for key, item in value.items() if key.endswith("_value")), value)
                merged[field["name"]] = typed
            else:
                merged[field["name"]] = value
    merged.update({k: v for k, v in node.items()
                   if not isinstance(v, (dict, list)) and k not in {"xuid"}})
    return merged


def state_from_read_response(doc: dict) -> dict:
    item = unwrap(doc)
    for node in _walk_dicts(item.get("reader_result", item)):
        record = _merged_record(node)
        if "snapshot_json" not in record:
            continue
        if str(record["snapshot_json"]).startswith("chunked:v1:"):
            raise RuntimeError("chunked MemoryState requires remote_state materialization")
        state = decode_state_snapshot(record["snapshot_json"])
        digest = record.get("snapshot_sha256")
        if not digest or state_sha256(state) != digest:
            raise RuntimeError("remote MemoryState snapshot digest mismatch")
        return state
    raise RuntimeError("xmemory response has no readable MemoryState object")


def _memory_state_records(doc: dict) -> dict[str, dict]:
    item = unwrap(doc)
    records = {}
    for node in _walk_dicts(item.get("reader_result", item)):
        record = _merged_record(node)
        stream_id = record.get("stream_id")
        if isinstance(stream_id, str) and "snapshot_json" in record:
            records[stream_id] = record
    return records


def remote_state(instance: str, with_metrics: bool = False):
    payload = {
        "query": "Return this MemoryState object exactly, including snapshot_json and digest.",
        "mode": "xresponse", "skip_suggestion_capture": True,
        "scope": {"objects": [{"type": "MemoryState",
                                "key": {"key": {"stream_id": "stream"}}}],
                  "relations_scope": "no_relations"},
    }
    root_doc = request_json(instance, "read", payload)
    records = _memory_state_records(root_doc)
    root = records.get("stream")
    if not root:
        raise RuntimeError("xmemory response has no root MemoryState object")
    encoded = str(root["snapshot_json"])
    provider_calls = 1
    if encoded.startswith("chunked:v1:"):
        try:
            count = int(encoded.removeprefix("chunked:v1:"))
        except ValueError as error:
            raise RuntimeError("invalid chunked MemoryState root") from error
        if count < 1 or count > 100:
            raise RuntimeError(f"invalid MemoryState chunk count: {count}")
        chunk_ids = [f"stream:{index:04d}" for index in range(1, count + 1)]
        chunk_doc = request_json(instance, "read", {
            "query": "Return every scoped MemoryState chunk exactly; do not omit empty chunks.",
            "mode": "xresponse", "skip_suggestion_capture": True,
            "scope": {"objects": [{"type": "MemoryState",
                                      "key": {"key": {"stream_id": stream_id}}}
                                     for stream_id in chunk_ids],
                      "relations_scope": "no_relations"},
        })
        provider_calls += 1
        chunks = _memory_state_records(chunk_doc)
        missing = [stream_id for stream_id in chunk_ids if stream_id not in chunks]
        if missing:
            raise RuntimeError(f"xmemory response omitted MemoryState chunks: {missing}")
        digest = root.get("snapshot_sha256")
        if any(chunks[stream_id].get("snapshot_sha256") != digest for stream_id in chunk_ids):
            raise RuntimeError("MemoryState chunk digest metadata mismatch")
        encoded = "".join(str(chunks[stream_id]["snapshot_json"]) for stream_id in chunk_ids)
        state = decode_state_snapshot(encoded)
    else:
        state = decode_state_snapshot(encoded)
    digest = root.get("snapshot_sha256")
    if not digest or state_sha256(state) != digest:
        raise RuntimeError("remote MemoryState snapshot digest mismatch")
    return (state, {"provider_calls": provider_calls}) if with_metrics else state


def facts_from_read_response(doc: dict, slices: list[str], top_k: int) -> list[dict]:
    item = unwrap(doc)
    result = item.get("reader_result", item)
    facts = []
    seen = set()
    for node in _walk_dicts(result):
        record = _merged_record(node)
        fact_id = record.get("fact_id")
        if not isinstance(fact_id, str) or fact_id in seen:
            continue
        prefix = fact_id.split(":")[1].split("-")[0] if ":" in fact_id else ""
        slice_name = PREFIX_SLICES.get(prefix)
        if slice_name not in slices:
            continue
        record["slice"] = slice_name
        record["object_type"] = SLICE_OBJECTS[slice_name]
        facts.append(record)
        seen.add(fact_id)
    return facts[:top_k]


def remote_facts(instance: str, task: str, slices: list[str], query: str,
                 top_k: int, fact_ids: list[str] | None = None) -> tuple[list[dict], dict]:
    fact_ids = (fact_ids or [])[:top_k]
    if not fact_ids:
        return [], {"provider_fact_ids": [], "provider_response_sha256": None,
                    "provider_response_chars": 0, "provider_calls": 0}
    object_types = [SLICE_OBJECTS[s] for s in slices]
    # Relevance is ranked deterministically from the remote MemoryState manifest before this
    # call. The scoped read is an exact materialization boundary; asking xmemory to rank again
    # can legitimately return zero objects even though every primary key exists.
    prompt = (f"Return every scoped stored technical fact object exactly. "
              f"Only object types {object_types}. Include every stored field, especially "
              "fact_id, status, statement, content, evidence and human_notes. "
              "Do not filter by relevance; the caller already ranked these primary keys.")
    scoped = []
    for fact_id in fact_ids:
        prefix = fact_id.split(":")[1].split("-")[0]
        scoped.append({"type": SLICE_OBJECTS[PREFIX_SLICES[prefix]],
                       "key": {"key": {"fact_id": fact_id}}})
    raw = request_json(instance, "read", {
        "query": prompt, "mode": "xresponse", "skip_suggestion_capture": True,
        "session_id": task,
        "scope": {"objects": scoped, "relations_scope": "no_relations"},
    })
    facts = facts_from_read_response(raw, slices, top_k)
    encoded = json.dumps(raw, sort_keys=True, ensure_ascii=False).encode()
    return facts, {"provider_fact_ids": [f["fact_id"] for f in facts],
                           "provider_response_sha256": hashlib.sha256(encoded).hexdigest(),
                           "provider_response_chars": len(encoded), "provider_calls": 1}


def common_fields() -> dict:
    return {
        "fact_id": {"type": "str", "required": True, "description": "Stable fact id"},
        "statement": {"type": "str", "required": True, "description": "Human label derived from typed fields"},
        "content": {"type": "str", "required": False, "description": "Auditable rendered fact content"},
        "evidence": {"type": "str", "required": True, "description": "JSON evidence refs"},
        "confidence": {"type": "str", "required": True, "enum": ["high", "medium", "low"]},
        "status": {"type": "str", "required": True, "enum": ["candidate", "active", "stale"]},
        "provenance": {"type": "str", "required": True, "enum": ["declared", "observed", "inferred"]},
        "auto_approved": {"type": "bool", "required": False, "default": False},
        "human_notes": {"type": "str", "required": False},
        "question": {"type": "str", "required": False},
        "source": {"type": "str", "required": True},
        "superseded_by": {"type": "str", "required": False},
        "status_reason": {"type": "str", "required": False},
    }


def schema() -> dict:
    domain = {
        "ApiContract": {
            "router_endpoint": "Router or endpoint", "method": "HTTP/registration method",
            "path": "URL path", "authentication_boundary": "Auth boundary",
            "response_behavior": "Response and error behavior"},
        "Invariant": {"rule": "Enforced rule", "protected_entity": "Protected entity",
                      "enforcement_mechanism": "Enforcement point", "failure_behavior": "Failure behavior",
                      "scope": "Tenant/domain scope"},
        "DataOwnership": {"model_table": "Model/table", "owning_repository": "Owning repository",
                          "group_scope": "Tenant scope", "read_write_behavior": "Access behavior",
                          "migration_requirement": "Migration rule"},
        "ConfigFlag": {"setting_name": "Setting/env name", "flag_type": "Value/type",
                       "flag_default": "Default", "read_location": "Runtime read location",
                       "validation_readiness": "Test/validation convention",
                       "documentation_evidence": "Documentation location"},
        "Gotcha": {"trigger": "Build/test trigger", "outcome": "Observed outcome",
                   "lesson": "Reusable lesson"},
    }
    objects = {
        "Task": {"description": "Coding or curator task", "fields": {
            "task_id": {"type": "str", "required": True},
            "title": {"type": "str", "required": True},
            "at": {"type": "str", "required": False},
            "used_facts": {"type": "str", "required": False},
            "produced_facts": {"type": "str", "required": False},
            "decisions": {"type": "str", "required": False}}, "primary_key": ["task_id"]},
        "MemoryState": {"description": "Canonical cloud-only clone manifest", "fields": {
            "stream_id": {"type": "str", "required": True},
            "state_version": {"type": "int", "required": True},
            "schema_version": {"type": "int", "required": True},
            "mode": {"type": "str", "required": True},
            "seed": {"type": "int", "required": True},
            "c0_sha256": {"type": "str", "required": True},
            "snapshot_json": {"type": "str", "required": True},
            "snapshot_sha256": {"type": "str", "required": True},
            "parent_instance_id": {"type": "str", "required": False},
            "session_id": {"type": "str", "required": False},
            "session_kind": {"type": "str", "required": False},
            },
            "primary_key": ["stream_id"]},
    }
    for object_type, fields in domain.items():
        typed = {name: {"type": "str", "required": False, "description": desc}
                 for name, desc in fields.items()}
        objects[object_type] = {"description": f"Typed {object_type} technical fact",
                                "fields": {**common_fields(), **typed},
                                "primary_key": ["fact_id"]}
    relations = {}
    for object_type in domain:
        snake = {"ApiContract": "api_contract", "Invariant": "invariant",
                 "DataOwnership": "data_ownership", "ConfigFlag": "config_flag",
                 "Gotcha": "gotcha"}[object_type]
        for verb in ("used", "produced"):
            relations[f"task_{verb}_{snake}"] = {
                "description": f"Facts {verb} by a Task",
                "objects": {
                    "task": {"type": "Task", "on_delete": "cascade"},
                    snake: {"type": object_type, "on_delete": "nullify"},
                },
                "keys": {"unique_pair": ["task", snake]},
            }
    return {"xmd_version": "v1", "title": "Mealie eval memory C0",
            "description": "Typed technical facts cloned independently for one eval stream",
            "objects": objects, "relations": relations}


def seed_payload(snapshot: Path) -> dict:
    state = new_state(snapshot, "template", 0, "xmemory")
    state["fallback"] = False
    state["lineage"] = {"parent_instance_id": "", "session_id": "c0-template",
                        "session_kind": "template"}
    return state_seed_payload(state)


def create_instance(name: str, description: str, xmemcli: str,
                    parent_schema_instance: str | None = None) -> str:
    with tempfile.TemporaryDirectory(prefix="kata-xmemory-schema-") as directory:
        if parent_schema_instance:
            path = Path(directory) / "schema.yaml"
            get_cmd = [xmemcli, "--json", "schema", "get", parent_schema_instance,
                       "-o", str(path)]
            result = subprocess.run(get_cmd, text=True, capture_output=True, timeout=240)
            if result.returncode or not path.exists():
                raise RuntimeError(result.stderr[-1600:] or result.stdout[-1600:])
            schema_type = "yaml"
        else:
            path = Path(directory) / "schema.json"
            path.write_text(json.dumps(schema(), indent=2, ensure_ascii=False), encoding="utf-8")
            schema_type = "json"
        cmd = [xmemcli, "--json", "instance", "create", "--name", name,
               "--description", description, "--schema-file", str(path),
               "--schema-type", schema_type]
        result = subprocess.run(cmd, text=True, capture_output=True, timeout=240)
        if result.returncode:
            raise RuntimeError(result.stderr[-1600:] or result.stdout[-1600:])
        doc = json.loads(result.stdout)
        instance_id = doc.get("instance_id") or (doc.get("data") or {}).get("instance_id")
        if not instance_id:
            raise RuntimeError(f"cannot parse xmemory instance id: {result.stdout[-1200:]}")
        return instance_id


def provision(name: str, snapshot: Path, xmemcli: str) -> str:
    instance_id = create_instance(name, "Read-only kata eval C0 template", xmemcli)
    post(instance_id, seed_payload(snapshot))
    return instance_id


def clone(parent: str, name: str, mode: str, seed: int, session_id: str,
          xmemcli: str) -> tuple[str, dict]:
    started = time.monotonic()
    state, parent_read = remote_state(parent, with_metrics=True)
    state["backend"] = "xmemory"
    state["fallback"] = False
    state["mode"] = mode
    state["seed"] = seed
    state["lineage"] = {"parent_instance_id": parent, "session_id": session_id,
                        "session_kind": "curator" if session_id.startswith("evolve") else "coding"}
    child = create_instance(name, f"Cloud-only child of {parent} for {session_id}", xmemcli,
                            parent_schema_instance=parent)
    post(child, state_seed_payload(state))
    verified, child_read = remote_state(child, with_metrics=True)
    if state_sha256(verified) != state_sha256(state):
        raise RuntimeError("cloud-to-cloud clone verification failed")
    return child, {"parent_instance_id": parent, "state_sha256": state_sha256(state),
                   "state_version": state["state_version"],
                   "provider_calls": 3 + parent_read["provider_calls"] + child_read["provider_calls"],
                   "wall_sec": round(time.monotonic() - started, 4)}


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    write = sub.add_parser("write")
    write.add_argument("--instance", required=True)
    create = sub.add_parser("provision")
    create.add_argument("--name", required=True)
    create.add_argument("--snapshot", default="dataset/facts/snapshot-c0.md")
    create.add_argument("--xmemcli", default="xmemcli")
    clone_cmd = sub.add_parser("clone")
    clone_cmd.add_argument("--parent", required=True)
    clone_cmd.add_argument("--name", required=True)
    clone_cmd.add_argument("--mode", required=True)
    clone_cmd.add_argument("--seed", type=int, required=True)
    clone_cmd.add_argument("--session-id", required=True)
    clone_cmd.add_argument("--xmemcli", default="xmemcli")
    delete_cmd = sub.add_parser("delete")
    delete_cmd.add_argument("--instance", required=True)
    state_cmd = sub.add_parser("state")
    state_cmd.add_argument("--instance", required=True)
    read_cmd = sub.add_parser("read-facts")
    read_cmd.add_argument("--instance", required=True)
    read_cmd.add_argument("--task", required=True)
    read_cmd.add_argument("--top-k", type=int, default=20)
    read_cmd.add_argument("--slices", nargs="+", required=True)
    args = parser.parse_args()
    try:
        if args.command == "write":
            payload = json.load(sys.stdin)
            result = post(args.instance, payload)
            print(json.dumps({"ok": True, "errors": result.get("errors", []),
                              "provider_calls": 1}))
        elif args.command == "provision":
            instance_id = provision(args.name, (ROOT / args.snapshot).resolve(), args.xmemcli)
            print(json.dumps({"instance_id": instance_id}))
        elif args.command == "clone":
            instance_id, metrics = clone(args.parent, args.name, args.mode, args.seed,
                                         args.session_id, args.xmemcli)
            print(json.dumps({"instance_id": instance_id, **metrics}))
        elif args.command == "delete":
            result = delete_instance(args.instance)
            print(json.dumps({"ok": True, "instance_id": args.instance,
                              "already_deleted": bool(result.get("already_deleted")),
                              "provider_calls": 1}))
        elif args.command == "state":
            state, metrics = remote_state(args.instance, with_metrics=True)
            print(json.dumps({"state": state, **metrics}, ensure_ascii=False))
        elif args.command == "read-facts":
            request = json.load(sys.stdin)
            query = request.get("query", "")
            facts, metrics = remote_facts(args.instance, args.task, args.slices, query, args.top_k,
                                          request.get("fact_ids") or [])
            print(json.dumps({"facts": facts, "metrics": metrics}, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
