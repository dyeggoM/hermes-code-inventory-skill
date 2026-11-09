import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import skill_updates  # noqa: E402


def quiet(func, *args, **kwargs):
    """Run the script's entry point without its human-readable output landing in test logs."""
    with redirect_stdout(io.StringIO()):
        return func(*args, **kwargs)


def run(cwd, *args):
    proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {proc.stderr}")
    return proc.stdout


class Upstream:
    """A tiny repository whose `origin` points at itself, so `fetch origin` works offline."""

    def __init__(self, base: Path):
        self.path = base / "repo"
        self.path.mkdir(parents=True)

    def init(self):
        run(self.path, "init", "-b", "main")
        run(self.path, "config", "user.email", "tests@example.com")
        run(self.path, "config", "user.name", "tests")
        run(self.path, "remote", "add", "origin", str(self.path))

    def write(self, rel: str, text: str):
        target = self.path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def commit(self, message: str):
        run(self.path, "add", "-A")
        run(self.path, "commit", "-m", message)
        return run(self.path, "rev-parse", "HEAD").strip()

    def remove(self, rel: str):
        (self.path / rel).unlink()

    def checkout(self, ref: str):
        run(self.path, "checkout", ref)

    def branch(self, name: str):
        run(self.path, "checkout", "-b", name)


class SkillUpdates(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.up = Upstream(self.base)
        self.up.init()
        self.up.write("SKILL.md", "name: demo\n")
        self.up.write("scripts/keep.py", "print('v1')\n")
        self.up.write("scripts/doomed.py", "print('gone in v2')\n")
        self.up.write("README.md", "readme\n")
        self.up.write(".gitignore", "__pycache__/\n")
        self.v1 = self.up.commit("v1")
        self.skill = self.base / "installed"
        self.skill.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def install_v1(self):
        code = quiet(skill_updates.main, ["--repo", str(self.up.path), "--skill", str(self.skill), "--apply"])
        self.assertEqual(code, 0)

    def check(self):
        return skill_updates.check(self.up.path, self.skill)

    def test_install_records_the_commit_and_skips_the_repo_gitignore(self):
        self.install_v1()
        marker = json.loads((self.skill / ".skill-install.json").read_text())
        self.assertEqual(marker["commit"], self.v1)
        self.assertIn("scripts/keep.py", marker["files"])
        self.assertFalse((self.skill / ".gitignore").exists())

    def test_up_to_date_after_install(self):
        self.install_v1()
        result = self.check()
        self.assertTrue(result["up_to_date"])
        self.assertEqual(result["changed"], {})
        self.assertEqual(quiet(skill_updates.main, ["--repo", str(self.up.path), "--skill", str(self.skill)]), 0)

    def test_reports_added_modified_and_deleted_files(self):
        self.install_v1()
        self.up.write("scripts/keep.py", "print('v2')\n")
        self.up.write("scripts/new.py", "print('new')\n")
        self.up.remove("scripts/doomed.py")
        self.up.commit("v2")

        result = self.check()
        self.assertFalse(result["up_to_date"])
        self.assertEqual(result["behind_commits"], 1)
        self.assertEqual(result["changed"],
                         {"scripts/keep.py": "modified",
                          "scripts/new.py": "added",
                          "scripts/doomed.py": "deleted"})
        self.assertEqual(quiet(skill_updates.main, ["--repo", str(self.up.path), "--skill", str(self.skill)]), 10)

    def test_apply_installs_the_new_tree_then_reports_up_to_date(self):
        self.install_v1()
        self.up.write("scripts/keep.py", "print('v2')\n")
        self.up.write("scripts/new.py", "print('new')\n")
        self.up.remove("scripts/doomed.py")
        head = self.up.commit("v2")

        self.assertEqual(quiet(skill_updates.main, ["--repo", str(self.up.path), "--skill",
                                                    str(self.skill), "--apply", "--json"]), 0)

        self.assertEqual((self.skill / "scripts/keep.py").read_text(), "print('v2')\n")
        self.assertEqual((self.skill / "scripts/new.py").read_text(), "print('new')\n")
        self.assertFalse((self.skill / "scripts/doomed.py").exists())
        self.assertEqual(json.loads((self.skill / ".skill-install.json").read_text())["commit"], head)
        self.assertTrue(self.check()["up_to_date"])

    def test_a_branch_install_reports_ahead_instead_of_offering_to_revert(self):
        self.up.write("scripts/keep.py", "print('v2')\n")
        self.up.write("scripts/branch_only.py", "print('branch')\n")
        head = self.up.commit("v2")
        self.assertEqual(quiet(skill_updates.main, ["--repo", str(self.up.path), "--skill",
                                                    str(self.skill), "--rev", head, "--apply"]), 0)

        result = skill_updates.check(self.up.path, self.skill, self.v1)
        self.assertEqual(result["status"], "ahead")
        self.assertEqual(result["ahead_commits"], 1)
        self.assertEqual(result["behind_commits"], 0)
        self.assertEqual(result["changed"], {}, "a branch install must not look like a revert")

    def test_diverged_install_is_reported_without_a_file_list(self):
        self.up.branch("side")
        self.up.write("scripts/side.py", "print('side')\n")
        side = self.up.commit("side work")
        self.up.checkout("main")
        self.up.write("README.md", "main moved on\n")
        self.up.commit("main work")

        self.assertEqual(quiet(skill_updates.main, ["--repo", str(self.up.path), "--skill",
                                                    str(self.skill), "--rev", side, "--apply"]), 0)
        result = skill_updates.check(self.up.path, self.skill, "main")
        self.assertEqual(result["status"], "diverged")
        self.assertEqual(result["changed"], {}, "a diverged install must not list a revert")

        result = skill_updates.check(self.up.path, self.skill, "main")
        self.assertEqual(quiet(skill_updates.main, ["--repo", str(self.up.path), "--skill",
                                                    str(self.skill), "--rev", "main"]), 13)
        with self.assertRaises(SystemExit):
            skill_updates.apply(self.up.path, self.skill, result, force=True)

    def test_local_edits_are_reported_and_block_apply(self):
        self.install_v1()
        (self.skill / "README.md").write_text("edited by hand\n", encoding="utf-8")

        result = self.check()
        self.assertIn("README.md", result["local_edits"])
        self.assertEqual(quiet(skill_updates.main, ["--repo", str(self.up.path), "--skill", str(self.skill)]), 11)
        with self.assertRaises(SystemExit):
            skill_updates.apply(self.up.path, self.skill, result, force=False)
        self.assertEqual((self.skill / "README.md").read_text(), "edited by hand\n")

    def test_without_a_marker_it_compares_files_against_main(self):
        # An installed copy that predates the marker: copy the files, no .skill-install.json.
        for rel in ("SKILL.md", "scripts/keep.py", "README.md", "scripts/doomed.py"):
            target = self.skill / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text((self.up.path / rel).read_text(), encoding="utf-8")
        self.up.write("scripts/keep.py", "print('v2')\n")
        self.up.commit("v2")

        result = self.check()
        self.assertEqual(result["installed_commit"], None)
        self.assertIn("scripts/keep.py", result["changed"])


if __name__ == "__main__":
    unittest.main()
