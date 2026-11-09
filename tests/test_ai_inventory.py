"""Tests for scripts/ai_inventory.py — the LLM-facing renderer.

No analyzer or SDK needed: the tests create a real project skeleton (so doc comments, line counts
and type lines can be read from source), populate the coordinator's schema by hand, and drive the
script the way a user would.
"""
import importlib.util
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
INVENTORY = SKILL / "scripts" / "inventory.py"
RENDER = SKILL / "scripts" / "ai_inventory.py"


def load_module():
    spec = importlib.util.spec_from_file_location("ai_inventory", RENDER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "proj"
        (self.root / "app" / "services").mkdir(parents=True)
        (self.root / "app" / "web").mkdir(parents=True)
        self.write("app/services/svc.py", (
            "class Order:\n"
            "    pass\n"
            "\n"
            "def save(order):\n"
            '    """Persist one order."""\n'
            "    return 1\n"
        ))
        self.write("app/web/api.py", (
            "from app.services.svc import save\n"
            "\n"
            "\n"
            "def handler(request):\n"
            "    return save(request)\n"
        ))
        subprocess.run([sys.executable, str(INVENTORY), "init", "--root", str(self.root)],
                       check=True, capture_output=True)
        self.populate()

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, rel, text):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def db(self):
        return sqlite3.connect(self.root / ".code-inventory" / "inventory.sqlite3")

    def populate(self):
        con = self.db()
        con.execute("insert into nodes(id,parent_id,kind,name,path,symbol_key) "
                    "values (1,null,'project','proj','.',null)")
        con.execute("insert into nodes(id,parent_id,kind,name,path,symbol_key) "
                    "values (2,1,'file','svc.py','app/services/svc.py',null)")
        con.execute("insert into nodes(id,parent_id,kind,name,path,symbol_key) "
                    "values (3,2,'class','Order','app/services/svc.py','py:app.services.svc:Order')")
        con.execute("insert into nodes(id,parent_id,kind,name,path,symbol_key) "
                    "values (4,2,'function','save','app/services/svc.py','py:app.services.svc:save')")
        con.execute("insert into nodes(id,parent_id,kind,name,path,symbol_key) "
                    "values (5,1,'file','api.py','app/web/api.py',null)")
        con.execute("insert into nodes(id,parent_id,kind,name,path,symbol_key) "
                    "values (6,5,'function','handler','app/web/api.py','py:app.web.api:handler')")
        con.execute("insert into nodes(id,parent_id,kind,name,path,symbol_key) "
                    "values (7,2,'module','app.services.svc','app/services/svc.py',"
                    "'py:app.services.svc:<module>')")
        rows = [
            ("py:app.services.svc:save", 4, "save", "def save(order)", "app/services/svc.py", 4),
            ("py:app.web.api:handler", 6, "handler", "def handler(request)", "app/web/api.py", 4),
        ]
        for key, node_id, name, sig, path, line in rows:
            con.execute("insert into functions(symbol_key,node_id,name,signature,path,line,"
                        "description,description_origin,language) values (?,?,?,?,?,?,'','empty','py')",
                        (key, node_id, name, sig, path, line))
        con.execute("insert into calls(caller_symbol_key,callee_symbol_key,callee_name,path,line,external)"
                    " values ('py:app.web.api:handler','py:app.services.svc:save','save',"
                    "'app/web/api.py',5,0)")
        con.execute("insert into calls(caller_symbol_key,callee_symbol_key,callee_name,path,line,external)"
                    " values ('py:app.services.svc:save',null,'open','app/services/svc.py',6,1)")
        for rel in ("app/services/svc.py", "app/web/api.py"):
            con.execute("insert into file_state(path,sha256,indexed_at,semantic_status,language)"
                        " values (?,?,'2026-01-01T00:00:00','ok','py')", (rel, "x"))
        con.commit()
        con.close()

    def render(self, *args):
        p = subprocess.run([sys.executable, str(RENDER), "--root", str(self.root), *args],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout


class SingleDocument(Base):
    def test_document_has_symbols_impact_and_purpose(self):
        summary = json.loads(self.render())
        self.assertFalse(summary["split"])
        self.assertEqual(summary["symbols"], 3, "two functions plus the Order class")
        doc = (self.root / "AI_INVENTORY.md").read_text(encoding="utf-8")
        self.assertIn("save", doc)
        self.assertIn("Persist one order.", doc, "doc comment becomes the purpose")
        self.assertIn("## Impact", doc)
        self.assertIn("handler", doc.split("## Impact")[1], "the caller must appear in impact")

    def test_json_companion_is_written(self):
        self.render()
        payload = json.loads((self.root / "ai_inventory.json").read_text(encoding="utf-8"))
        self.assertEqual(len(payload["edges"]), 1)
        self.assertEqual(payload["unresolved"][0]["name"], "open")


class CommittedDocument(Base):
    def test_no_stamp_makes_the_document_byte_identical_across_runs(self):
        out = self.root / "AI_INVENTORY.md"
        self.render("--no-stamp", "--no-json")
        first = out.read_text(encoding="utf-8")
        self.assertNotIn("UTC", first.splitlines()[2],
                         "a committed inventory must not carry a timestamp")

        self.render("--no-stamp", "--no-json")
        self.assertEqual(first, out.read_text(encoding="utf-8"),
                         "re-rendering the same code must not change the file")

    def test_the_default_header_keeps_its_stamp(self):
        self.render("--no-json")
        header = (self.root / "AI_INVENTORY.md").read_text(encoding="utf-8").splitlines()[2]
        self.assertIn("UTC", header)
        self.assertIn("Generated", header)


class SplitMode(Base):
    def test_over_budget_writes_index_and_parts(self):
        summary = json.loads(self.render("--max-chars", "200"))
        self.assertTrue(summary["split"])
        self.assertGreaterEqual(summary["parts"], 2)
        index = (self.root / "AI_INVENTORY.md").read_text(encoding="utf-8")
        self.assertIn("## Modules", index)
        self.assertIn("inventory/", index)
        parts = sorted((self.root / "inventory").glob("*.md"))
        self.assertEqual(len(parts), summary["parts"])
        self.assertTrue(all(p.stat().st_size > 0 for p in parts))

    def test_parts_keep_cross_module_impact(self):
        self.render("--max-chars", "200")
        joined = "\n".join(p.read_text(encoding="utf-8")
                           for p in (self.root / "inventory").glob("*.md"))
        self.assertIn("app/web/api.py", joined,
                      "the part holding svc.save must still name its caller in app/web")


class ContextPack(Base):
    def test_context_returns_code_and_callers(self):
        out = self.render("--context", "save")
        self.assertIn("# Context: `save", out)
        self.assertIn("```", out)
        self.assertIn("## Callers", out)
        self.assertIn("app/web/api.py", out)

    def test_unknown_symbol_says_so(self):
        out = self.render("--context", "does_not_exist")
        self.assertIn("No indexed symbol matches", out)


class Grouping(unittest.TestCase):
    def test_group_of_uses_depth(self):
        mod = load_module()
        self.assertEqual(mod.group_of("app/services/svc.py", 2), "app/services")
        self.assertEqual(mod.group_of("app/services/svc.py", 1), "app")
        self.assertEqual(mod.group_of("main.py", 2), ".")

    def test_big_group_is_refined_per_file(self):
        mod = load_module()
        syms = {}
        for i in range(150):
            path = f"app/services/mod{i % 6}.py"
            syms[f"py:{path}:f{i}"] = {"key": f"py:{path}:f{i}", "path": path, "line": i + 1}
        groups = mod.build_groups(syms, depth=1, max_symbols=50)
        self.assertGreater(len(groups), 1)
        self.assertTrue(all(len(v) <= 50 for v in groups.values()))


if __name__ == "__main__":
    unittest.main()