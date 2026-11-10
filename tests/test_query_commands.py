"""Query-command tests: `impact`, `delta` and `check`.

These are the three commands an agent leans on to avoid reading code it does not need, so they are
tested for the property that matters: the answer must be cheaper (and more precise) than opening the
files, and it must say so honestly when it cannot answer.

The Roslyn analyzer is not needed: a stub `dotnet` emits analyzer-shaped JSON, same trick as
`test_coordinator.py`.
"""
import json
import os
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
    for line_no, line in enumerate(src.splitlines(), start=1):
        if "CallsOther()" in line:
            calls.append({"caller_symbol_key": key, "callee_symbol_key": "M:Stub.Other.Run()",
                          "callee_name": "void Other.Run()", "line": line_no, "external": 0})
    print(json.dumps({"declarations": decls, "calls": calls, "diagnostics": []}))
''')


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.root = base / "proj"
        self.root.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(["git", "-C", str(self.root), "config", "user.email", "t@example.test"], check=True)
        subprocess.run(["git", "-C", str(self.root), "config", "user.name", "t"], check=True)
        self.bin = base / "bin"
        self.bin.mkdir()
        stub = self.bin / "dotnet"
        stub.write_text(STUB)
        stub.chmod(0o755)
        self.env = dict(os.environ, PATH=f"{self.bin}:{os.environ.get('PATH','')}")
        self.env.pop("DOTNET_ROOT", None)
        analyzer = self.root / ".code-inventory" / "analyzer"
        analyzer.mkdir(parents=True, exist_ok=True)
        (analyzer / "CodeInventory.Analyzer.dll").write_text("stub")

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, rel, text):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args],
                              capture_output=True, text=True, check=True).stdout.strip()

    def commit(self, message="wip"):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)

    def cli(self, *args, expect=0):
        p = subprocess.run([sys.executable, str(SCRIPT), *args, "--root", str(self.root)],
                           capture_output=True, text=True, env=self.env)
        self.assertEqual(p.returncode, expect, p.stdout + p.stderr)
        return p

    def sync(self, *extra):
        return json.loads(self.cli("sync", *extra).stdout)


class Check(Base):
    def test_clean_after_sync(self):
        self.write("src/A.cs", "class A { }")
        self.commit()
        self.sync()
        out = json.loads(self.cli("check").stdout)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["counts"], {"stale": 0, "deleted": 0, "never_indexed": 0})

    def test_tells_apart_stale_and_never_indexed(self):
        # The failure that made a whole map lie: the hook was never installed, so the document kept
        # describing older code and nothing complained. `check` is the alarm.
        self.write("src/A.cs", "class A { }")
        self.commit()
        self.sync()
        self.write("src/A.cs", "class A { void Extra() { } }")   # edited after the sync
        self.write("src/B.cs", "class B { }")                    # added after the sync
        out = json.loads(self.cli("check", expect=1).stdout)
        self.assertFalse(out["ok"])
        self.assertEqual(out["stale"], ["src/A.cs"])
        self.assertEqual(out["never_indexed"], ["src/B.cs"])

    def test_reports_a_file_deleted_on_disk(self):
        self.write("src/A.cs", "class A { }")
        self.write("src/B.cs", "class B { }")
        self.commit()
        self.sync()
        (self.root / "src" / "B.cs").unlink()
        out = json.loads(self.cli("check", expect=1).stdout)
        self.assertEqual(out["deleted_since_sync"], ["src/B.cs"])


class Impact(Base):
    def setUp(self):
        super().setUp()
        self.write("src/Other.cs", "class Other { }\nvoid Run() { }")
        self.write("src/Caller.cs", "class Caller { }\nvoid Run() { }\nCallsOther();")
        self.sync()

    def test_lists_callers_with_path_and_line(self):
        # Caller.cs calls Other.Run(): the impact of the callee is the answer worth having.
        out = self.cli("impact", "--symbol", "Stub.Other.Run").stdout
        self.assertIn("depth 1", out)
        self.assertIn("src/Caller.cs", out)
        self.assertIn("callers listed: 1", out)

    def test_says_so_when_nothing_calls_it(self):
        # An HTTP action legitimately has no code caller; the useful answer is "entry point", not "0".
        self.write("src/Entry.cs", "class Entry { }\nvoid Run() { }")
        self.sync()
        out = self.cli("impact", "--symbol", "Stub.Entry.Run").stdout
        self.assertIn("no callers in the index", out)
        self.assertIn("entry point", out)

    def test_ambiguous_name_lists_candidates_instead_of_guessing(self):
        out = self.cli("impact", "--symbol", "Run", expect=2).stdout
        self.assertIn("ambiguous", out)
        self.assertIn("Caller.Run", out)

    def test_unknown_symbol_exits_two(self):
        self.cli("impact", "--symbol", "NoSuchSymbolAnywhere", expect=2)

    def test_budget_truncates(self):
        out = self.cli("impact", "--symbol", "Stub.Other.Run", "--budget", "10").stdout
        self.assertIn("truncated at --budget", out)


class Delta(Base):
    def test_worktree_delta_names_the_change_and_its_outsiders(self):
        self.write("src/Callee.cs", "class Callee { }\nvoid Run() { }")
        self.write("src/Caller.cs", "class Caller { }\nvoid Run() { }\nCallsOther();")
        self.commit("base")
        self.sync()
        self.write("src/Callee.cs", "class Callee { }\nvoid Run() { }\n// touched\n")
        out = self.cli("delta", "--base", "HEAD", "--worktree").stdout
        self.assertIn("src/Callee.cs", out)
        self.assertIn("1 file(s) changed", out.splitlines()[0])

    def test_branch_delta_uses_three_dot_diff(self):
        self.write("src/A.cs", "class A { }\nvoid Run() { }")
        self.commit("base")
        self.sync()
        self.write("src/B.cs", "class B { }\nvoid Run() { }")
        self.commit("second")
        out = self.cli("delta", "--base", "HEAD~1").stdout
        self.assertIn("src/B.cs", out)
        self.assertNotIn("src/A.cs —", out)

    def test_unindexed_changes_are_listed_separately(self):
        self.write("src/A.cs", "class A { }\nvoid Run() { }")
        self.commit("base")
        self.sync()
        self.write("README.md", "docs only\n")
        out = self.cli("delta", "--base", "HEAD", "--worktree").stdout
        self.assertIn("not indexed", out)
        self.assertIn("README.md", out)


if __name__ == "__main__":
    unittest.main()
