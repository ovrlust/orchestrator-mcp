"""Per-project understanding cache.

Read a project ONCE into a compact structural map (file tree + extracted symbols
per file), keyed by content hash, stored globally. On later runs only files whose
hash changed are re-scanned — unchanged files are never re-read. Claude reads the
MAP instead of raw files, so understanding a project is cheap, fast, and doesn't
need re-reading every session.

Deterministic + free (no LLM): the map is built from the filesystem + lightweight
symbol regexes. Optional cheap-worker summaries can layer on top later.
"""

import os
import re
import json
import time
import hashlib
import pathlib
import posixpath

HOME = pathlib.Path(os.environ.get("DELEGATE_HOME", os.path.expanduser("~/.delegate")))
PROJECTS_DIR = HOME / "projects"

# Cache schema version. Bump when the per-file record shape changes so an old
# cache is rebuilt once instead of silently missing new fields (e.g. imports).
VERSION = 3

IGNORE_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".delegate",
    "dist",
    "build",
    ".next",
    ".turbo",
    ".cache",
    "coverage",
    "target",
    ".idea",
    ".vscode",
    "vendor",
}
# Extensions worth mapping (code/config/docs). Others are listed but not symbol-scanned.
CODE_EXT = {
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".mjs",
    ".go",
    ".rs",
    ".java",
    ".rb",
    ".c",
    ".h",
    ".cpp",
    ".hpp",
    ".cs",
    ".php",
    ".swift",
    ".kt",
    ".scala",
    ".sh",
}
MAX_FILE = 500_000  # skip files bigger than this (likely data/generated)
MAX_FILES = 4000


def _abs(path: str) -> str:
    return str(pathlib.Path(path).expanduser().resolve())


def _cache_path(root: str) -> pathlib.Path:
    h = hashlib.sha1(root.encode()).hexdigest()[:16]
    return PROJECTS_DIR / f"{h}.json"


# ------------------------- symbol extraction (per language) -------------------------

_SYMBOL_PATTERNS = {
    ".py": [r"^\s*(?:async\s+)?def\s+(\w+)", r"^\s*class\s+(\w+)"],
    ".go": [r"^func\s+(?:\([^)]*\)\s*)?(\w+)", r"^type\s+(\w+)"],
    ".rs": [
        r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+(\w+)",
        r"^\s*(?:pub\s+)?(?:struct|enum|trait)\s+(\w+)",
    ],
}
_JS_PATTERNS = [
    r"^\s*export\s+(?:default\s+)?(?:async\s+)?function\s+(\w+)",
    r"^\s*(?:export\s+)?class\s+(\w+)",
    r"^\s*export\s+(?:const|let|var)\s+(\w+)",
    r"^\s*(?:async\s+)?function\s+(\w+)",
]
for _e in (".ts", ".tsx", ".js", ".jsx", ".mjs"):
    _SYMBOL_PATTERNS[_e] = _JS_PATTERNS


def extract_symbols(text: str, ext: str) -> list:
    pats = _SYMBOL_PATTERNS.get(ext)
    if not pats:
        return []
    out, seen = [], set()
    for line in text.splitlines():
        for pat in pats:
            m = re.match(pat, line)
            if m and m.group(1) not in seen:
                seen.add(m.group(1))
                out.append(m.group(1))
    return out[:60]


# ------------------------- import extraction (per language) -------------------------

_IMPORT_PATTERNS = {
    ".py": [r"^\s*from\s+([.\w]+)\s+import", r"^\s*import\s+([.\w]+)"],
}
_JS_IMPORTS = [
    r"""import\s+[^'"]*?from\s+['"]([^'"]+)['"]""",
    r"""require\(\s*['"]([^'"]+)['"]\s*\)""",
    r"""import\s+['"]([^'"]+)['"]""",
]
for _e in (".ts", ".tsx", ".js", ".jsx", ".mjs"):
    _IMPORT_PATTERNS[_e] = _JS_IMPORTS

_JS_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs")


