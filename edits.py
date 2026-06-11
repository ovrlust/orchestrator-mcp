"""Surgical string edits — the apply core shared by run_agent and delegate_run.

Whole-file overwrite is the defining weakness of cheap-model coding harnesses:
the model regenerates the whole file to change three lines, drifts elsewhere, and
burns output tokens. Instead, a worker returns small {old, new} edits and this
module applies them with the same safety checks Claude Code uses: the target must
exist exactly once (unless replace_all), and a no-op edit is an error.
"""

import re
import json


class EditError(Exception):
    """A surgical edit could not be applied (not found / ambiguous / malformed)."""


def apply_edits(content: str, edits: list) -> str:
    """Apply a sequence of edits to `content`, returning the new content.

    Each edit: {old|old_string, new|new_string (default ""), replace_all? (bool)}.
    Edits apply in order; each sees the result of the previous one. Raises
    EditError with a precise reason on any failure (nothing is partially written
    by this function — the caller decides what to persist).
    """
    if not isinstance(edits, list):
        raise EditError("edits must be a list")
    for i, e in enumerate(edits):
        if not isinstance(e, dict):
            raise EditError(f"edit {i}: must be an object, got {type(e).__name__}")
        old = e.get("old", e.get("old_string"))
        new = e.get("new", e.get("new_string", ""))
        replace_all = bool(e.get("replace_all", False))
        if old is None:
            raise EditError(f"edit {i}: missing 'old'")
        if not isinstance(old, str) or not isinstance(new, str):
            raise EditError(f"edit {i}: 'old' and 'new' must be strings")
        if old == new:
            raise EditError(f"edit {i}: 'old' equals 'new' (no-op)")
        count = content.count(old)
        if count == 0:
            raise EditError(f"edit {i}: 'old' not found: {old[:80]!r}")
        if count > 1 and not replace_all:
            raise EditError(
                f"edit {i}: 'old' appears {count} times; add surrounding context to "
                f"make it unique, or set replace_all: {old[:80]!r}"
            )
        content = (
            content.replace(old, new) if replace_all else content.replace(old, new, 1)
        )
    return content


def apply_one(content: str, old: str, new: str, replace_all: bool = False) -> str:
    """Convenience wrapper for a single edit."""
    return apply_edits(content, [{"old": old, "new": new, "replace_all": replace_all}])


def parse_edit_payload(text: str) -> list:
    """Parse a worker's edit output (a JSON array of edits) into a list.

    Tolerates a ```json ...``` fence and a single-object payload. Raises EditError
    on anything that isn't valid edit JSON.
    """
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s).strip()
    try:
        ops = json.loads(s)
    except Exception as e:  # noqa: BLE001
        raise EditError(f"edits are not valid JSON: {e}")
    if isinstance(ops, dict):
        ops = [ops]
    if not isinstance(ops, list):
        raise EditError("edits must be a JSON array of {old, new} objects")
    return ops
