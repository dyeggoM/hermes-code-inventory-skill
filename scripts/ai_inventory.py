#!/usr/bin/env python3
"""Render the code-inventory database(s) as an AI-facing reference document.

The .sqlite3 index is good for machines; an LLM needs something smaller and opinionated:
what exists, what it does, who calls whom, and where to look. This script merges every
`<project>/.code-inventory/inventory.sqlite3` under --root into one Markdown file plus a
JSON companion, and adds three things the database does not carry:

  * doc-comment purposes (read from source at render time)
  * HTTP route tables (C# attributes, syntactic)
  * impact (reverse) edges, so a signature change can be traced to its callers

Usage:
  python3 ai_inventory.py --root /path/to/repo                 # writes AI_INVENTORY.md + ai_inventory.json
  python3 ai_inventory.py --root . --out /tmp/x.md --no-json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

DB_NAME = os.path.join(".code-inventory", "inventory.sqlite3")
SKIP_DIRS = {".git", "node_modules", "bin", "obj", ".code-inventory", ".vs", "dist", "build",
             "coverage", ".next", "vendor", "venv", ".venv", "__pycache__"}
TYPE_KINDS = {"type", "class", "interface", "struct", "enum", "record", "object"}
MEMBER_KINDS = {"property", "field"}
CALLABLE_KINDS = {"method", "function", "constructor", "component", "local_function", "object"}

# ---------------------------------------------------------------- discovery / loading


def find_databases(root: str) -> list[str]:
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        candidate = os.path.join(dirpath, DB_NAME)
        if os.path.isfile(candidate):
            found.append(candidate)
    return sorted(found)


def load(root: str) -> dict:
    files, symbols, edges, warnings, containers = {}, {}, [], [], {}
    for db in find_databases(root):
        project_dir = os.path.dirname(os.path.dirname(db))
        prefix = os.path.relpath(project_dir, root)
        prefix = "" if prefix in (".", "") else prefix.replace(os.sep, "/") + "/"
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        cols = {r[1] for r in con.execute("PRAGMA table_info(functions)")}
        has_desc = "description" in cols
        kind_by_key = {r["symbol_key"]: r["kind"] for r in con.execute(
            "SELECT symbol_key, kind FROM nodes WHERE symbol_key IS NOT NULL")}
        for r in con.execute(
                "SELECT symbol_key, name, signature, path, line"
                + (", description" if has_desc else ", '' AS description")
                + " FROM functions"):
            path = prefix + r["path"].replace("\\", "/")
            key = r["symbol_key"]
            symbols[key] = {
                "key": key,
                "name": r["name"],
                "signature": (r["signature"] or "").strip(),
                "path": path,
                "line": r["line"] or 1,
                "kind": kind_by_key.get(key, "symbol"),
                "db_description": (r["description"] or "").strip(),
            }
        for r in con.execute(
                "SELECT symbol_key, kind, name, path FROM nodes "
                "WHERE symbol_key IS NOT NULL AND kind IN ('type','class','interface','struct','enum','record')"):
            key = r["symbol_key"]
            if key in symbols:
                continue
            symbols[key] = {
                "key": key,
                "name": r["name"],
                "signature": f"{r['kind']} {r['name']}",
                "path": prefix + (r["path"] or "").replace("\\", "/"),
                "line": 0,  # resolved from source at render time
                "kind": r["kind"],
                "db_description": "",
            }
        for r in con.execute(
                "SELECT symbol_key, kind, name, path FROM nodes "
                "WHERE symbol_key IS NOT NULL AND kind IN ('module','object','component')"):
            key = r["symbol_key"]
            if key not in symbols:
                containers[key] = {"kind": r["kind"], "name": r["name"],
                                   "path": prefix + (r["path"] or "").replace("\\", "/")}
        for r in con.execute(
                "SELECT caller_symbol_key, callee_symbol_key, callee_name, path, line, external FROM calls"):
            edges.append({
                "caller": r["caller_symbol_key"],
                "callee": r["callee_symbol_key"],
                "name": re.sub(r"\s+", " ", (r["callee_name"] or "")).strip(),
                "path": prefix + (r["path"] or "").replace("\\", "/"),
                "line": r["line"],
                "external": bool(r["external"]),
            })
        for r in con.execute("SELECT path, semantic_status FROM file_state"):
            files[prefix + r["path"].replace("\\", "/")] = r["semantic_status"]
        try:
            for r in con.execute("SELECT severity, message FROM diagnostics ORDER BY id DESC LIMIT 20"):
                warnings.append(f"{r['severity']}: {r['message']}")
        except sqlite3.Error:
            pass
        con.close()
    # de-duplicate identical edges coming from overlapping databases
    seen, unique = set(), []
    for e in edges:
        sig = (e["caller"], e["callee"], e["name"], e["line"])
        if sig in seen:
            continue
        seen.add(sig)
        unique.append(e)
    return {"files": files, "symbols": symbols, "edges": unique, "warnings": warnings,
            "containers": containers}


# ---------------------------------------------------------------- source enrichment

_source_cache: dict[str, list[str]] = {}


def source_lines(root: str, path: str) -> list[str]:
    if path not in _source_cache:
        try:
            with open(os.path.join(root, path), encoding="utf-8", errors="replace") as fh:
                _source_cache[path] = fh.read().splitlines()
        except OSError:
            _source_cache[path] = []
    return _source_cache[path]


def _clean(parts: list[str], limit: int = 240) -> str:
    text = " ".join(p.strip(" \t/*#") .strip() for p in parts if p.strip(" \t/*#").strip())
    text = re.sub(r"<[^>]{0,80}>", "", text)  # drop xml tags like <summary>
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def python_docstring(root: str, sym: dict) -> str:
    """PEP 257 docstring: the first string literal inside the def/class body."""
    lines = source_lines(root, sym["path"])
    for k in range(sym["line"] - 1, min(sym["line"] + 3, len(lines))):
        text = lines[k].strip()
        pos = max(text.find('"""'), text.find("'''"))
        if pos < 0:
            continue
        quote = text[pos:pos + 3]
        body = text[pos + 3:]
        if quote in body:
            return _clean([body.split(quote)[0]], limit=240)
        collected = [body] if body else []
        for j in range(k + 1, min(k + 12, len(lines))):
            inner = lines[j]
            if quote in inner:
                collected.append(inner.split(quote)[0])
                return _clean(collected, limit=240)
            collected.append(inner)
        return _clean(collected, limit=240)
    return ""