def extract_imports(text: str, ext: str) -> list:
    pats = _IMPORT_PATTERNS.get(ext)
    if not pats:
        return []
    out, seen = [], set()
    for line in text.splitlines():
        for pat in pats:
            m = re.search(pat, line)
            if m and m.group(1) not in seen:
                seen.add(m.group(1))
                out.append(m.group(1))
    return out[:40]


def _resolve_import(raw: str, ext: str, rel: str, fileset: set):
    """Resolve one raw import to a repo file path, or None if external/unresolved."""
    d = posixpath.dirname(rel)
    if ext in _JS_EXTS:
        if not raw.startswith("."):  # bare import = external dep
            return None
        base = posixpath.normpath(posixpath.join(d, raw))
        for e in _JS_EXTS:
            if base + e in fileset:
                return base + e
            idx = posixpath.join(base, "index" + e)
            if idx in fileset:
                return idx
        return None
    if ext == ".py":
        if raw.startswith("."):  # relative: leading dots = levels up
            ups = len(raw) - len(raw.lstrip("."))
            base = d
            for _ in range(ups - 1):
                base = posixpath.dirname(base)
            parts = [p for p in raw[ups:].split(".") if p]
            cand = posixpath.join(base, *parts) if parts else base
        else:
            cand = "/".join(raw.split("."))
        for c in (cand + ".py", posixpath.join(cand, "__init__.py")):
            if c in fileset:
                return c
        return None
    return None


def build_graph(files: dict):
    """In-degree (how many files import each file) + edges. Pure, from cached imports."""
    fileset = set(files)
    indeg = {r: 0 for r in files}
    edges = {}
    for rel, e in files.items():
        ext = os.path.splitext(rel)[1].lower()
        for raw in e.get("imports", []):
            tgt = _resolve_import(raw, ext, rel, fileset)
            if tgt and tgt != rel:
                indeg[tgt] = indeg.get(tgt, 0) + 1
                edges.setdefault(rel, []).append(tgt)
    return indeg, edges


# Filenames that usually mark an executable entrypoint.
ENTRY_NAMES = {
    "main.py",
    "__main__.py",
    "manage.py",
    "app.py",
    "server.py",
    "cli.py",
    "wsgi.py",
    "asgi.py",
    "index.ts",
    "index.js",
    "index.tsx",
    "index.jsx",
    "main.ts",
    "main.js",
    "main.tsx",
    "main.go",
    "main.rs",
    "server.ts",
    "server.js",
}


def entrypoints(root: str, files: dict) -> list:
    """Best-effort entrypoints: known filenames + package.json main/module/bin."""
    eps = [rel for rel in files if os.path.basename(rel) in ENTRY_NAMES]
    pj = pathlib.Path(root) / "package.json"
    if pj.exists():
        try:
            j = json.loads(pj.read_text())
            for key in ("main", "module"):
                if isinstance(j.get(key), str):
                    eps.append(j[key])
            b = j.get("bin")
            if isinstance(b, str):
                eps.append(b)
            elif isinstance(b, dict):
                eps += [v for v in b.values() if isinstance(v, str)]
        except Exception:  # noqa: BLE001
            pass
    return list(dict.fromkeys(eps))  # dedup, keep order


# ------------------------- scan + incremental cache -------------------------


def _iter_files(root: str):
    base = pathlib.Path(root)
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames if d not in IGNORE_DIRS and not d.startswith(".")
        ]
        for fn in filenames:
            if fn.startswith("."):
                continue
            full = pathlib.Path(dirpath) / fn
            try:
                st = full.stat()
                if st.st_size > MAX_FILE:
                    continue
            except OSError:
                continue
            yield str(full.relative_to(base)), full, st
            count += 1
            if count >= MAX_FILES:
                return


