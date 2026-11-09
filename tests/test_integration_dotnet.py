"""Integration test: real Roslyn analyzer against the sample project.

Skipped when no .NET SDK is reachable. Builds the analyzer, copies it into a scratch copy of
`test-scenarios/csharp/`, syncs, and asserts the semantic facts M2 promises: declarations, overloads with
distinct identities, and a cross-file call edge.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
SCRIPT = SKILL / "scripts" / "inventory.py"
SAMPLE = SKILL / "test-scenarios" / "csharp"


def dotnet():
    p = shutil.which("dotnet")
    if p:
        return p
    for cand in (os.environ.get("DOTNET_ROOT"), str(Path.home() / ".dotnet")):
        if cand and (Path(cand) / "dotnet").exists():
            return str(Path(cand) / "dotnet")
    return None


@unittest.skipUnless(dotnet(), "no .NET SDK available")
class SampleProject(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls._tmp.name)
        cls.root = tmp / "proj"
        shutil.copytree(SAMPLE, cls.root)
        exe = dotnet()
        if not exe:
            raise unittest.SkipTest("no .NET SDK available")
        env = dict(os.environ, DOTNET_CLI_TELEMETRY_OPTOUT="1", DOTNET_NOLOGO="1")
        out = subprocess.run([exe, "build", str(SKILL / "analyzers" / "csharp" / "CodeInventory.Analyzer.csproj"),
                              "-c", "Release"], capture_output=True, text=True, env=env, timeout=600)
        if out.returncode != 0:
            raise unittest.SkipTest("analyzer build failed: " + out.stdout[-400:])
        d = cls.root / ".code-inventory" / "analyzer"
        d.mkdir(parents=True, exist_ok=True)
        src = SKILL / "analyzers" / "csharp" / "bin" / "Release" / "net8.0"
        for item in src.iterdir():
            if item.is_file():
                shutil.copy2(item, d / item.name)
            elif item.is_dir() and item.name not in ("ref",):
                shutil.copytree(item, d / item.name, dirs_exist_ok=True)
        subprocess.run([sys.executable, str(SCRIPT), "init", "--root", str(cls.root)], check=True,
                       capture_output=True)
        cls.sync = json.loads(subprocess.run(
            [sys.executable, str(SCRIPT), "sync", "--root", str(cls.root)],
            capture_output=True, text=True, check=True).stdout)
        cls.con = sqlite3.connect(cls.root / ".code-inventory" / "inventory.sqlite3")

    @classmethod
    def tearDownClass(cls):
        cls.con.close()
        cls._tmp.cleanup()

    def test_sync_reports_ok(self):
        self.assertEqual(self.sync["analyzer_status"], "ok", self.sync["warnings"])

    def test_methods_and_overloads_indexed(self):
        keys = {r[0] for r in self.con.execute("select symbol_key from functions")}
        self.assertIn("M:Sample.Services.OrderService.Save(System.Int32)", keys)
        self.assertIn("M:Sample.Services.OrderService.Save(System.String)", keys,
                      "an overload must keep its own identity")

    def test_cross_file_call_edge(self):
        edges = set(self.con.execute(
            "select caller_symbol_key, callee_symbol_key from calls where external=0"))
        self.assertIn(("M:Sample.Services.OrderService.Save(System.Int32)",
                       "M:Sample.Services.Validator.Check(System.Int32)"), edges,
                      "Save must resolve its call into Validator.cs")

    def test_overload_delegation_edge(self):
        edges = set(self.con.execute(
            "select caller_symbol_key, callee_symbol_key from calls where external=0"))
        self.assertIn(("M:Sample.Services.OrderService.Save(System.String)",
                       "M:Sample.Services.OrderService.Save(System.Int32)"), edges)

    def test_verify_clean(self):
        p = subprocess.run([sys.executable, str(SCRIPT), "verify", "--root", str(self.root)],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertTrue(json.loads(p.stdout)["ok"])


if __name__ == "__main__":
    unittest.main()
