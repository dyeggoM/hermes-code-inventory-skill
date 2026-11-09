"""Coordinator tests.

The Roslyn analyzer is not required: these tests put a stub `dotnet` on PATH that emits
analyzer-shaped JSON, so the coordinator's own logic (hashing, hierarchy, description
preservation, transactional refresh, semantic status, call cleanup) is tested deterministically.
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "inventory.py"

STUB = textwrap.dedent('''\
    #!/usr/bin/env python3
    import json, os, sys
    args = sys.argv[1:]
    if os.environ.get("FAKE_DOTNET_FAIL"):
        sys.stderr.write("stub analyzer failure\\n")
        sys.exit(3)
    if "--file" not in args:
        sys.exit(2)
    f = args[args.index("--file") + 1]
    stem = os.path.basename(f).rsplit(".", 1)[0]
    key = "M:Stub.%s.Run()" % stem
    decls = [
        {"kind": "type", "name": stem, "symbol_key": "T:Stub.%s" % stem,
         "parent_symbol_key": None, "signature": stem, "line": 1},
        {"kind": "method", "name": "Run", "symbol_key": key,
         "parent_symbol_key": "T:Stub.%s" % stem,
         "signature": "void %s.Run()" % stem, "line": 2},
    ]
    calls = []
    try:
        src = open(f, encoding="utf-8").read()
    except OSError:
        src = ""
    if "Callee()" in src:
        calls.append({"caller_symbol_key": key, "callee_symbol_key": "M:Stub.Callee.Run()",
                      "callee_name": "void Callee.Run()", "line": 3, "external": 0})
    print(json.dumps({"declarations": decls, "calls": calls, "diagnostics": []}))
''')


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "proj"
        self.root.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.bin = Path(self._tmp.name) / "bin"
        self.bin.mkdir()
        stub = self.bin / "dotnet"
        stub.write_text(STUB)
        stub.chmod(0o755)
        self.env = dict(os.environ, PATH=f"{self.bin}:{os.environ.get('PATH','')}")
        self.env.pop("DOTNET_ROOT", None)
        self.install_analyzer()

    def tearDown(self):
        self._tmp.cleanup()

    def install_analyzer(self):
        d = self.root / ".code-inventory" / "analyzer"
        d.mkdir(parents=True, exist_ok=True)
        (d / "CodeInventory.Analyzer.dll").write_text("stub")

    def write(self, rel, text):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def cli(self, *args, expect=0):
        p = subprocess.run([sys.executable, str(SCRIPT), *args, "--root", str(self.root)],
                           capture_output=True, text=True, env=self.env)
        self.assertEqual(p.returncode, expect, p.stderr)
        return p

    def sync(self, *extra, expect=0):
        return json.loads(self.cli("sync", *extra, expect=expect).stdout)

    def db(self):
        return sqlite3.connect(self.root / ".code-inventory" / "inventory.sqlite3")

    def q(self, sql, *params):
        con = self.db()
        try:
            return con.execute(sql, params).fetchall()
        finally:
            con.close()

    def init(self):
        self.cli("init")


class Discovery(Base):
    def test_generated_tooling_dirs_are_not_indexed(self):
        # Quasar/Nuxt/Vite scaffolds write generated sources into the project; they are build
        # output, not code to reason about, and they inflate every count in the rendered document.
        self.write("src/A.cs", "class A {}")
        self.write(".quasar/dev-spa/app.js", "console.log('generated')")
        self.write(".nuxt/app.js", "console.log('generated')")
        self.write("out/main.js", "console.log('generated')")
        self.init()
        out = self.sync()
        self.assertEqual(out["discovered_files"], 1,
                         "generated tooling directories must not be discovered")

    def test_nested_file_and_incremental(self):
        self.write("src/a/Foo.cs", "class Foo {}")
        self.init()
        out = self.sync()
        self.assertEqual(out["discovered_files"], 1)
        self.assertEqual(self.q("select count(*) from nodes where kind='folder'")[0][0], 2)
        self.write("src/a/Foo.cs", "class Foo { void Bar() {} }")
        out = self.sync()
        self.assertEqual(out["changed_files"], 1)


class FunctionIndexing(Base):
    def test_functions_and_calls_are_recorded(self):
        self.write("Services/Callee.cs", "class Callee { void Run() {} }")
        self.write("Services/Caller.cs", "class Caller { void Run() { Callee(); } }")
        self.init()
        self.sync()
        keys = [r[0] for r in self.q("select symbol_key from functions order by symbol_key")]
        self.assertIn("M:Stub.Callee.Run()", keys)
        self.assertIn("M:Stub.Caller.Run()", keys)
        edges = self.q("select caller_symbol_key, callee_symbol_key from calls")
        self.assertIn(("M:Stub.Caller.Run()", "M:Stub.Callee.Run()"), edges)


class Descriptions(Base):
    def test_manual_description_survives_resync(self):
        self.write("A.cs", "class A { void Run() { } }")
        self.init()
        self.sync()
        con = self.db()
        con.execute("update functions set description=?, description_origin='manual' "
                    "where symbol_key=?", ("Saves an order to the database.", "M:Stub.A.Run()"))
        con.commit()
        con.close()
        self.write("A.cs", "class A { void Run() { int x = 1; } }")
        self.sync()
        row = self.q("select description, description_origin from functions where symbol_key=?",
                     "M:Stub.A.Run()")
        self.assertEqual(row[0][0], "Saves an order to the database.")
        self.assertEqual(row[0][1], "manual")


class AnalyzerFailure(Base):
    def test_failed_analyzer_preserves_previous_data(self):
        self.write("A.cs", "class A { void Run() { } }")
        self.init()
        self.sync()
        self.assertEqual(len(self.q("select * from functions")), 1)
        self.write("A.cs", "class A { void Run() { int y = 2; } }")
        self.env["FAKE_DOTNET_FAIL"] = "1"
        out = self.sync()
        self.assertEqual(out["analyzer_status"], "partial")
        self.assertEqual(len(self.q("select * from functions")), 1,
                         "a failed analysis must not wipe the last good rows")
        self.assertNotEqual(self.q("select semantic_status from file_state where path='A.cs'")[0][0],
                            "ok")

    def test_missing_analyzer_is_not_marked_indexed(self):
        self.write("A.cs", "class A { void Run() { } }")
        self.init()
        (self.root / ".code-inventory/analyzer/CodeInventory.Analyzer.dll").unlink()
        self.sync()
        self.assertNotEqual(self.q("select semantic_status from file_state where path='A.cs'")[0][0],
                            "ok", "a file indexed without semantics must not look complete")
        self.install_analyzer()
        out = self.sync()  # file content unchanged, but semantics are still missing
        self.assertEqual(out["analyzer_status"], "ok")
        self.assertEqual(self.q("select semantic_status from file_state where path='A.cs'")[0][0],
                         "ok", "installing the analyzer must trigger a retry of incomplete files")
        self.assertEqual(len(self.q("select * from functions")), 1)

    def test_retry_does_not_recount_as_content_change(self):
        self.write("A.cs", "class A { void Run() { } }")
        self.init()
        (self.root / ".code-inventory/analyzer/CodeInventory.Analyzer.dll").unlink()
        self.sync()
        self.install_analyzer()
        second = self.sync()
        self.assertEqual(second["changed_files"], 0)
        self.assertEqual(second["analyzer_status"], "ok")


class CallIntegrity(Base):
    def test_deleted_file_removes_dangling_calls(self):
        self.write("Callee.cs", "class Callee { void Run() {} }")
        self.write("Caller.cs", "class Caller { void Run() { Callee(); } }")
        self.init()
        self.sync()
        self.assertEqual(len(self.q("select * from calls")), 1)
        (self.root / "Callee.cs").unlink()
        self.sync()
        self.assertEqual(self.q("select count(*) from calls where callee_symbol_key='M:Stub.Callee.Run()'")[0][0], 0)
        p = self.cli("verify")
        self.assertIn('"ok": true', p.stdout)


if __name__ == "__main__":
    unittest.main()