def understand(path: str) -> dict:
    """Build/refresh the project map incrementally. Only re-scans changed files."""
    root = _abs(path)
    if not os.path.isdir(root):
        return {"error": f"not a directory: {root}"}
    prev = {}
    try:
        cached = json.loads(_cache_path(root).read_text())
        if cached.get("version") == VERSION:  # stale schema -> full rebuild once
            prev = cached.get("files", {})
    except Exception:  # noqa: BLE001
        pass

    files, reused, changed, added = {}, 0, 0, 0
    for rel, full, st in _iter_files(root):
        mtime, size = int(st.st_mtime), st.st_size
        old = prev.get(rel)
        # Fast-path: same mtime+size as cached -> unchanged, skip the read entirely.
        if old and old.get("mtime") == mtime and old.get("size") == size:
            files[rel] = old
            reused += 1
            continue
        try:
            data = full.read_bytes()
        except OSError:
            continue
        digest = hashlib.sha1(data).hexdigest()[:16]
        # Touched but content identical (hash match): reuse record, refresh stat only.
        if old and old.get("hash") == digest:
            old["mtime"], old["size"] = mtime, size
            files[rel] = old
            reused += 1
            continue
        ext = full.suffix.lower()
        text = data.decode("utf-8", "replace")
        is_code = ext in CODE_EXT
        rec = {
            "hash": digest,
            "mtime": mtime,
            "size": size,
            "lines": text.count("\n") + 1,
            "symbols": extract_symbols(text, ext) if is_code else [],
            "imports": extract_imports(text, ext) if is_code else [],
        }
        # content changed -> old summary is stale; summarize_project will refresh it.
        files[rel] = rec
        if old:
            changed += 1
        else:
            added += 1

    removed = [r for r in prev if r not in files]
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    _cache_path(root).write_text(
        json.dumps(
            {
                "version": VERSION,
                "root": root,
                "generated_at": round(time.time(), 3),
                "files": files,
            },
            indent=2,
        )
    )
    return {
        "root": root,
        "total_files": len(files),
        "added": added,
        "changed": changed,
        "removed": len(removed),
        "reused": reused,
    }


def context(path: str, max_files: int = 400) -> dict:
    """Return the cached compact map for Claude to read instead of the raw repo."""
    root = _abs(path)
    cp = _cache_path(root)
    if not cp.exists():
        return {
            "root": root,
            "cached": False,
            "hint": "no map yet — call understand_project first (cheap, deterministic).",
        }
    cache = json.loads(cp.read_text())
    files = cache.get("files", {})
    # most-symbol-rich files first (the ones worth knowing about)
    ranked = sorted(files.items(), key=lambda kv: -len(kv[1].get("symbols", [])))
    out = [
        {"path": rel, "lines": e.get("lines", 0), "symbols": e.get("symbols", [])}
        for rel, e in ranked[:max_files]
    ]
    return {
        "root": root,
        "cached": True,
        "generated_at": cache.get("generated_at"),
        "total_files": len(files),
        "shown": len(out),
        "files": out,
    }


# Files that mark a directory as a real project (so the hook skips random dirs).
PROJECT_MARKERS = {
    ".git",
    "package.json",
    "pyproject.toml",
    "setup.py",
    "requirements.txt",
    "go.mod",
    "Cargo.toml",
    "pom.xml",
    "build.gradle",
    "Gemfile",
    "composer.json",
    "tsconfig.json",
    "Makefile",
    "CLAUDE.md",
    "AGENTS.md",
}


def is_project(path: str) -> bool:
    p = pathlib.Path(path).expanduser()
    return any((p / m).exists() for m in PROJECT_MARKERS)


def overview(path: str) -> dict:
    """High-signal architecture digest from the cached map (free, zero-LLM):
    entrypoints, core modules (most-imported), language mix, per-file role."""
    root = _abs(path)
    cp = _cache_path(root)
    if not cp.exists():
        return {
            "root": root,
            "cached": False,
            "hint": "no map yet — call understand_project first (cheap, deterministic).",
        }
    cache = json.loads(cp.read_text())
    files = cache.get("files", {})
    indeg, _edges = build_graph(files)
    core = sorted((r for r in indeg if indeg[r] > 0), key=lambda r: -indeg[r])[:15]
    langs = {}
    for rel in files:
        ext = os.path.splitext(rel)[1].lower()
        if ext:
            langs[ext] = langs.get(ext, 0) + 1
    return {
        "root": root,
        "cached": True,
        "generated_at": cache.get("generated_at"),
        "total_files": len(files),
        "languages": dict(sorted(langs.items(), key=lambda kv: -kv[1])),
        "entrypoints": entrypoints(root, files),
        "core_modules": [
            {
                "path": r,
                "imported_by": indeg[r],
                "summary": files[r].get("summary", ""),
                "symbols": files[r].get("symbols", [])[:8],
            }
            for r in core
        ],
    }


