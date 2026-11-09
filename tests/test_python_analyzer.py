"""Python analyzer integration: skipped until analyzers/python/python_analyzer.py exists."""
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
ANALYZER = SKILL / "analyzers" / "python" / "python_analyzer.py"
FIXTURE = SKILL / "test-scenarios" / "python"


@unittest.skipUnless(ANALYZER.exists(), "python analyzer not installed")
class PythonFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name) / "proj"
        shutil.copytree(FIXTURE, cls.root)
        subprocess.run([sys.executable, str(SCRIPT), "init", "--root", str(cls.root)],
                       check=True, capture_output=True)
        cls.sync = json.loads(subprocess.run(
            [sys.executable, str(SCRIPT), "sync", "--root", str(cls.root), "--lang", "py"],
            capture_output=True, text=True, check=True).stdout)
        cls.con = sqlite3.connect(cls.root / ".code-inventory" / "inventory.sqlite3")

    @classmethod
    def tearDownClass(cls):
        cls.con.close()
        cls._tmp.cleanup()

    def test_sync_ok(self):
        self.assertEqual(self.sync["analyzer_status"], "ok", self.sync["warnings"])

    def test_every_python_file_semantically_indexed(self):
        rows = dict(self.con.execute("select path, semantic_status from file_state"))
        self.assertEqual(set(rows), {"app/__init__.py", "app/service.py", "app/validator.py", "main.py"})
        self.assertTrue(all(v == "ok" for v in rows.values()), rows)

    def test_methods_indexed_with_language(self):
        rows = list(self.con.execute("select symbol_key, language from functions"))
        keys = {r[0] for r in rows}
        self.assertIn("py:app.service:Service.save", keys)
        self.assertIn("py:app.service:Service._persist", keys)
        self.assertIn("py:app.validator:check", keys)
        self.assertTrue(all(r[1] == "py" for r in rows))

    def test_cross_file_edge_resolved(self):
        edges = set(self.con.execute("select caller_symbol_key, callee_symbol_key from calls"))
        self.assertIn(("py:app.service:Service.save", "py:app.validator:check"), edges)

    def test_same_file_self_call_resolved(self):
        edges = set(self.con.execute("select caller_symbol_key, callee_symbol_key from calls"))
        self.assertIn(("py:app.service:Service.save", "py:app.service:Service._persist"), edges)

    def test_verify_clean(self):
        p = subprocess.run([sys.executable, str(SCRIPT), "verify", "--root", str(self.root)],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout)


if __name__ == "__main__":
    unittest.main()
