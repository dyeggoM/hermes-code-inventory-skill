#!/usr/bin/env python3
"""Python analyzer for code-inventory (standard library only).

Scans every ``.py`` file under ``--root`` using ``ast`` and prints ONE JSON object
to stdout: per-file declarations, calls resolved against a project-wide symbol
table, and diagnostics. A call that cannot be resolved to a symbol declared in
this project is emitted with ``callee_symbol_key: null`` and ``external: 1``
(never guessed). A file that fails to parse is reported as an ``error`` diagnostic
rather than crashing the run.
"""

import argparse
import ast
import json
import os

SKIP_DIRS = {".git", ".venv", "venv", "node_modules",
             "__pycache__", ".code-inventory", "build", "dist"}
MODULE_QN = "<module>"
FUNC = (ast.FunctionDef, ast.AsyncFunctionDef)
NAMED = FUNC + (ast.ClassDef,)


def dotted_module(relpath):
    """Root-relative posix path -> dotted module name (app/service.py -> app.service)."""
    stem = relpath[:-3] if relpath.endswith(".py") else relpath
    parts = [p for p in stem.split("/") if p]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts else relpath


def key(module, qn):
    return f"py:{module}:{qn}"


def qualname_of(node, parent):
    parts, cur = [], node
    while cur is not None:
        if isinstance(cur, NAMED):
            parts.append(cur.name)
        cur = parent.get(cur)
    return ".".join(reversed(parts))


def nearest_ancestor(node, parent, types):
    cur = parent.get(node)
    while cur is not None:
        if isinstance(cur, types):
            return cur
        cur = parent.get(cur)
    return None


def build_parent_map(tree):
    parent = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[child] = node
    return parent


def _u(node):
    return ast.unparse(node)


def fmt_args(a):
    out = []
    pos = list(a.posonlyargs) + list(a.args)
    off = len(pos) - len(a.defaults)
    for i, arg in enumerate(pos):
        s = arg.arg
        if arg.annotation is not None:
            s += ": " + _u(arg.annotation)
        if i >= off:
            s += "=" + _u(a.defaults[i - off])
        out.append(s)
    if a.vararg is not None:
        s = "*" + a.vararg.arg
        if a.vararg.annotation is not None:
            s += ": " + _u(a.vararg.annotation)
        out.append(s)
    for i, arg in enumerate(a.kwonlyargs):
        s = arg.arg
        if arg.annotation is not None:
            s += ": " + _u(arg.annotation)
        if i < len(a.kw_defaults) and a.kw_defaults[i] is not None:
            s += "=" + _u(a.kw_defaults[i])
        out.append(s)
    if a.kwarg is not None:
        s = "**" + a.kwarg.arg
        if a.kwarg.annotation is not None:
            s += ": " + _u(a.kwarg.annotation)
        out.append(s)
    return ", ".join(out)


def func_sig(node):
    s = f"def {node.name}(" + fmt_args(node.args) + ")"
    if node.returns is not None:
        s += " -> " + _u(node.returns)
    return s


def is_property(node):
    for d in node.decorator_list:
        if (isinstance(d, ast.Name) and d.id == "property") or \
           (isinstance(d, ast.Attribute) and d.attr == "property"):
            return True
    return False


def find_py_files(root):
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if fn.endswith(".py"):
                found.append(os.path.join(dirpath, fn))
    return sorted(found)


