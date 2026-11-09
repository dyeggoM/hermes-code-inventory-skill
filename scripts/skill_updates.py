#!/usr/bin/env python3
"""Check an installed copy of this skill against the published repository, and update it on demand.

The skill is installed as plain files (no .git), so the install records where it came from in
`.skill-install.json`. This script answers one question — "is the installed copy behind main?" — and
only writes anything when called with `--apply`, which is the approval step.

Usage
    python3 scripts/skill_updates.py                      # check only
    python3 scripts/skill_updates.py --apply              # install the pending changes
    python3 scripts/skill_updates.py --json               # machine-readable result

Defaults: --repo $CODE_INVENTORY_REPO or ~/projects/code-inventory,
          --skill $CODE_INVENTORY_SKILL or ~/.hermes/skills/software-development/code-inventory

Exit codes: 0 up to date (or applied), 10 update available, 11 local edits would be overwritten,
2 error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

MARKER = ".skill-install.json"
SKIP_PARTS = {".git", "__pycache__", "node_modules", "bin", "obj", ".code-inventory"}


def git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout


def tracked_files(repo: Path, rev: str) -> list[str]:
    out = git(repo, "ls-tree", "-r", "--name-only", rev)
    return [line for line in out.splitlines() if line.strip()]


def blob_bytes(repo: Path, rev: str, path: str) -> bytes:
    proc = subprocess.run(["git", "-C", str(repo), "show", f"{rev}:{path}"], capture_output=True)
    if proc.returncode != 0:
        raise SystemExit(f"cannot read {path} at {rev}: {proc.stderr.decode().strip()}")
    return proc.stdout


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def install_targets(repo: Path, rev: str) -> dict[str, str]:
    """Tracked files that belong in an installed copy (the repo's own .gitignore is not one)."""
    return {p: digest(blob_bytes(repo, rev, p)) for p in tracked_files(repo, rev)
            if p != ".gitignore" and not set(Path(p).parts) & SKIP_PARTS}


def read_marker(skill: Path) -> dict:
    marker = skill / MARKER
    if not marker.is_file():
        return {}
    try:
        return json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def local_state(skill: Path) -> dict[str, str]:
    state = {}
    for path in skill.rglob("*"):
        if path.is_dir() or set(path.relative_to(skill).parts) & SKIP_PARTS:
            continue
        rel = path.relative_to(skill).as_posix()
        if rel == MARKER:
            continue
        state[rel] = digest(path.read_bytes())
    return state


def has_commit(repo: Path, rev: str) -> bool:
    proc = subprocess.run(["git", "-C", str(repo), "cat-file", "-e", f"{rev}^{{commit}}"],
                          capture_output=True)
    return proc.returncode == 0


def upstream_deleted(repo: Path, rev: str, path: str) -> bool:
    """True when `rev`'s history contains a commit that deleted `path`."""
    out = git(repo, "log", "--format=%H", "--max-count=1", "--diff-filter=D", rev, "--", path,
              check=False)
    return bool(out.strip())


def content_changes(repo: Path, rev: str, targets: dict[str, str],
                    current: dict[str, str]) -> dict[str, str]:
    """What the installed *files* are missing from `rev` — used when provenance is unusable.

    A file the installed copy has and `rev` does not is only removed when `rev`'s history shows an
    actual deletion: otherwise it is a file from the branch this copy was installed from, not
    something upstream dropped.
    """
    changed: dict[str, str] = {}
    for path, sha in targets.items():
        if current.get(path) != sha:
            changed[path] = "added" if path not in current else "modified"
    for path in current:
        if path in targets or path == MARKER:
            continue
        if upstream_deleted(repo, rev, path):
            changed[path] = "deleted"
    return changed


def check(repo: Path, skill: Path, rev: str = "origin/main") -> dict:
    """Compare the installed copy with `rev` (default: the published main)."""
    git(repo, "fetch", "--quiet", "origin")
    upstream = git(repo, "rev-parse", rev).strip()
    targets = install_targets(repo, upstream)
    marker = read_marker(skill)
    installed_commit = marker.get("commit")
    current = local_state(skill)

    changed: dict[str, str] = {}
    ahead = behind = 0
    same_tree = False
    status = "unmarked"
    if installed_commit and has_commit(repo, installed_commit):
        ahead = int(git(repo, "rev-list", "--count", f"{upstream}..{installed_commit}",
                        check=False).strip() or 0)
        behind = int(git(repo, "rev-list", "--count", f"{installed_commit}..{upstream}",
                        check=False).strip() or 0)
        if ahead and behind:
            # Neither commit contains the other: the recorded commit was rebased away, or the
            # install came from a branch that has moved on. The marker is provenance only — the
            # files on disk are what matters, so reconcile by content.
            same_tree = subprocess.run(
                ["git", "-C", str(repo), "diff", "--quiet", installed_commit, upstream],
                capture_output=True).returncode == 0
            status = "same" if same_tree else "stale"
            if not same_tree:
                changed = content_changes(repo, upstream, targets, current)
                status = "stale" if changed else "same"
        elif ahead:
            status = "ahead"
        elif behind:
            status = "behind"
            out = git(repo, "diff", "--name-status", "--no-renames",
                      f"{installed_commit}..{upstream}", check=False)
            for line in out.splitlines():
                fields = line.split("\t")
                if len(fields) < 2:
                    continue
                kind, path = fields[0], fields[-1]
                if path == ".gitignore" or set(Path(path).parts) & SKIP_PARTS:
                    continue
                changed[path] = {"A": "added", "M": "modified", "D": "deleted"}.get(kind[0], "changed")
        else:
            status = "same"
    else:
        changed = content_changes(repo, upstream, targets, current)
        status = "behind" if changed else "same"

    local_edits = {p: "modified locally" for p, sha in current.items()
                   if p in marker.get("files", {}) and marker["files"][p] != sha
                   and p not in changed}

    return {
        "repo": str(repo),
        "skill": str(skill),
        "rev": rev,
        "status": status,
        "same_tree": same_tree,
        "installed_commit": installed_commit,
        "upstream_commit": upstream,
        "behind_commits": behind,
        "ahead_commits": ahead,
        "changed": changed,
        "local_edits": local_edits,
        "up_to_date": not changed,
    }


def apply(repo: Path, skill: Path, result: dict, force: bool) -> dict:
    if result["local_edits"] and not force:
        raise SystemExit("refusing to overwrite local edits (use --force): "
                         + ", ".join(sorted(result["local_edits"])))
    upstream = result["upstream_commit"]
    written, removed = [], []
    for path, kind in result["changed"].items():
        target = skill / path
        if kind == "deleted":
            if target.exists():
                target.unlink()
                removed.append(path)
            continue
        data = blob_bytes(repo, upstream, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.read_bytes() == data:
            continue
        target.write_bytes(data)
        written.append(path)
    targets = install_targets(repo, upstream)
    marker = {
        "commit": upstream,
        "installed_at": datetime.now(timezone.utc).isoformat(),
        "files": targets,
    }
    (skill / MARKER).write_text(json.dumps(marker, indent=1) + "\n", encoding="utf-8")
    return {"written": written, "removed": removed, "commit": upstream}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Check or update an installed copy of this skill.")
    ap.add_argument("--repo", default=os.environ.get("CODE_INVENTORY_REPO",
                                                     os.path.expanduser("~/projects/code-inventory")))
    ap.add_argument("--skill", default=os.environ.get(
        "CODE_INVENTORY_SKILL", os.path.expanduser("~/.hermes/skills/software-development/code-inventory")))
    ap.add_argument("--rev", default=os.environ.get("CODE_INVENTORY_REF", "origin/main"),
                    help="git ref to compare against (default: origin/main)")
    ap.add_argument("--apply", action="store_true", help="install the pending changes")
    ap.add_argument("--force", action="store_true", help="overwrite files edited locally")
    ap.add_argument("--json", action="store_true", help="print the result as JSON")
    args = ap.parse_args(argv)

    repo, skill = Path(args.repo).expanduser(), Path(args.skill).expanduser()
    if not (repo / ".git").exists():
        print(f"no git clone at {repo}; clone the published skill first", file=sys.stderr)
        return 2
    if not skill.is_dir():
        print(f"no installed skill at {skill}", file=sys.stderr)
        return 2

    result = check(repo, skill, args.rev)
    applied = apply(repo, skill, result, args.force) if args.apply else None
    payload = dict(result, applied=applied)
    if args.json:
        print(json.dumps(payload, indent=1))
    else:
        if result["status"] == "ahead":
            print(f"installed copy is ahead of {args.rev} by {result['ahead_commits']} commit(s) "
                  f"({result['installed_commit'][:7]}) — a branch install, nothing to pull")
        elif result["status"] == "stale":
            print(f"{len(result['changed'])} file(s) differ from {args.rev} ({result['upstream_commit'][:7]}); "
                  f"the recorded commit {result['installed_commit'][:7]} is not in its history "
                  "(rebased install) — reconciling by file content")
            for path, kind in sorted(result["changed"].items()):
                print(f"  {kind:<9} {path}")
        elif result["up_to_date"]:
            print(f"up to date: {skill} matches {args.rev} ({result['upstream_commit'][:7]})")
            if result.get("same_tree") and result["installed_commit"] != result["upstream_commit"]:
                print("  same files, different history (rebased?) — --apply re-anchors the install")
        else:
            print(f"{len(result['changed'])} file(s) to update — {result['behind_commits']} commit(s) behind "
                  f"{args.rev} ({result['upstream_commit'][:7]})")
            for path, kind in sorted(result["changed"].items()):
                print(f"  {kind:<9} {path}")
        if result["local_edits"]:
            print("local edits (--force to overwrite):")
            for path in sorted(result["local_edits"]):
                print(f"  {path}")
        if applied:
            print(f"applied: {len(applied['written'])} written, {len(applied['removed'])} removed")
            print("re-run the test suite from the installed copy:")
            print(f"  cd {skill} && python3 -m unittest discover -s tests")
        elif not result["up_to_date"] and result["status"] != "ahead":
            print("review the list, then re-run with --apply")

    if applied:
        return 0
    if result["local_edits"]:
        return 11
    return 0 if result["up_to_date"] else 10


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
