"""Filesystem + shell safety rails shared by every layer.

These are best-effort rails against accidents and obviously destructive
commands, NOT a security boundary — a hostile prompt running with a permissive
allowlist can still do damage. Keep allowlists tight.
"""

import re
import shlex
import pathlib

# Hard denylist: blocked in run_command, shell validators, and shell hooks
# regardless of any allowlist.
DENY = re.compile(
    r"(rm\s+-[rf]|rm\s+--(?:recursive|force)|-exec\s+rm|find\b.*-delete|"
    r"sudo|shutdown|reboot|halt|mkfs|dd\s+if=|:\(\)\s*\{|"
    r"curl\s|wget\s|chmod\s+777|chown\s|>\s*/dev|/etc/|ssh|killall|kill\s+-9)"
)

# Shell chaining/metacharacters: with shell=True a prefix allowlist is useless
# if the command can chain (`echo x; rm ...`), so these are rejected outright
# whenever an allowlist is in force.
SHELL_META = re.compile(r"[;&|<>`\n]|\$\(")


def check_command(cmd: str, allow_cmds: list) -> str | None:
    """Gate one shell command against the denylist + allowlist.

    Returns a denial reason string, or None if the command may run. The
    allowlist entries are command prefixes ("python", "git status") matched on
    a token boundary, so allow=["echo"] passes "echo hi" but not "echofoo".
    """
    if DENY.search(cmd):
        return f"dangerous pattern: {cmd}"
    if not allow_cmds:
        return f"not in allow_commands []: {cmd}"
    if SHELL_META.search(cmd):
        return f"shell chaining/redirection not allowed: {cmd}"
    try:
        shlex.split(cmd)
    except ValueError as e:
        return f"unparseable command ({e}): {cmd}"
    if not any(cmd == a or cmd.startswith(a + " ") for a in allow_cmds):
        return f"not in allow_commands {allow_cmds}: {cmd}"
    return None


def safe_path(work, p) -> pathlib.Path:
    """Resolve `p` under work_dir, raising if it escapes the sandbox."""
    base = pathlib.Path(work).resolve()
    target = (base / p).resolve()
    if target != base and base not in target.parents:
        raise ValueError(f"path escapes work_dir: {p}")
    return target