def purpose(root: str, sym: dict) -> str:
    """Doc comment immediately above a declaration, minus attributes/annotations."""
    if sym["db_description"]:
        return sym["db_description"]
    lines = source_lines(root, sym["path"])
    i = sym["line"] - 2
    while i >= 0 and re.match(r"^\s*(\[[^\]]*\]|@\w)", lines[i] or ""):
        i -= 1
    if i < 0:
        return ""
    block: list[str] = []
    if sym["path"].endswith(".cs"):
        while i >= 0 and lines[i].strip().startswith("///"):
            block.append(lines[i].strip()[3:])
            i -= 1
    elif i >= 0 and lines[i].strip().endswith("*/"):
        j = i
        while j >= 0 and not lines[j].strip().startswith("/*"):
            j -= 1
        if j >= 0:
            inner = [lines[k].strip().lstrip("*").strip() for k in range(j, i + 1)]
            inner[0] = re.sub(r"^/\*+", "", inner[0]).strip()
            inner[-1] = re.sub(r"\*+/$", "", inner[-1]).strip()
            block = inner
    else:
        while i >= 0 and lines[i].strip().startswith("//"):
            block.append(lines[i].strip()[2:])
            i -= 1
    text = _clean(list(reversed(block)))
    if not text and lang3(sym["key"]) == "python":
        return python_docstring(root, sym)
    return text


def ts_interface_members(root: str, sym: dict) -> list[str]:
    """Depth-1 member names of a TS interface, straight from the source."""
    lines = source_lines(root, sym["path"])
    out: list[str] = []
    depth = 0
    started = False
    for raw in lines[sym["line"] - 1:sym["line"] + 200]:
        if not started:
            if "{" in raw:
                started = True
                depth = raw.count("{") - raw.count("}")
                tail = raw.split("{", 1)[1]
                m = re.match(r"^\s*(\w+)\??\s*:\s*(.+?);?\s*$", tail)
                if m:
                    out.append(f"{m.group(1)}:{_short_type(m.group(2))}")
            continue
        depth += raw.count("{") - raw.count("}")
        if depth <= 0:
            break
        m = re.match(r"^\s{2,6}(\w+)\??\s*:\s*(.+?);?\s*$", raw)
        if m and not m.group(2).startswith("//"):
            out.append(f"{m.group(1)}:{_short_type(m.group(2))}")
    return out


def _short_type(text: str) -> str:
    text = re.sub(r"\s+", "", text)
    return text[:40]


def csharp_type_params(sig: str) -> str:
    """`List<Item> ItemDTO.Children` -> `List<Item>`"""
    return sig.split(" ", 1)[0] if " " in sig else ""


# ---------------------------------------------------------------- routes


CLASS_RE = re.compile(r"^\s*public\s+(?:sealed\s+|partial\s+|abstract\s+)*class\s+(\w+)\s*(?:<[^>]*>)?\s*(?::\s*([\w<>, .]+))?")


