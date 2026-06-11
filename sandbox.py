"""Filesystem + shell safety rails shared by every layer."""

import re
import pathlib

# Hard denylist: blocked in run_command, shell validators, and shell hooks
# regardless of any allowlist.
DENY = re.compile(
    r"(rm\s+-[rf]|sudo|shutdown|reboot|halt|mkfs|dd\s+if=|:\(\)\s*\{|"
    r"curl\s|wget\s|chmod\s+777|chown\s|>\s*/dev|/etc/|ssh|killall|kill\s+-9)"
)


def safe_path(work, p) -> pathlib.Path:
    """Resolve `p` under work_dir, raising if it escapes the sandbox."""
    base = pathlib.Path(work).resolve()
    target = (base / p).resolve()
    if target != base and base not in target.parents:
        raise ValueError(f"path escapes work_dir: {p}")
    return target
