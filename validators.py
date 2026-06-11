"""Deterministic gates for worker output. No model judgment lives here."""

import re
import json
import subprocess

from sandbox import check_command

REFUSALS = (
    "i cannot",
    "i can't",
    "i am unable",
    "i'm unable",
    "as an ai",
    "i'm sorry",
    "i am sorry",
)


def validate(spec: dict, text: str, abspath, work: str, allow_cmds: list) -> dict:
    """Gate one worker result. Returns {ok: bool, error: str}.

    spec.type one of: nonempty | regex | json | shell.
    Pure except for `shell` (runs a subprocess) and `json` schema (optional dep).
    """
    if not spec:
        return {"ok": True, "error": ""}
    t = spec.get("type")

    if t == "nonempty":
        s = text.strip()
        if not s:
            return {"ok": False, "error": "output is empty"}
        mn, mx = spec.get("min_len"), spec.get("max_len")
        if mn and len(s) < mn:
            return {"ok": False, "error": f"too short ({len(s)} < {mn})"}
        if mx and len(s) > mx:
            return {"ok": False, "error": f"too long ({len(s)} > {mx})"}
        low = s.lower()
        if any(r in low for r in REFUSALS):
            return {"ok": False, "error": "output looks like a refusal"}
        return {"ok": True, "error": ""}

    if t == "regex":
        pat = spec.get("pattern", "")
        must_not = bool(spec.get("must_not", False))
        m = re.search(pat, text, re.S)
        if must_not and m:
            return {"ok": False, "error": f"matched forbidden pattern: {pat}"}
        if not must_not and not m:
            return {"ok": False, "error": f"did not match required pattern: {pat}"}
        return {"ok": True, "error": ""}

    if t == "json":
        try:
            obj = json.loads(text)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"not valid JSON: {e}"}
        schema = spec.get("schema")
        if schema:
            try:
                import jsonschema  # optional

                jsonschema.validate(obj, schema)
            except ImportError:
                return {
                    "ok": False,
                    "error": "jsonschema not installed; cannot check schema",
                }
            except Exception as e:  # noqa: BLE001 - validation error
                return {"ok": False, "error": f"schema mismatch: {e}"}
        return {"ok": True, "error": ""}

    if t == "shell":
        cmd = spec.get("cmd", "").strip()
        if not cmd:
            return {"ok": False, "error": "shell validator has no cmd"}
        denied = check_command(cmd, allow_cmds)
        if denied:
            return {"ok": False, "error": f"validator cmd denied ({denied})"}
        try:
            r = subprocess.run(
                cmd, shell=True, cwd=work, capture_output=True, text=True, timeout=300
            )
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"validator failed to run: {e}"}
        if r.returncode != 0:
            return {
                "ok": False,
                "error": f"exit={r.returncode}\n{(r.stdout + r.stderr)[-2000:]}",
            }
        return {"ok": True, "error": ""}

    return {"ok": False, "error": f"unknown validator type: {t}"}
