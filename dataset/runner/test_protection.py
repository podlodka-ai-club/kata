#!/usr/bin/env python3
"""Physical protection and pristine proofs for base-commit tests.

The coding agent may add tests only below the sibling ``agent_tests`` tree. Every test
path that existed in the materialized base commit is made read-only and, on
macOS, user-immutable for the duration of the coding session. The immutable
directory flag prevents rename/delete or additions anywhere under the protected tree.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any


ADDED_TESTS_DIR = Path("agent_tests")


class TestProtectionError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_manifest(workspace: Path) -> dict[str, Any]:
    """Record every base test path before the writable addition directory exists."""
    tests = workspace / "tests"
    entries: list[dict[str, Any]] = []
    index = _git_index(workspace)
    if tests.exists():
        for path in sorted([tests, *tests.rglob("*")]):
            relative = path.relative_to(workspace).as_posix()
            info = path.lstat()
            kind = "symlink" if path.is_symlink() else "dir" if path.is_dir() else "file"
            entry: dict[str, Any] = {
                "path": relative,
                "kind": kind,
                "mode": stat.S_IMODE(info.st_mode),
            }
            if relative in index:
                entry["git_mode"], entry["git_blob"] = index[relative]
            if kind == "file":
                entry["sha256"] = _sha256(path)
            elif kind == "symlink":
                entry["target"] = os.readlink(path)
            entries.append(entry)
    canonical = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
    return {
        "format_version": 1,
        "entries": entries,
        "manifest_sha256": hashlib.sha256(canonical).hexdigest(),
        "added_tests_dir": ADDED_TESTS_DIR.as_posix(),
        "tracked_paths": sorted(index),
    }


def _git_index(workspace: Path) -> dict[str, tuple[str, str]]:
    result = subprocess.run(["git", "ls-files", "--stage", "-z", "--", "tests"],
                            cwd=workspace, capture_output=True)
    if result.returncode:
        raise TestProtectionError(result.stderr.decode(errors="replace")[-1200:])
    records = {}
    for raw in result.stdout.split(b"\0"):
        if not raw:
            continue
        metadata, path = raw.split(b"\t", 1)
        mode, blob, _stage = metadata.decode().split()
        records[path.decode(errors="surrogateescape")] = (mode, blob)
    return records


def _base_paths(workspace: Path, manifest: dict[str, Any]) -> list[Path]:
    return [workspace / entry["path"] for entry in manifest.get("entries", [])]


def _chflags(paths: list[Path], flag: str) -> None:
    chflags = shutil.which("chflags")
    if not chflags:
        raise TestProtectionError(
            "physical test protection requires chflags; refusing to spend coding tokens")
    # Avoid command-line size limits while keeping every target explicit.
    for start in range(0, len(paths), 200):
        result = subprocess.run([chflags, flag, *map(str, paths[start:start + 200])],
                                text=True, capture_output=True)
        if result.returncode:
            raise TestProtectionError(result.stderr[-1200:] or "chflags failed")


def protect(workspace: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    """Lock the complete base test tree while leaving a sibling addition tree writable."""
    allowed = workspace / ADDED_TESTS_DIR
    allowed.mkdir(parents=True, exist_ok=True)
    paths = _base_paths(workspace, manifest)
    files = [workspace / entry["path"] for entry in manifest["entries"]
             if entry["kind"] in {"file", "symlink"}]
    directories = [workspace / entry["path"] for entry in manifest["entries"]
                   if entry["kind"] == "dir"]
    for path in files:
        if not path.is_symlink():
            path.chmod(stat.S_IMODE(path.stat().st_mode) & ~0o222)
    # Existing directories become structurally read-only. The sibling addition
    # directory remains writable and can accept arbitrary new tests.
    for path in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        path.chmod(stat.S_IMODE(path.stat().st_mode) & ~0o222)
    allowed.chmod(0o755)
    try:
        _chflags([path for path in paths if path.exists() or path.is_symlink()], "uchg")
    except Exception:
        # Restore modes if the physical lock could not be completed.
        restore_modes(workspace, manifest, clear_flags=False)
        raise
    held = verify_protected(workspace, manifest)
    return {
        "mechanism": "readonly-modes+darwin-uchg",
        "protected_entries": len(paths),
        "base_manifest_sha256": manifest["manifest_sha256"],
        "added_tests_dir": ADDED_TESTS_DIR.as_posix(),
        "lock_held": held,
    }


def restore_modes(workspace: Path, manifest: dict[str, Any], clear_flags: bool = True) -> None:
    paths = [path for path in _base_paths(workspace, manifest)
             if path.exists() or path.is_symlink()]
    if clear_flags and paths:
        _chflags(paths, "nouchg")
    # Restore children before parents so traversal never depends on a read-only parent.
    for entry in sorted(manifest.get("entries", []),
                        key=lambda item: len(Path(item["path"]).parts), reverse=True):
        path = workspace / entry["path"]
        if path.exists() and not path.is_symlink():
            path.chmod(int(entry["mode"]))


def verify(workspace: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    """Prove hash/type/mode equality for all base test entries before scoring."""
    mismatches = []
    for entry in manifest.get("entries", []):
        path = workspace / entry["path"]
        if not (path.exists() or path.is_symlink()):
            mismatches.append({"path": entry["path"], "reason": "missing"})
            continue
        actual_kind = "symlink" if path.is_symlink() else "dir" if path.is_dir() else "file"
        if actual_kind != entry["kind"]:
            mismatches.append({"path": entry["path"], "reason": "kind_changed",
                               "expected": entry["kind"], "actual": actual_kind})
            continue
        actual_mode = stat.S_IMODE(path.lstat().st_mode)
        if actual_mode != int(entry["mode"]):
            mismatches.append({"path": entry["path"], "reason": "mode_changed",
                               "expected": int(entry["mode"]), "actual": actual_mode})
        if actual_kind == "file":
            actual_hash = _sha256(path)
            if actual_hash != entry["sha256"]:
                mismatches.append({"path": entry["path"], "reason": "content_changed",
                                   "expected": entry["sha256"], "actual": actual_hash})
        elif actual_kind == "symlink" and os.readlink(path) != entry["target"]:
            mismatches.append({"path": entry["path"], "reason": "symlink_changed"})
    current_index = _git_index(workspace)
    expected_index = {entry["path"]: (entry["git_mode"], entry["git_blob"])
                      for entry in manifest.get("entries", []) if "git_mode" in entry}
    if current_index != expected_index:
        mismatches.append({"path": "tests", "reason": "git_index_changed",
                           "expected_count": len(expected_index),
                           "actual_count": len(current_index)})
    result = {
        "ok": not mismatches,
        "checked_entries": len(manifest.get("entries", [])),
        "base_manifest_sha256": manifest.get("manifest_sha256"),
        "hash_mode_equal": not mismatches,
        "mismatches": mismatches,
    }
    if mismatches:
        raise TestProtectionError(f"base tests changed despite physical protection: {mismatches[:5]}")
    return result


def verify_protected(workspace: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    """Verify session-time protected modes, hashes and immutable flags before unlocking."""
    mismatches = []
    immutable_flag = getattr(stat, "UF_IMMUTABLE", 0x00000002)
    for entry in manifest.get("entries", []):
        path = workspace / entry["path"]
        if not (path.exists() or path.is_symlink()):
            mismatches.append({"path": entry["path"], "reason": "missing_while_locked"})
            continue
        info = path.lstat()
        actual_kind = "symlink" if path.is_symlink() else "dir" if path.is_dir() else "file"
        if actual_kind != entry["kind"]:
            mismatches.append({"path": entry["path"], "reason": "kind_changed_while_locked"})
            continue
        expected_mode = int(entry["mode"]) if actual_kind == "symlink" else int(entry["mode"]) & ~0o222
        if stat.S_IMODE(info.st_mode) != expected_mode:
            mismatches.append({"path": entry["path"], "reason": "protected_mode_changed",
                               "expected": expected_mode,
                               "actual": stat.S_IMODE(info.st_mode)})
        if not (getattr(info, "st_flags", 0) & immutable_flag):
            mismatches.append({"path": entry["path"], "reason": "immutable_flag_cleared"})
        if actual_kind == "file" and _sha256(path) != entry["sha256"]:
            mismatches.append({"path": entry["path"], "reason": "content_changed_while_locked"})
        if actual_kind == "symlink" and os.readlink(path) != entry["target"]:
            mismatches.append({"path": entry["path"], "reason": "symlink_changed_while_locked"})
    expected_index = {entry["path"]: (entry["git_mode"], entry["git_blob"])
                      for entry in manifest.get("entries", []) if "git_mode" in entry}
    if _git_index(workspace) != expected_index:
        mismatches.append({"path": "tests", "reason": "git_index_changed_while_locked"})
    result = {"ok": not mismatches, "checked_entries": len(manifest.get("entries", [])),
              "immutable_flags_held": not any(
                  item["reason"] == "immutable_flag_cleared" for item in mismatches),
              "protected_hash_mode_equal": not mismatches, "mismatches": mismatches}
    if mismatches:
        raise TestProtectionError(f"protected tests changed during coding: {mismatches[:5]}")
    return result


def adversarial_preflight(workspace: Path) -> dict[str, Any]:
    """Exercise modification, deletion, rename and allowed-addition boundaries for free."""
    manifest = build_manifest(workspace)
    if not manifest["entries"]:
        raise TestProtectionError("test protection preflight needs a non-empty tests tree")
    regular = next((workspace / entry["path"] for entry in manifest["entries"]
                    if entry["kind"] == "file"), None)
    if regular is None:
        raise TestProtectionError("test protection preflight needs a regular test file")
    protection = protect(workspace, manifest)
    attempts: dict[str, bool] = {}
    addition = workspace / ADDED_TESTS_DIR / "test_agent_addition.py"
    try:
        try:
            regular.write_bytes(b"adversarial overwrite")
            attempts["modify_blocked"] = False
        except OSError:
            attempts["modify_blocked"] = True
        try:
            regular.unlink()
            attempts["delete_blocked"] = False
        except OSError:
            attempts["delete_blocked"] = True
        try:
            regular.chmod(0o644)
            attempts["chmod_blocked"] = False
        except OSError:
            attempts["chmod_blocked"] = True
        try:
            regular.rename(regular.with_name(regular.name + ".renamed"))
            attempts["file_rename_blocked"] = False
        except OSError:
            attempts["file_rename_blocked"] = True
        try:
            (workspace / "tests" / "test_forbidden_addition.py").write_text("x = 1\n")
            attempts["addition_under_tests_blocked"] = False
        except OSError:
            attempts["addition_under_tests_blocked"] = True
        try:
            (workspace / "tests").rename(workspace / "tests-renamed")
            attempts["tree_rename_blocked"] = False
        except OSError:
            attempts["tree_rename_blocked"] = True
        try:
            addition.write_text("def test_agent_addition():\n    assert True\n", encoding="utf-8")
            attempts["addition_allowed"] = addition.exists()
        except OSError:
            attempts["addition_allowed"] = False
        sandbox_exec = shutil.which("sandbox-exec")
        if not sandbox_exec:
            raise TestProtectionError("sandbox-exec missing; cannot verify the arbitrary-Python boundary")
        profile_path = str((workspace / "tests").resolve()).replace("\\", "\\\\").replace('"', '\\"')
        profile = f'(version 1)(allow default)(deny file-write* (subpath "{profile_path}"))'
        attack = (
            "import json,os,pathlib,sys; p=pathlib.Path(sys.argv[1]); root=pathlib.Path(sys.argv[2]); out={}; "
            "ops={'write':lambda:p.write_text('bad'),'unlink':lambda:p.unlink(),"
            "'chmod':lambda:p.chmod(0o644),'clear_flags':lambda:os.chflags(p,0),"
            "'rename_tree':lambda:(root/'tests').rename(root/'tests-x'),"
            "'add_under_tests':lambda:(root/'tests'/'forbidden.py').write_text('bad')}; "
            "exec(\"for name,fn in ops.items():\\n try: fn(); out[name]=False\\n except OSError: out[name]=True\"); "
            "print(json.dumps(out)); sys.exit(0 if all(out.values()) else 9)"
        )
        sandboxed = subprocess.run([sandbox_exec, "-p", profile, sys.executable, "-c", attack,
                                    str(regular), str(workspace)], text=True, capture_output=True)
        if sandboxed.returncode:
            raise TestProtectionError(
                "Seatbelt adversarial boundary unavailable or bypassed: "
                + (sandboxed.stderr[-800:] or sandboxed.stdout[-800:]))
        attempts["seatbelt_arbitrary_python_blocked"] = all(
            json.loads(sandboxed.stdout).values())
    finally:
        restore_modes(workspace, manifest)
        addition.unlink(missing_ok=True)
    pristine = verify(workspace, manifest)
    ok = all(attempts.values()) and pristine["ok"]
    return {"ok": ok, **protection, "attempts": attempts, "pristine": pristine}