def csharp_routes(root: str, files: list[str]) -> list[tuple[str, str, str]]:
    """[(verb, route, handler)] from attributes, including endpoints inherited from a base
    controller. Syntactic only: no dynamic routing, no `[FromRoute]` semantics."""
    route_re = re.compile(r'^\s*\[Route\("([^"]*)"\)\]')
    http_re = re.compile(r'^\s*\[Http(Get|Post|Put|Delete|Patch|Head|Options)(?:\("([^"]*)"\))?\]')
    method_re = re.compile(r"^\s*public\s+(?:async\s+)?[\w<>,\[\]?. ]+\s+(\w+)\s*\(")
    classes: dict[str, dict] = {}
    order: list[str] = []
    for path in files:
        if not path.endswith(".cs"):
            continue
        current, pending_route, pending_http = None, None, None
        for line in source_lines(root, path):
            m = route_re.match(line)
            if m:
                pending_route = m.group(1)
                continue
            m = CLASS_RE.match(line)
            if m:
                current = m.group(1)
                base = (m.group(2) or "").split("<")[0].split(",")[0].strip() or None
                classes[current] = {"route": pending_route, "base": base, "methods": [], "path": path}
                order.append(current)
                pending_route, pending_http = None, None
                continue
            m = http_re.match(line)
            if m and current:
                pending_http = (m.group(1).upper(), m.group(2) or "")
                continue
            if pending_http and current:
                hm = method_re.match(line)
                if hm:
                    classes[current]["methods"].append((pending_http[0], pending_http[1], hm.group(1)))
                    pending_http = None
                elif line.strip() and not line.strip().startswith(("[", "//")):
                    pending_http = None
    rows: list[tuple[str, str, str]] = []
    for name in order:
        info = classes[name]
        if info["route"] is None:
            continue  # generic base or a controller with no route: its attributes are inherited
        base_route = info["route"].replace("[controller]", re.sub(r"Controller$", "", name).lower())
        methods = list(info["methods"])
        seen = {m[2] for m in methods}
        cursor = info["base"]
        depth = 0
        while cursor and cursor in classes and depth < 4:
            for verb, rel, handler in classes[cursor]["methods"]:
                if handler not in seen:
                    methods.append((verb, rel, handler))
                    seen.add(handler)
            cursor = classes[cursor]["base"]
            depth += 1
        for verb, rel, handler in methods:
            full = "/" + "/".join(p for p in (base_route, rel) if p).replace("//", "/")
            rows.append((verb, full, f"{name}.{handler}"))
    return rows


# ---------------------------------------------------------------- rendering


def qualname(key: str, lang: str) -> str:
    if lang == "cs":
        body = key[2:] if len(key) > 2 and key[1] == ":" else key
        body = re.sub(r"`+\d*", "", body.split("(", 1)[0])
        body = re.sub(r"\{[^{}]*\}", "", body)
        return body.split(".", 1)[1] if "." in body else body
    parts = key.split(":", 2)
    q = parts[2] if len(parts) > 2 else key
    return q.split("(", 1)[0]


def resolve_type_line(root: str, sym: dict) -> int:
    """Type nodes carry no line in the index; find the declaration in source once."""
    if sym["line"]:
        return sym["line"]
    pattern = re.compile(r"\b(class|interface|record|struct|enum|type)\s+" + re.escape(sym["name"]) + r"\b")
    for i, line in enumerate(source_lines(root, sym["path"]), 1):
        if pattern.search(line):
            sym["line"] = i
            return i
    sym["line"] = 1
    return 1


def key_lang_str(key: str) -> str:
    return "cs" if key.startswith(("M:", "T:", "P:", "N:", "F:", "E:")) else "js"


def key_lang(sym: dict) -> tuple[str, str]:
    return key_lang_str(sym["key"]), sym["key"]


def lang3(key: str) -> str:
    if key.startswith("py:"):
        return "python"
    return "cs" if key_lang_str(key) == "cs" else "javascript"


