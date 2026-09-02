#!/usr/bin/env python3
"""Fail-closed write guard for Claude Edit/Write tools in eval workspaces."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _resolved_candidate(root: Path, raw: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        path = root / path
    # strict=False resolves existing symlink ancestors and normalizes '..'.
    return path.resolve(strict=False)


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def main() -> int:
    try:
        event = json.load(sys.stdin)
        root_raw = os.environ["KATA_WORKSPACE_ROOT"]
        root = Path(root_raw).resolve(strict=True)
        tool = event.get("tool_name") or event.get("tool")
        tool_input = event.get("tool_input") or {}
        raw = tool_input.get("file_path") or tool_input.get("path")
        if tool not in {"Edit", "Write"} or not isinstance(raw, str) or not raw.strip():
            raise ValueError("missing Edit/Write file path")
        target = _resolved_candidate(root, raw)
        protected = [root / "tests", root / ".claude", root / ".kata-hooks",
                     root / ".kata-bin", root / ".git"]
        if not _inside(target, root):
            raise ValueError("target escapes the disposable workspace")
        if any(_inside(target, path.resolve(strict=False)) for path in protected):
            raise ValueError("target is runner-owned or a protected base-test path")
        # New tests in agent_tests and source edits elsewhere are both allowed.
        return 0
    except Exception as exc:
        print(f"kata write guard denied tool call: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
