#!/usr/bin/env python3
"""SessionStart hook: auto-load a project's cached understanding into context.

On every session, refresh the project map (incremental — only changed files are
re-read, so it's cheap) and print a compact overview, which Claude Code injects
into the session. Effect: Claude starts already knowing the project structure and
never has to re-read unchanged files. Persists across sessions, so it doesn't
"forget".

Reads the session cwd from the hook's JSON stdin (falls back to $PWD). Skips
non-project dirs. Never errors the session — always exits 0.
"""

import os
import sys
import json
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))


def main() -> None:
    cwd = os.getcwd()
    try:
        raw = sys.stdin.read()
        if raw.strip():
            cwd = json.loads(raw).get("cwd") or cwd
    except Exception:
        pass

    try:
        import project

        if not project.is_project(cwd):
            return  # not a real project — stay quiet
        project.understand(cwd)  # incremental refresh (cheap)
        text = project.summary_text(cwd)
        if text:
            print(text)
    except Exception:
        pass  # a hook must never break the session


if __name__ == "__main__":
    main()
    sys.exit(0)