def render(root: str, data: dict, title: str | None = None, note: str | None = None, stamp: bool = True,
           unresolved_per_file: int = 8, include_map: bool = True,
           context_syms: dict[str, dict] | None = None) -> tuple[str, dict]:
    syms: dict[str, dict] = data["symbols"]
    edges = data["edges"]
    out: list[str] = []
    w = out.append

    by_file: dict[str, list[dict]] = defaultdict(list)
    for s in syms.values():
        resolve_type_line(root, s)
        by_file[s["path"]].append(s)
    for lst in by_file.values():
        lst.sort(key=lambda s: (s["line"], s["key"]))

    containers: dict[str, dict] = data.get("containers", {})
    ctx_syms: dict[str, dict] = context_syms or {}

    def split_params(text: str) -> list[str]:
        parts, depth, buf = [], 0, []
        for ch in text:
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
            if ch == "," and depth == 0:
                parts.append("".join(buf))
                buf = []
            else:
                buf.append(ch)
        if buf:
            parts.append("".join(buf))
        return parts

    def label(key: str) -> str:
        """Short, overload-distinguishing label: qualified name + parameter names only."""
        q = qualname(key, key_lang_str(key))
        s = syms.get(key) or ctx_syms.get(key)
        if s and "(" in s["signature"]:
            sig = s["signature"]
            names = []
            for p in split_params(sig[sig.find("(") + 1: sig.rfind(")")]):
                p = p.strip()
                if not p or p in ("self", "cls"):
                    continue
                name = re.split(r"[:=]", p)[0].strip().lstrip("*&")
                names.append(name or "?")
            if names:
                q += "(" + ", ".join(names[:4]) + (", …" if len(names) > 4 else "") + ")"
            else:
                q += "()"
        return q

    def info(key: str) -> tuple[str, str, int]:
        s = syms.get(key) or ctx_syms.get(key)
        if s:
            return label(key), s["path"], s["line"]
        c = containers.get(key)
        if c:
            return ("<module>" if c["kind"] == "module" else "<object>"), c["path"], 0
        return qualname(key, key_lang_str(key)), "?", 0

    known = set(syms) | set(containers) | set(ctx_syms)
    resolved = [e for e in edges if e["callee"] in syms and e["caller"] in known]
    unresolved = [e for e in edges if e["callee"] not in syms and e["callee"] not in ctx_syms]
    callers_of: dict[str, list[dict]] = defaultdict(list)
    callees_of: dict[str, list[dict]] = defaultdict(list)
    for e in resolved:
        callers_of[e["callee"]].append(e)
        callees_of[e["caller"]].append(e)

    langs = Counter(lang3(s["key"]) for s in syms.values())
    statuses = Counter(data["files"].get(s["path"], "?") for s in syms.values())
    commit = "(unknown)"
    try:
        import subprocess
        commit = subprocess.run(["git", "-C", root, "rev-parse", "--short", "HEAD"],
                                capture_output=True, text=True, timeout=10).stdout.strip() or "(unknown)"
    except Exception:
        pass

    w(f"# {title or 'AI Inventory — ' + os.path.basename(os.path.abspath(root))}")
    w("")
    if stamp:
        w(f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} from `{commit}` by "
          f"`code-inventory` / `ai_inventory.py`. Do not hand-edit: regenerate with `sync` + this script.")
    else:
        # A committed inventory must not change just because it was rendered again.
        w("Generated by `code-inventory` / `ai_inventory.py`. Do not hand-edit: regenerate with "
          "`sync` + this script.")
    if note:
        w("")
        w(note)
    w("")
    w(f"Indexed: **{len(syms)} symbol{'s' if len(syms) != 1 else ''}** across "
      f"**{len(by_file)} file{'s' if len(by_file) != 1 else ''}** "
      f"({langs.get('cs', 0)} C#, {langs.get('python', 0)} Python, {langs.get('javascript', 0)} JS/TS/Vue) · "
      f"**{len(resolved)} resolved** in-project call edges, {len(unresolved)} unresolved "
      f"(framework or dynamic). Analyzer states: {dict(statuses)}.")
    w("")

    w("## How to use this file")
    w("")
    w("1. **Look before you write.** Search this file for the symbol name/route you need. If it exists, "
      "extend or call it — do not re-implement it in another file.")
    w("2. **Fetch code by range, not by repo.** Every entry carries `path:line`; open only that file (or "
      "that range plus ~10 lines) and only the files listed under *Impact* for the symbol you touch.")
    w("3. **Before changing a signature, read its Impact line** — every listed caller must be updated in "
      "the same change. Transitive impact = walk *Impact* up one level at a time.")
    w("4. **Notation:** `→` calls, `←` called by, `(Lnn)` source line, `kind` is one of "
      "`method|constructor|property|type|interface|object|component|function`. A listed call without a "
      "target (`?`) is framework/dynamic and cannot be traced statically.")
    w("5. **Absence is not proof.** Dynamic dispatch, reflection, templates, generated code and "
      "unsupported languages are not indexed (see *Coverage*). Grep before concluding a symbol is unused.")
    w("6. **Never paste this document or a whole source file into the conversation as \"context\".** Cite "
      "`path:line` and read only what the change needs: anything quoted into the prompt is re-sent on "
      "every later turn.")
    w("")

    # ---------------- project map
    if include_map:
        w("## Project map")
        w("")
        for folder in sorted({p.split("/", 1)[0] if "/" in p else "." for p in by_file}):
            files_here = sorted(p for p in by_file if (p.split("/", 1)[0] if "/" in p else ".") == folder)
            if folder.endswith((".md", ".json", ".sln", ".csproj")):
                continue
            n_syms = sum(len(by_file[p]) for p in files_here)
            top = Counter(s["kind"] for p in files_here for s in by_file[p]).most_common(4)
            w(f"- **{folder}/** — {len(files_here)} indexed files, {n_syms} symbols "
              f"({', '.join(f'{k} {v}' for k, v in top)})")
        w("")

    # ---------------- routes
    routes = csharp_routes(root, sorted(by_file))
    if routes:
        w("## HTTP routes")
        w("")
        seen = set()
        for verb, route, handler in routes:
            if (verb, route, handler) in seen:
                continue
            seen.add((verb, route, handler))
            w(f"- `{verb:<6} {route}` — `{handler}`")
        generic = sorted({s["name"] for s in syms.values()
                          if s["kind"] == "type" and "Controller" in s["name"] and "<" in s["signature"]})
        if generic:
            w("")
            w(f"Inherited from `{', '.join(generic)}` (template CRUD applies to every derived controller): "
              "`GET {base}`, `GET {base}/{id}`, `POST {base}`, `PUT {base}/{id}`, `DELETE {base}/{id}`, "
              "`GET {base}/test`.")
        w("")

    # ---------------- symbols
    w("## Symbols by file")
    w("")
    for path in sorted(by_file):
        lst = by_file[path]
        lines_total = len(source_lines(root, path))
        status = data["files"].get(path, "?")
        w(f"### `{path}` — {lines_total} lines, {len(lst)} symbol" + ("s" if len(lst) != 1 else "") + ("" if status == "ok" else f", index {status}"))
        w("")
        type_children: dict[str, list[dict]] = defaultdict(list)
        owner = ""
        for s in lst:
            if s["kind"] in TYPE_KINDS or s["kind"] in ("component", "module"):
                owner = s["key"]
            elif s["kind"] in MEMBER_KINDS and owner:
                type_children[owner].append(s)
        for s in lst:
            if s["kind"] in MEMBER_KINDS or s["kind"] == "module":
                continue
            text = _symbol_line(root, s, type_children.get(s["key"], []), callers_of, callees_of)
            if text:
                w(text)
        w("")

    # ---------------- call graph
    if resolved:
        w("## Call graph (resolved, in-project)")
        w("")
        for key in sorted(callees_of, key=lambda k: (info(k)[1], info(k)[2])):
            name, path, line = info(key)
            seen, targets = set(), []
            for e in sorted(callees_of[key], key=lambda e: e["line"] or 0):
                if e["callee"] in seen:
                    continue
                seen.add(e["callee"])
                targets.append(f"{label(e['callee'])}`(L{e['line']})`")
            extra = len({e['callee'] for e in callees_of[key]}) - len(targets)
            text = ", ".join(targets[:8]) + (f" +{extra} more" if extra > 0 else "")
            w(f"- `{name}` ({path}{':' + str(line) if line else ''}) → {text}")
        w("")

        # ---------------- impact
        w("## Impact — who calls what")
        w("")
        w("Changing a symbol means checking every symbol on its right-hand side.")
        w("")
        for key in sorted(callers_of, key=lambda k: (-len({e['caller'] for e in callers_of[k]}),
                                                     syms[k]["path"], syms[k]["line"])):
            name = label(key)
            first: dict[str, dict] = {}
            for e in sorted(callers_of[key], key=lambda e: (e["path"], e["line"] or 0)):
                first.setdefault(e["caller"], e)
            shown = list(first.items())[:6]
            callers = []
            for caller_key, e in shown:
                cname, cpath, _ = info(caller_key)
                callers.append(f"{cname}`({cpath}:{e['line']})`")
            text = ", ".join(callers) + (f" +{len(first) - len(shown)} more" if len(first) > len(shown) else "")
            w(f"- `{name}` ← {text}")
        w("")

    # ---------------- unresolved
    if unresolved:
        w("## Framework / dynamic calls (unresolved)")
        w("")
        w("These call sites have no in-project target: framework APIs, interface dispatch or dynamic "
          "code. Useful to know what a file depends on, not for impact analysis.")
        w("")
        per_file: dict[str, Counter] = defaultdict(Counter)
        for e in unresolved:
            per_file[e["path"]][re.sub(r"\(.*", "", e["name"] or "?").split("(")[0]] += 1
        for path in sorted(per_file):
            top = ", ".join(f"{n} ×{c}" for n, c in per_file[path].most_common(unresolved_per_file))
            w(f"- `{path}` — {top}")
        w("")

    # ---------------- regenerate
    w("## Regenerate")
    w("")
    w("```bash")
    w("# after changing code (incremental; add --full to force)")
    w("python3 <skill>/scripts/inventory.py sync --root . --lang js")
    w("python3 <skill>/scripts/inventory.py sync --root <csharp-project> --lang cs")
    w("python3 <skill>/scripts/ai_inventory.py --root .")
    w("```")
    w("")

    # ---------------- coverage
    w("## Coverage & caveats")
    w("")
    w(f"- Index states: {dict(statuses)}. Files not marked `ok` were not analysed.")
    w(f"- In-project call edges resolved: **{len(resolved)}** of {len(edges)} call sites "
      f"({(100 * len(resolved) // max(len(edges), 1))}%). Unresolved ones stay `?`-less and are not guessed.")
    w("- C#: no `new`-constructor, property-access or field/event edges; reflection/`dynamic` unresolved. "
      "A call through an interface resolves to the **interface member** (the contract), not to every "
      "implementation — search the symbol by name to find the classes that implement it.")
    w("- JS/TS: member calls on unknown receivers, computed access, barrel re-exports and dynamic "
      "`require` stay unresolved. Object-literal methods are indexed; `<script>` blocks only — template "
      "expressions (e.g. `@click=\"fn\"`) are not.")
    w("- Purposes come from doc comments in the source; blank means the code has none. Do not treat a "
      "blank purpose as 'unknown' behaviour — read the file.")
    for warning in dict.fromkeys(data["warnings"]):
        w(f"- analyzer warning: {warning}")
    w("")

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "root": os.path.abspath(root),
        "commit": commit,
        "symbols": [dict(s, qualname=qualname(s["key"], key_lang(s)[0]),
                          purpose=purpose(root, s), lang=key_lang(s)[0]) for s in syms.values()],
        "edges": resolved,
        "unresolved": [
            {"path": e["path"], "line": e["line"], "caller": e["caller"], "name": e["name"]}
            for e in unresolved
        ],
        "files": data["files"],
    }
    return "\n".join(out).rstrip() + "\n", payload


