"""JS/TS/Vue/Angular analyzer integration: skipped until analyzers/js is installed and built."""
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
SCRIPT = SKILL / "scripts" / "inventory.py"
JS_DIR = SKILL / "analyzers" / "js"
ANALYZER = JS_DIR / "analyze.js"
FIXTURE = SKILL / "test-scenarios" / "js"


def node():
    p = shutil.which("node")
    if p:
        return p
    base = Path.home() / ".hermes" / "tools"
    if base.is_dir():
        for d in sorted(base.glob("node-*"), reverse=True):
            cand = d / "bin" / "node"
            if cand.exists():
                return str(cand)
    return None


ready = ANALYZER.exists() and node() and (JS_DIR / "node_modules").is_dir()


@unittest.skipUnless(ready, "js analyzer or its node_modules not installed")
class JsFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name) / "proj"
        shutil.copytree(FIXTURE, cls.root)
        subprocess.run([sys.executable, str(SCRIPT), "init", "--root", str(cls.root)],
                       check=True, capture_output=True)
        cls.sync = json.loads(subprocess.run(
            [sys.executable, str(SCRIPT), "sync", "--root", str(cls.root), "--lang", "js"],
            capture_output=True, text=True, check=True).stdout)
        cls.con = sqlite3.connect(cls.root / ".code-inventory" / "inventory.sqlite3")

    @classmethod
    def tearDownClass(cls):
        cls.con.close()
        cls._tmp.cleanup()

    def test_sync_ok(self):
        self.assertEqual(self.sync["analyzer_status"], "ok", self.sync["warnings"])

    def test_all_four_files_indexed(self):
        rows = dict(self.con.execute("select path, semantic_status from file_state"))
        expected = {"src/util/format.ts", "src/components/Button.jsx",
                    "src/app/hero.component.ts", "src/views/Card.vue"}
        self.assertEqual(set(rows), expected)
        self.assertTrue(all(v == "ok" for v in rows.values()), rows)

    def test_react_cross_file_edge(self):
        edges = set(self.con.execute("select caller_symbol_key, callee_symbol_key from calls"))
        self.assertIn(("js:src/components/Button:Button", "js:src/util/format:formatDate"), edges)

    def test_vue_and_angular_components_present(self):
        keys = {r[0] for r in self.con.execute("select symbol_key from functions")}
        self.assertTrue(any(k.startswith("js:src/views/Card") for k in keys), keys)
        self.assertIn("js:src/app/hero.component:HeroComponent.ngOnInit", keys)

    def test_angular_method_calls_resolved(self):
        edges = set(self.con.execute("select caller_symbol_key, callee_symbol_key from calls"))
        self.assertIn(("js:src/app/hero.component:HeroComponent.ngOnInit",
                       "js:src/util/format:formatDate"), edges)

    def test_verify_clean(self):
        p = subprocess.run([sys.executable, str(SCRIPT), "verify", "--root", str(self.root)],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout)



@unittest.skipUnless(ready, "js analyzer or its node_modules not installed")
class JsSemanticStatus(unittest.TestCase):
    """A file the analyzer could not parse must never be reported as ok.

    v0.4 applied the (partial) analyzer output and marked the file ok, so the warning
    appeared once and the next sync certified an index that did not contain the file.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "proj"
        (self.root / "src").mkdir(parents=True)
        (self.root / "src" / "ok.ts").write_text(
            "export function fine(x: number) { return x + 1; }\n"
            "export function caller() { return fine(1); }\n")
        (self.root / "src" / "broken.ts").write_text("export function oops(  { return 1\n")
        subprocess.run([sys.executable, str(SCRIPT), "init", "--root", str(self.root)],
                       check=True, capture_output=True)

    def tearDown(self):
        self._tmp.cleanup()

    def sync(self):
        return json.loads(subprocess.run(
            [sys.executable, str(SCRIPT), "sync", "--root", str(self.root), "--lang", "js"],
            capture_output=True, text=True, check=True).stdout)

    def status(self):
        con = sqlite3.connect(self.root / ".code-inventory" / "inventory.sqlite3")
        try:
            return dict(con.execute("select path, semantic_status from file_state"))
        finally:
            con.close()

    def test_parse_failure_is_not_ok(self):
        first = self.sync()
        self.assertEqual(first["analyzer_status"], "partial", first["warnings"])
        self.assertEqual(self.status()["src/broken.ts"], "failed")

    def test_parse_failure_stays_visible_on_next_sync(self):
        self.sync()
        second = self.sync()
        self.assertEqual(second["analyzer_status"], "partial", second["warnings"])
        self.assertTrue(any("broken.ts" in w for w in second["warnings"]), second["warnings"])
        self.assertEqual(self.status()["src/broken.ts"], "failed")

    def test_verify_lists_file_without_semantics(self):
        self.sync()
        out = json.loads(subprocess.run(
            [sys.executable, str(SCRIPT), "verify", "--root", str(self.root)],
            capture_output=True, text=True).stdout)
        self.assertIn("src/broken.ts", out["files_without_semantics"])

if __name__ == "__main__":
    unittest.main()
