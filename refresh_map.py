#!/usr/bin/env python3
"""PostToolUse(Edit|Write) hook: keep the project map fresh after every edit.

Reads the hook JSON on stdin, finds the edited file's project root (walks up to the
nearest PROJECT_MARKER), and runs an incremental `understand` on it. The mtime/size
fast-path means only the file(s) that actually changed are re-read — so this is
near-instant even on big repos. Silent, never blocks, never errors the tool.

This is what makes our map beat a one-shot indexer: it self-updates on every edit,
not just at session start.
"""

import os
import sys
import json
import pathlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _find_root(start: pathlib.Path):
    import project

    cur = start if start.is_dir() else start.parent
    for d in [cur, *cur.parents]:
        if any((d / m).exists() for m in project.PROJECT_MARKERS):
            return str(d)
    return None


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return
    fp = (data.get("tool_input") or {}).get("file_path") or ""
    if not fp:
        return
    try:
        import project

        root = _find_root(pathlib.Path(fp).expanduser().resolve())
        if root and project.is_project(root):
            project.understand(root)  # incremental; fast-path skips unchanged files
    except Exception:
        pass  # a map refresh must never break the edit


if __name__ == "__main__":
    main()
    sys.exit(0)