def base_types(root: str, sym: dict) -> str:
    """Base list from the declaration line (`class X : Base<T>`, `class X(Base):`)."""
    lines = source_lines(root, sym["path"])
    if not (0 < sym["line"] <= len(lines)):
        return ""
    line = lines[sym["line"] - 1]
    if lang3(sym["key"]) == "python":
        m = re.match(r"^\s*class\s+\w+\s*\(([^)]*)\)", line)
        return m.group(1).strip() if m else ""
    m = re.search(r":\s*([^({]+?)(?:\{|$)", line)
    return m.group(1).strip() if m else ""


def _symbol_line(root: str, sym: dict, props: list[dict], callers_of, callees_of) -> str:
    lang, _ = key_lang(sym)
    q = qualname(sym["key"], lang)
    kind = sym["kind"]
    sig = sym["signature"]
    detail = ""
    if lang == "cs":
        if kind in ("method", "constructor"):
            params = sig[sig.find("("):] if "(" in sig else "()"
            ret = csharp_type_params(sig)
            detail = params + (f" → {ret}" if ret and kind == "method" else "")
        elif kind in TYPE_KINDS:
            detail = "" if sig.startswith(kind) else f" ({sig})"
    elif kind in ("function", "method", "constructor", "local_function"):
        params = sig[sig.find("("):] if "(" in sig else "()"
        detail = params
    elif kind == "component":
        detail = " (SFC)" if sig.startswith("SFC") else ""
    line = f"- `L{sym['line']}` **{kind}** `{q}`{detail}"
    if kind in TYPE_KINDS or kind == "component":
        fields: list[str] = []
        if props:
            fields = [_prop_label(p, lang) for p in sorted(props, key=lambda p: p["line"])]
        elif kind == "interface" and lang == "js":
            fields = ts_interface_members(root, sym)
        base = base_types(root, sym) if lang3(sym["key"]) in ("cs", "python") else ""
        if base:
            line += f" : {base}"
        if fields:
            line += f" — {'state' if kind == 'component' else 'fields'}: `{', '.join(fields)}`"
    why = purpose(root, sym)
    if why:
        line += f" — {why}"
    if kind in CALLABLE_KINDS and kind != "object":
        outs = callees_of.get(sym["key"], [])
        if outs:
            names = sorted({qualname(e["callee"], key_lang_str(e["callee"])) for e in outs})
            line += f" · → {len(outs)} ({', '.join(names[:4])}{'…' if len(names) > 4 else ''})"
    ins = callers_of.get(sym["key"], [])
    if ins:
        line += f" · ← {len(ins)}"
    return line