def overview_text(path: str, max_chars: int = 6000) -> str:
    """Compact, injectable architecture digest — for the SessionStart hook."""
    o = overview(path)
    if not o.get("cached"):
        return ""
    langs = ", ".join(f"{k} {v}" for k, v in list(o["languages"].items())[:6])
    lines = [
        f"## Codebase overview (auto-loaded, free, cached) — {o['root']}",
        f"{o['total_files']} files. {langs}",
    ]
    if o["entrypoints"]:
        lines.append("Entrypoints: " + ", ".join(o["entrypoints"][:8]))
    if o["core_modules"]:
        lines.append("Core modules (most imported = likely where logic lives):")
        for m in o["core_modules"][:12]:
            role = m["summary"] or ", ".join(m["symbols"][:6])
            lines.append(
                f"- {m['path']} ←{m['imported_by']}" + (f": {role}" if role else "")
            )
    lines.append(
        "Use this to know WHERE things are; call project_context for the full file "
        "list, understand_project to refresh, then clean/grep to dive in."
    )
    return "\n".join(lines)[:max_chars]


def summary_text(path: str, max_files: int = 120, max_chars: int = 6000) -> str:
    """Injectable map for the SessionStart hook: architecture digest first, then a
    ranked file list."""
    head = overview_text(path, max_chars)
    if not head:
        return ""
    ctx = context(path, max_files)
    lines = [head, "", "Files (symbol-rich first):"]
    for f in ctx.get("files", [])[:max_files]:
        syms = ", ".join(f["symbols"][:8])
        lines.append(f"- {f['path']} ({f['lines']}L){': ' + syms if syms else ''}")
    return "\n".join(lines)[:max_chars]


async def summarize_project(path: str, model: str = "", limit: int = 0) -> dict:
    """OPT-IN cheap-LLM layer: give each code file a 1-line role summary, stored in
    the map (keyed by content hash, incremental — only un-summarized/changed files
    cost anything). Turns the symbol dump into readable 'what each file does', which
    `overview` then surfaces. Returns {summarized, skipped, total}."""
    import httpx
    from workers import call_model

    root = _abs(path)
    cp = _cache_path(root)
    if not cp.exists():
        return {"error": "no map yet — call understand_project first."}
    cache = json.loads(cp.read_text())
    files = cache.get("files", {})
    todo = [
        rel
        for rel, e in files.items()
        if e.get("symbols") and e.get("summary_hash") != e.get("hash")
    ]
    if limit and limit > 0:
        todo = todo[:limit]
    if not todo:
        return {"summarized": 0, "skipped": len(files), "total": len(files)}

    done = 0
    async with httpx.AsyncClient() as client:
        for rel in todo:
            e = files[rel]
            full = pathlib.Path(root) / rel
            try:
                text = full.read_text("utf-8", "replace")[:6000]
            except OSError:
                continue
            prompt = (
                f"One line (<=120 chars) describing what this file does. No preamble, "
                f"just the description.\n\nFILE: {rel}\n\n{text}"
            )
            r = await call_model(client, prompt, model, temperature=0, max_tokens=80)
            if "error" in r:
                continue
            e["summary"] = r["text"].strip().splitlines()[0][:160]
            e["summary_hash"] = e["hash"]
            done += 1
    cache["files"] = files
    cp.write_text(json.dumps(cache, indent=2))
    return {"summarized": done, "skipped": len(files) - done, "total": len(files)}