def parse_file(path):
    """Return (tree, diagnostics); tree is None when the file cannot be parsed."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            src = fh.read()
    except OSError as e:
        return None, [{"severity": "error", "message": f"cannot read file: {e}"}]
    try:
        return ast.parse(src, filename=path), []
    except SyntaxError as e:
        return None, [{"severity": "error",
                       "message": f"parse error at line {e.lineno}: {e.msg}"}]
    except ValueError as e:
        return None, [{"severity": "error", "message": f"cannot parse: {e}"}]


def module_decl(module):
    return {"kind": "module", "name": module.split(".")[-1],
            "symbol_key": key(module, MODULE_QN), "parent_symbol_key": None,
            "signature": f"module {module}", "line": 1}


def collect_declarations(tree, parent, module, out):
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            par = nearest_ancestor(node, parent, (ast.ClassDef,))
            bases = [_u(b) for b in node.bases]
            out.append({"kind": "class", "name": node.name,
                        "symbol_key": key(module, qualname_of(node, parent)),
                        "parent_symbol_key": key(module, qualname_of(par, parent)) if par else None,
                        "signature": f"class {node.name}" + (f"({', '.join(bases)})" if bases else ""),
                        "line": node.lineno})
        elif isinstance(node, FUNC):
            enc = nearest_ancestor(node, parent, NAMED)
            if enc is None:
                kind = "function"
            elif isinstance(enc, ast.ClassDef):
                kind = "property" if is_property(node) else "method"
            else:
                kind = "local_function"
            par = nearest_ancestor(node, parent, NAMED)
            out.append({"kind": kind, "name": node.name,
                        "symbol_key": key(module, qualname_of(node, parent)),
                        "parent_symbol_key": key(module, qualname_of(par, parent)) if par else None,
                        "signature": func_sig(node), "line": node.lineno})


def imports_for(tree, module):
    from_imports, module_aliases = {}, {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                module_aliases[a.asname or a.name.split(".")[0]] = a.name
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level > 0:  # relative import
                parts = module.split(".")
                b = parts[:len(parts) - node.level] if node.level < len(parts) else []
                if base:
                    b = b + base.split(".")
                target = ".".join(b)
            else:  # absolute import
                target = base
            for a in node.names:
                if a.name == "*":
                    continue
                from_imports[a.asname or a.name] = (target, a.name)
    return from_imports, module_aliases


def resolve_name(name, module, from_imports, symbols):
    cand = key(module, name)
    if cand in symbols:
        return cand
    if name in from_imports:
        tm, tn = from_imports[name]
        cand = key(tm, tn)
        if cand in symbols:
            return cand
    return None


def resolve_receiver(val, module, from_imports, classes):
    """If val is a class (same-file, imported, or a ``X()`` constructor) return its symbol key."""
    if isinstance(val, ast.Name):
        nm = val.id
    elif isinstance(val, ast.Call) and isinstance(val.func, ast.Name):
        nm = val.func.id
    else:
        return None
    if key(module, nm) in classes:
        return key(module, nm)
    if nm in from_imports:
        tm, tn = from_imports[nm]
        if key(tm, tn) in classes:
            return key(tm, tn)
    return None


def caller_key(call, parent, module):
    fn = nearest_ancestor(call, parent, FUNC)
    if fn is not None:
        return key(module, qualname_of(fn, parent))
    return key(module, MODULE_QN)


def callee_name_of(func):
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    try:
        return _u(func)
    except Exception:
        return "<expr>"


def collect_calls(tree, parent, module, from_imports, module_aliases, symbols, classes, out):
    for call in ast.walk(tree):
        if not isinstance(call, ast.Call):
            continue
        func, line = call.func, call.lineno
        cname, resolved = callee_name_of(func), None
        if isinstance(func, ast.Name):
            resolved = resolve_name(func.id, module, from_imports, symbols)
        elif isinstance(func, ast.Attribute):
            attr, val = func.attr, func.value
            cname = attr
            if isinstance(val, ast.Name) and val.id in ("self", "cls"):
                cls = nearest_ancestor(call, parent, (ast.ClassDef,))
                if cls is not None:
                    cand = key(module, qualname_of(cls, parent) + "." + attr)
                    resolved = cand if cand in symbols else None
            elif isinstance(val, ast.Name) and val.id in module_aliases:
                cand = key(module_aliases[val.id], attr)
                resolved = cand if cand in symbols else None
            else:
                recv = resolve_receiver(val, module, from_imports, classes)
                if recv:
                    m, rest = recv[3:].split(":", 1)
                    cand = key(m, rest + "." + attr)
                    resolved = cand if cand in symbols else None
        out.append({"caller_symbol_key": caller_key(call, parent, module),
                    "callee_symbol_key": resolved, "callee_name": cname,
                    "line": line, "external": 0 if resolved else 1})


def analyze(root):
    symbols, classes = set(), set()
    entries = []
    for path in find_py_files(root):  # phase 1: collect declarations, build the global table
        rel = os.path.relpath(path, root).replace(os.sep, "/")
        module = dotted_module(rel)
        tree, diags = parse_file(path)
        decls = [module_decl(module)]
        parent = None
        from_imports, module_aliases = {}, {}
        if tree is not None:
            parent = build_parent_map(tree)
            collect_declarations(tree, parent, module, decls)
            from_imports, module_aliases = imports_for(tree, module)
        for d in decls:
            symbols.add(d["symbol_key"])
            if d["kind"] == "class":
                classes.add(d["symbol_key"])
        decls.sort(key=lambda d: d["line"])
        entries.append({"path": rel, "module": module, "tree": tree, "diags": diags,
                        "decls": decls, "parent": parent,
                        "from_imports": from_imports, "module_aliases": module_aliases})
    for e in entries:  # phase 2: resolve calls against the completed project table
        calls = []
        if e["tree"] is not None:
            collect_calls(e["tree"], e["parent"], e["module"], e["from_imports"],
                          e["module_aliases"], symbols, classes, calls)
        calls.sort(key=lambda c: c["line"])
        e["calls"] = calls
    files = [{"path": e["path"], "declarations": e["decls"], "calls": e["calls"],
              "diagnostics": e["diags"]}
             for e in sorted(entries, key=lambda e: e["path"])]
    return {"files": files}


def main():
    p = argparse.ArgumentParser(description="Python code-inventory analyzer")
    sub = p.add_subparsers(dest="cmd", required=True)
    ap = sub.add_parser("analyze-project")
    ap.add_argument("--root", required=True, help="absolute project directory")
    args = p.parse_args()
    if args.cmd != "analyze-project":
        p.error("unknown command")
    print(json.dumps(analyze(os.path.abspath(args.root)), indent=2))


if __name__ == "__main__":
    main()