def _prop_label(p: dict, lang: str) -> str:
    if lang == "cs":
        t = csharp_type_params(p["signature"])
        return f"{p['name']}:{t}" if t else p["name"]
    return p["name"]


# ---------------------------------------------------------------- main


def context_pack(root: str, data: dict, query: str, budget: int) -> str:
    """Minimal context for one change: the symbol's code plus its callers/callees by reference."""
    syms = data["symbols"]
    edges = data["edges"]
    q = query.lower().strip()

    def matches(s: dict) -> tuple[int, str]:
        name = qualname(s["key"], key_lang_str(s["key"])).lower()
        if name == q or s["key"].lower() == q:
            return (0, name)
        if name.endswith("." + q) or name.split(".")[-1] == q:
            return (1, name)
        if q in name:
            return (2, name)
        return (9, name)

    ranked = sorted(((matches(s), s) for s in syms.values()), key=lambda t: (t[0][0], len(t[0][1])))
    ranked = [t for t in ranked if t[0][0] < 9]
    if not ranked:
        return (f"No indexed symbol matches `{query}`. Grep the repo; it may be external, "
                f"generated or unindexed.\n")
    _, sym = ranked[0]
    name = qualname(sym["key"], key_lang_str(sym["key"]))
    by_file: dict[str, list[int]] = defaultdict(list)
    for s in syms.values():
        by_file[s["path"]].append(s["line"])
    lines = source_lines(root, sym["path"])
    starts = sorted(x for x in by_file[sym["path"]] if x > sym["line"])
    end = (starts[0] - 1) if starts else len(lines)
    end = min(end, sym["line"] + 119)
    fence = "csharp" if key_lang_str(sym["key"]) == "cs" else "ts"
    if sym["path"].endswith(".vue"):
        fence = "vue"

    out = [f"# Context: `{name}`", "",
           f"`{sym['path']}:{sym['line']}` — {sym['kind']} — {sym.get('purpose') or '(no doc comment)'}", ""]
    if len(ranked) > 1:
        others = ", ".join(f"`{qualname(s['key'], key_lang_str(s['key']))}`" for _, s in ranked[1:6])
        out += [f"Other matches: {others}", ""]
    out += [f"```{fence}"] + lines[sym["line"] - 1:end] + ["```", ""]
    ins = [e for e in edges if e["callee"] == sym["key"]]
    outs = [e for e in edges if e["caller"] == sym["key"] and e["callee"]]
    if ins:
        out += ["## Callers — they must be checked when this changes", ""]
        out += [f"- `{qualname(e['caller'], key_lang_str(e['caller']))}` — {e['path']}:{e['line']}" for e in ins]
        out.append("")
    if outs:
        out += ["## Callees — contracts this code relies on", ""]
        out += [f"- `{qualname(e['callee'], key_lang_str(e['callee']))}` — {e['path']}:{e['line']}" for e in outs]
        out.append("")
    text = "\n".join(out)
    if len(text) > budget:
        text = text[:budget] + "\n\n[truncated at --budget]\n"
    return text


