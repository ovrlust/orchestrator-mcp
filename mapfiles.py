"""Bulk transform: apply one instruction to every file matching a glob, fanned
out across cheap workers. The orchestrator's strongest use case (high-volume,
fully-specified grind) as a single call instead of hand-built orders.

Each matched file becomes a delegate order — read -> worker -> apply -> validate
-> retry -> rollback-on-fail — so the existing safety + concurrency + ledger
machinery is reused verbatim. The file contents are read server-side into the
worker prompts; only the compact report comes back, so the orchestrator's
context is never filled with the files themselves.
"""

import os
import pathlib

from sandbox import safe_path
from workers import MAX_FILE
from delegate import run_delegate

IGNORE_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".delegate",
    "dist",
    "build",
}


def match_files(work: str, pattern: str, exclude: str = "") -> list:
    """Relative paths of files under work matching `pattern` (glob), minus
    IGNORE_DIRS and an optional `exclude` glob. Sorted, deterministic."""
    root = pathlib.Path(work)
    out = []
    for p in sorted(root.glob(pattern)):
        if not p.is_file():
            continue
        if any(part in IGNORE_DIRS for part in p.relative_to(root).parts):
            continue
        rel = str(p.relative_to(root))
        if exclude and p.match(exclude):
            continue
        out.append(rel)
    return out


async def run_map_files(
    work_dir: str,
    pattern: str,
    instruction: str,
    validate: dict = None,
    model: str = "",
    edit: bool = False,
    exclude: str = "",
    allow_commands: list = None,
    max_retries: int = 1,
    max_files: int = 200,
    reset: bool = False,
    fallback: str = "",
    dry_run: bool = False,
) -> dict:
    """Apply `instruction` to every file matching `pattern`. See server.map_files."""
    if not instruction.strip():
        return {"error": "instruction is required"}
    work = str(pathlib.Path(work_dir).expanduser().resolve())
    if not os.path.isdir(work):
        return {"error": f"work_dir not found: {work_dir}"}

    rels = match_files(work, pattern, exclude)
    if not rels:
        return {"error": f"no files matched {pattern!r} under {work}", "matched": 0}
    truncated = len(rels) > max_files
    rels = rels[:max_files]
    if dry_run:
        return {
            "dry_run": True,
            "matched": len(rels),
            "truncated": truncated,
            "files": rels,
        }

    orders = []
    for rel in rels:
        try:
            content = safe_path(work, rel).read_text(encoding="utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001 - unreadable file becomes a clean skip
            content = None
            read_err = str(e)
        if content is None:
            # represent as an order that will fail its precondition cleanly
            orders.append({"id": rel, "prompt": f"unreadable: {read_err}", "output_path": rel})
            continue
        prompt = (
            instruction
            + "\n\nReturn "
            + (
                "ONLY a JSON array of {old, new} edits to apply"
                if edit
                else "the COMPLETE updated file content, nothing else"
            )
            + ".\n\n--- CURRENT CONTENT OF "
            + rel
            + " ---\n"
            + content[:MAX_FILE]
        )
        orders.append(
            {
                "id": rel,
                "prompt": prompt,
                "output_path": rel,
                "edit": edit,
                "validate": validate,
                "max_retries": max_retries,
            }
        )

    report = await run_delegate(
        orders, work, allow_commands, model, None, reset, fallback
    )
    if isinstance(report, dict) and "summary" in report:
        report["summary"]["matched"] = len(rels)
        report["summary"]["truncated"] = truncated
    return report
