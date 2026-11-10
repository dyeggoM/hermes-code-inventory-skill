#!/bin/sh
# Pre-commit: refresh the code inventory from the staged files, so the AI_INVENTORY.md a reviewer
# reads in the diff describes the code in that same commit.
#
# Install (once per clone):
#   mkdir -p .githooks && cp <skill>/templates/pre-commit-hook.sh .githooks/pre-commit
#   chmod +x .githooks/pre-commit && git config core.hooksPath .githooks
#
# Skip for one commit:  git commit --no-verify
#
# Roots are project-specific — edit ROOTS below. CODE_INVENTORY_HOME points at the skill directory;
# without it the hook warns and lets the commit through, because a missing local tool must never
# block work.
set -e

root=$(git rev-parse --show-toplevel)
inventory=${CODE_INVENTORY_HOME:-$HOME/.hermes/skills/software-development/code-inventory}

# "<relative root>:<lang>" pairs. "." is the repository root.
ROOTS='.:js
IMCAPI:cs
Testing:cs'

if [ ! -f "$inventory/scripts/inventory.py" ]; then
  echo "pre-commit: code-inventory not found at $inventory"
  echo "            skipping the inventory refresh (set CODE_INVENTORY_HOME to enable it)"
  exit 0
fi

# SDKs installed with dotnet-install.sh are not on a fresh shell's PATH.
export DOTNET_ROOT="${DOTNET_ROOT:-$HOME/.dotnet}"
export PATH="$DOTNET_ROOT:$PATH"

# D included: a deleted or renamed file still needs the sync that drops its rows.
staged=$(git diff --cached --name-only --diff-filter=ACMRD)
[ -n "$staged" ] || exit 0

touch_js=0
touch_cs=0
for path in $staged; do
  case "$path" in
    *.ts | *.tsx | *.js | *.mjs | *.cjs | *.vue) touch_js=1 ;;
    *.cs) touch_cs=1 ;;
  esac
done

if [ "$touch_js" -eq 0 ] && [ "$touch_cs" -eq 0 ]; then
  exit 0
fi

echo "pre-commit: refreshing the code inventory"

# The index reads files on disk, so a partially staged commit embeds the unstaged edits. Say so
# instead of letting the document look like it describes only the staged diff.
if [ -n "$(git diff --name-only)" ]; then
  echo "pre-commit: note — the working tree has unstaged changes; the inventory reflects the files,"
  echo "            not just the staged diff"
fi

echo "$ROOTS" | while IFS=: read -r rel lang; do
  [ -n "$rel" ] || continue
  case "$lang" in
    js) [ "$touch_js" -eq 1 ] || continue ;;
    cs) [ "$touch_cs" -eq 1 ] || continue ;;
  esac
  python3 "$inventory/scripts/inventory.py" sync --root "$root/$rel" --lang "$lang" >/dev/null
done

# --no-stamp keeps the header stable: identical code renders byte-identical, so the commit carries
# only the inventory diff the code produced. The split flags keep the document usable by an agent:
# a small router index plus one file per module, instead of a single document nobody can afford to
# open. Change 40000 here if this repository needs bigger or smaller slices.
python3 "$inventory/scripts/ai_inventory.py" --root "$root" --no-stamp --no-json --max-chars 40000 --split auto >/dev/null
git add AI_INVENTORY.md
# the module slices are part of the document: add the ones that exist and any that were removed
[ -d "$root/inventory" ] && git add "$root/inventory" 2>/dev/null || true
git add -u -- AI_INVENTORY.md inventory 2>/dev/null || true

echo "pre-commit: AI_INVENTORY.md updated"