def group_of(path: str, depth: int) -> str:
    parts = path.split("/")
    dirs = parts[:-1]
    if not dirs:
        return "."
    return "/".join(dirs[:depth])


def build_groups(syms: dict[str, dict], depth: int, max_symbols: int = 120,
                 max_depth: int = 4) -> dict[str, list[str]]:
    """Module groups, refined by sub-folder while a group is too big, then per file."""
    def rec(d: int, keys: list[str]) -> dict[str, list[str]]:
        buckets: dict[str, list[str]] = defaultdict(list)
        for k in keys:
            buckets[group_of(syms[k]["path"], d)].append(k)
        if len(buckets) > 1 and d < max_depth:
            out: dict[str, list[str]] = {}
            for g, ks in buckets.items():
                out.update(rec(d + 1, ks) if len(ks) > max_symbols else {g: ks})
            return out
        out = {}
        for k in keys:
            out.setdefault(syms[k]["path"], []).append(k)
        return out
    return rec(depth, list(syms))


def write_split(root: str, data: dict, out: str, depth: int, stamp: bool = True) -> dict:
    """Large repo: AI_INVENTORY.md becomes a router; one document per module group."""
    syms: dict[str, dict] = data["symbols"]
    edges = data["edges"]
    groups = build_groups(syms, depth)
    for g in groups:
        groups[g].sort(key=lambda k: (syms[k]["path"], syms[k]["line"]))

    outdir = os.path.join(os.path.dirname(out), "inventory")
    os.makedirs(outdir, exist_ok=True)
    written: dict[str, dict] = {}
    cross: Counter = Counter()
    for g in sorted(groups):
        keys = set(groups[g])
        sub_syms = {k: syms[k] for k in keys}
        sub_edges = [e for e in edges if (e["callee"] in keys) or (e["caller"] in keys)]
        for e in sub_edges:
            if e["caller"] in keys and e["callee"] in syms and e["callee"] not in keys:
                cross[(g, group_of(syms[e["callee"]]["path"], depth))] += 1
        title = f"AI Inventory — {os.path.basename(os.path.abspath(root))} / {g}"
        note = (f"Part of a split inventory ({len(groups)} parts). Entry point and cross-module "
                f"summary: `{os.path.basename(out)}`.")
        text, _ = render(root, {"symbols": sub_syms, "edges": sub_edges, "files": data["files"],
                                "containers": data.get("containers", {}), "warnings": data["warnings"]},
                         title=title, note=note, unresolved_per_file=3, include_map=False,
                         context_syms=syms, stamp=stamp)
        name = g.replace("/", "-").replace(".", "-").replace(":", "") + ".md"
        path = os.path.join(outdir, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        written[g] = {"path": path, "chars": len(text), "symbols": len(sub_syms),
                      "files": len({syms[k]["path"] for k in keys})}

    w: list[str] = []
    w.append(f"# AI Inventory — {os.path.basename(os.path.abspath(root))}")
    w.append("")
    w.append(f"{'Generated' if not stamp else 'Generated ' + datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} by `code-inventory` / "
             f"`ai_inventory.py`. Do not hand-edit.")
    w.append("")
    w.append(f"This index routes you to {len(groups)} module documents. Indexed: **{len(syms)} symbols** "
             f"across **{len({s['path'] for s in syms.values()})} files**.")
    w.append("")
    w.append("## How to use this file — mandatory protocol")
    w.append("")
    w.append("You are an AI agent working in this repository. This index is the map; the module files are "
             "the territory. Follow these rules in order, they exist because skipping them is expensive:")
    w.append("")
    w.append("1. **Start here, not in the code.** Read this index, pick the module below that owns your "
             "change, and open **only** that module file. Loading every module is the same mistake as "
             "reading the whole repo.")
    w.append("2. **Then the range, not the file.** Every symbol carries `path:line`. Open that range plus "
             "~10 lines of context. Do not open a 700-line controller to look at one method.")
    w.append("3. **Reuse before writing.** Search the module for the symbol, route or DTO you need. If it "
             "exists, extend or call it; a second implementation of the same concern is a defect.")
    w.append("4. **Read Impact before changing a signature.** Every caller listed must be updated in the "
             "same change, including callers in other modules (they carry their own `path:line`). Walk one "
             "level at a time for transitive impact.")
    w.append("5. **Never copy the inventory or a source file into the conversation as \"context\".** Cite "
             "`path:line` instead and read what you actually need. The document is a map, quoted into the "
             "prompt it becomes dead weight that is re-sent on every turn.")
    w.append("6. **One symbol, one question? Use the CLI, not a document:**")
    w.append("")
    w.append("```bash")
    w.append("python3 <skill>/scripts/ai_inventory.py --root . --context SYMBOL_NAME --budget 8000")
    w.append("```")
    w.append("")
    w.append("7. **Absence is not proof.** Dynamic dispatch, reflection, generated code and unsupported "
             "languages are not indexed. Grep before concluding that something is unused.")
    w.append("")
    w.append("## Modules")
    w.append("")
    for g in sorted(written):
        info = written[g]
        sample = ", ".join(f"`{syms[k]['name']}`" for k in groups[g][:5])
        base = os.path.basename(info["path"])
        w.append(f"- **{g}** — {info['files']} file{'s' if info['files'] != 1 else ''}, "
                 f"{info['symbols']} symbols, {info['chars'] // 1024} KB → [`inventory/{base}`](inventory/{base})"
                 f" · {sample}")
    w.append("")
    if cross:
        w.append("## Cross-module calls (who depends on whom)")
        w.append("")
        for (src, dst), n in cross.most_common(40):
            w.append(f"- `{src}` → `{dst}` — {n} call{'s' if n != 1 else ''}")
        w.append("")
    unresolved = [e for e in edges if e["callee"] not in syms]
    w.append("## Coverage & caveats")
    w.append("")
    w.append(f"- {len(edges) - len(unresolved)} in-project call edges of {len(edges)} call sites; "
             f"the rest are framework/dynamic.")
    w.append("- Purposes come from doc comments only; blank means none. Absence of a symbol is not proof "
             "it does not exist (dynamic dispatch, generated code and unsupported languages are not indexed).")
    for warning in dict.fromkeys(data["warnings"]):
        w.append(f"- analyzer warning: {warning}")
    w.append("")
    w.append("## Regenerate")
    w.append("")
    w.append("```bash")
    w.append("python3 <skill>/scripts/inventory.py sync --root . --lang py   # or cs / js")
    w.append("python3 <skill>/scripts/ai_inventory.py --root .")
    w.append("```")
    w.append("")
    text = "\n".join(w).rstrip() + "\n"
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(text)
    return {"index": out, "index_chars": len(text), "parts": written,
            "largest_part": max((v["chars"] for v in written.values()), default=0)}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Render an AI-facing inventory from code-inventory databases.")
    ap.add_argument("--root", default=".")
    ap.add_argument("--out", default=None, help="markdown path (default <root>/AI_INVENTORY.md)")
    ap.add_argument("--json", default=None, help="json path (default <root>/ai_inventory.json)")
    ap.add_argument("--no-json", action="store_true")
    ap.add_argument("--max-chars", type=int, default=40_000,
                    help="size that triggers the module split (default 40000)")
    ap.add_argument("--context", default=None,
                    help="print a minimal context pack for one symbol instead of the whole document")
    ap.add_argument("--budget", type=int, default=8000, help="max chars for --context (default 8000)")
    ap.add_argument("--split", choices=["auto", "never"], default="auto",
                    help="over --max-chars, write a router index plus one file per module (default auto)")
    ap.add_argument("--depth", type=int, default=2, help="path segments that define a module (default 2)")
    ap.add_argument("--no-stamp", action="store_true",
                    help="omit the timestamp/commit line so a committed document only changes with the code")
    args = ap.parse_args(argv)
    root = os.path.realpath(args.root)
    if not find_databases(root):
        print("no .code-inventory/inventory.sqlite3 under " + root, file=sys.stderr)
        return 2
    data = load(root)
    if args.context:
        enriched = {k: dict(v, purpose=purpose(root, v)) for k, v in data["symbols"].items()}
        text = context_pack(root, {"symbols": enriched, "edges": data["edges"]}, args.context, args.budget)
        if args.out:
            with open(os.path.abspath(args.out), "w", encoding="utf-8") as fh:
                fh.write(text)
            print(json.dumps({"context": os.path.abspath(args.out), "chars": len(text)}))
        else:
            sys.stdout.write(text)
        return 0
    markdown, payload = render(root, data, stamp=not args.no_stamp)
    out = os.path.abspath(args.out or os.path.join(root, "AI_INVENTORY.md"))
    split = None
    if len(markdown) > args.max_chars and args.split != "never":
        split = write_split(root, data, out, args.depth, stamp=not args.no_stamp)
    else:
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(markdown)
    js = None
    if not args.no_json:
        js = os.path.abspath(args.json or os.path.join(root, "ai_inventory.json"))
        with open(js, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=1)
    size = len(markdown)
    summary = {
        "markdown": out,
        "split": bool(split),
        "chars": split["index_chars"] if split else size,
        "approx_tokens": (split["index_chars"] if split else size) // 4,
        "json": js,
        "symbols": len(payload["symbols"]),
        "resolved_edges": len(payload["edges"]),
        "unresolved_calls": len(payload["unresolved"]),
    }
    if split:
        summary["parts"] = len(split["parts"])
        summary["largest_part_kb"] = split["largest_part"] // 1024
        summary["parts_written"] = split["parts"]
    print(json.dumps(summary, indent=2))
    if not split and size > args.max_chars:
        print(f"warning: {size} chars exceeds --max-chars {args.max_chars}; use --split auto or --depth 1",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))