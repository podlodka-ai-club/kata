#!/usr/bin/env python3
"""Provision isolated xmemory streams and send structured-mutation batches.

Credentials are read by xmemcli from `.xmemrc.json` (or XMEM_* env vars).  The
helper never prints keys.  Provisioning creates one typed instance per mode/seed
from the same frozen C0 and writes only pre-C0 facts.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from memory_backend import SLICE_OBJECTS, parse_snapshot


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


def post(instance: str, payload: dict) -> dict:
    api_url, api_key = credentials()
    request = urllib.request.Request(
        f"{api_url.rstrip('/')}/instances/{instance}/write",
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
        "created_at": {"type": "str", "required": False},
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
            "at": {"type": "str", "required": False}}, "primary_key": ["task_id"]},
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
                "description": f"Facts {verb} by a Task", "endpoints": [
                    {"name": "task", "type": "Task"}, {"name": snake, "type": object_type}]}
    return {"xmd_version": "v1", "title": "Mealie eval memory C0",
            "description": "Typed technical facts cloned independently for one eval stream",
            "objects": objects, "relations": relations}


def seed_payload(snapshot: Path) -> dict:
    mutations = []
    for fact in parse_snapshot(snapshot):
        values = {key: value for key, value in fact.items()
                  if key not in {"fact_id", "slice", "object_type", "title"}}
        values["evidence"] = json.dumps(values.get("evidence", []), ensure_ascii=False)
        values["human_notes"] = json.dumps(values.get("human_notes", []), ensure_ascii=False)
        mutations.append({"object_mutation": {"object_type": fact["object_type"], "create": {
            "key": {"fact_id": fact["fact_id"]}, "values": values}}})
    return {"structured_mutations": mutations}


def provision(name: str, snapshot: Path, xmemcli: str) -> str:
    with tempfile.TemporaryDirectory(prefix="kata-xmemory-schema-") as directory:
        path = Path(directory) / "schema.json"
        path.write_text(json.dumps(schema(), indent=2, ensure_ascii=False), encoding="utf-8")
        cmd = [xmemcli, "--json", "instance", "create", "--name", name,
               "--description", "Isolated kata eval stream cloned from frozen C0",
               "--schema-file", str(path), "--schema-type", "json"]
        result = subprocess.run(cmd, text=True, capture_output=True, timeout=240)
        if result.returncode:
            raise RuntimeError(result.stderr[-1600:] or result.stdout[-1600:])
        doc = json.loads(result.stdout)
        instance_id = doc.get("instance_id") or (doc.get("data") or {}).get("instance_id")
        if not instance_id:
            raise RuntimeError(f"cannot parse xmemory instance id: {result.stdout[-1200:]}")
    post(instance_id, seed_payload(snapshot))
    return instance_id


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    write = sub.add_parser("write")
    write.add_argument("--instance", required=True)
    create = sub.add_parser("provision")
    create.add_argument("--name", required=True)
    create.add_argument("--snapshot", default="dataset/facts/snapshot-c0.md")
    create.add_argument("--xmemcli", default="xmemcli")
    args = parser.parse_args()
    try:
        if args.command == "write":
            payload = json.load(sys.stdin)
            result = post(args.instance, payload)
            print(json.dumps({"ok": True, "errors": result.get("errors", [])}))
        else:
            instance_id = provision(args.name, (ROOT / args.snapshot).resolve(), args.xmemcli)
            print(json.dumps({"instance_id": instance_id}))
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
