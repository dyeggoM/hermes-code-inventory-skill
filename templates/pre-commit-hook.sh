#!/bin/sh
# Pre-commit: refresh the code inventory from the staged files, so the AI_INVENTORY.md a reviewer
# reads in the diff describes the code in that same commit.
#
# This file is a template: copy it into a repository and set ROOTS to that repository's layout.
#
# Install (once per clone):
#   mkdir -p .githooks && cp <skill>/templates/pre-commit-hook.sh .githooks/pre-commit
#   chmod +x .githooks/pre-commit && git config core.hooksPath .githooks
#
# Skip for one commit:  git commit --no-verify
#
# CODE_INVENTORY_HOME points at the skill directory; without it the hook warns and lets the commit
# through, because a missing local tool must never block work.
set -e

root=$(git rev-parse --show-toplevel)
inventory=${CODE_INVENTORY_HOME:-$HOME/.hermes/skills/software-development/code-inventory}

# "<relative root>:<lang>" pairs, one per line; "." is the repository root. Languages: cs, py, js.
#
# One index per root, and roots must not nest: the renderer merges every index under --root, so an
# overlapping pair counts the same symbol twice and inflates every fan-in number.
#
# Shape the list to the repository:
#   one-language service        ROOTS='.:py'
#   API plus its tests (C#)     ROOTS='src:cs
#                                      tests:cs'
#   web + backend + workers     ROOTS='web:js
#                                      backend:cs
#                                      services:py'
ROOTS='.:js'

if [ ! -f "$inventory/scripts/inventory.py" ]; then
  echo "pre-commit: code-inventory not found at $inventory"
  echo "            skipping the inventory refresh (set CODE_INVENTORY_HOME to enable it)"
  exit 0
fi

# SDKs installed by dotnet-install.sh are not on a fresh shell's PATH.
export DOTNET_ROOT="${DOTNET_ROOT:-$HOME/.dotnet}"
export PATH="$DOTNET_ROOT:$PATH"

# D included: a deleted or renamed file still needs the sync that drops its rows.
staged=$(git diff --cached --name-only --diff-filter=ACMRD)
[ -n "$staged" ] || exit 0

# Which languages the staged files touch, derived from their extensions — no per-project list to
# keep in sync with the analyzers.
touched=""
for path in $staged; do
  case "$path" in
    *.cs) lang=cs ;;
    *.py) lang=py ;;
    *.ts | *.tsx | *.js | *.jsx | *.mjs | *.cjs | *.vue) lang=js ;;
    *) continue ;;
  esac
  case " $touched " in *" $lang "*) ;; *) touched="$touched $lang" ;; esac
done

[ -n "$touched" ] || exit 0

echo "pre-commit: refreshing the code inventory"

# The index reads files on disk, so a partially staged commit embeds the unstaged edits. Say so
# instead of letting the document look like it describes only the staged diff.
if [ -n "$(git diff --name-only)" ]; then
  echo "pre-commit: note — the working tree has unstaged changes; the inventory reflects the files,"
  echo "            not just the staged diff"
fi

echo "$ROOTS" | while IFS=: read -r rel lang; do
  [ -n "$rel" ] || continue
  case " $touched " in
    *" $lang "*) ;;
    *) continue ;;
  esac
  python3 "$inventory/scripts/inventory.py" sync --root "$root/$rel" --lang "$lang" >/dev/null
done

# --no-stamp keeps the header stable: identical code renders byte-identical, so the commit carries
# only the inventory diff the code produced. The split flags keep the document usable by an agent:
# a small router index plus one file per module, instead of a single document nobody can afford to
# open. Change 40000 here if this repository needs bigger or smaller slices.
python3 "$inventory/scripts/ai_inventory.py" --root "$root" --no-stamp --no-json --max-chars 40000 --split auto >/dev/null
# A project may decide the map is a local aid and ignore it (`AI_INVENTORY.md` in .gitignore). Then
# `git add` refuses, and under `set -e` that would abort a commit the hook has no business blocking.
if ! git add AI_INVENTORY.md 2>/dev/null; then
  echo "pre-commit: AI_INVENTORY.md is ignored here — refreshed on disk, nothing to stage"
  exit 0
fi
# the module slices are part of the document: add the ones that exist and any that were removed
[ -d "$root/inventory" ] && git add "$root/inventory" 2>/dev/null || true
git add -u -- AI_INVENTORY.md inventory 2>/dev/null || true

echo "pre-commit: AI_INVENTORY.md updated"
