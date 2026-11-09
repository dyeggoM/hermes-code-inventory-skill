---
name: code-inventory
description: "Use when indexing code. C#, Python, JS/TS call graphs."
version: 0.5.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [csharp, python, javascript, typescript, react, vue, angular, roslyn, code-index, sqlite, call-graph]
    related_skills: [codebase-inspection, systematic-debugging]
---

# Code inventory (C#, Python, JS/TS)

## When to Use

Load this when the task needs structural facts about a codebase rather than a grep: who calls
this function, what a function calls, which classes exist in which module, what changed since the
last index, or a machine-readable inventory of a repo. Useful before a refactor (impact radius),
during debugging (call path to the failure), for onboarding onto an unfamiliar repository, and —
with `scripts/ai_inventory.py` — to give an LLM a map of the code instead of the code itself.

Supported: C# (Roslyn/MSBuild), Python (stdlib `ast`), JavaScript/TypeScript including React, Vue
SFCs and Angular (`@babel/parser`).

Do not load it for: languages outside that list, a one-off "where is X defined" (use
`search_files`), or a repo you only need line counts from (use `codebase-inspection`).

One coordinator, one analyzer per language. The coordinator owns storage, hashing, hierarchy and
incremental policy; each analyzer parses its language and resolves cross-file edges. The boundary
is `references/analyzer-contract.md` — read it before adding a language.

## Workflow

1. `python3 scripts/inventory.py init --root /path/to/project`
   The index is not source: add `<project>/.code-inventory/` to that project's `.gitignore`, or its
   `git status` fills with database files. Whether `AI_INVENTORY.md` (and `inventory/` in split
   mode) is ignored is a decision: ignore it when the map is a local aid, commit it when reviewers
   and other agents should read it — and then refresh it before every commit (see *Committing the
   document*).
2. Make each language's analyzer available (only the ones you need):
   - *C#* — build once and copy into the project:
     `dotnet build analyzers/csharp/CodeInventory.Analyzer.csproj -c Release` then
     `mkdir -p <project>/.code-inventory/analyzer && cp -a analyzers/csharp/bin/Release/net8.0/. <project>/.code-inventory/analyzer/`
     The project keeps its own copy of the DLL: after rebuilding, re-copy and `sync --lang cs --full`
     once, or it silently re-indexes with the previous analyzer.
   - *Python* — nothing to install; `analyzers/python/python_analyzer.py` runs on the stdlib.
   - *JS/TS/Vue* — one-time `npm install --prefix analyzers/js` (pulls `@babel/parser`, a few MB).
3. `python3 scripts/inventory.py sync --root /path/to/project` (add `--lang cs|py|js` to limit)
4. Query: `status`, `search --query X [--lang]`, `callers --symbol K`, `callees --symbol K`,
   `verify`, `export --output inventory.json`.
5. When the consumer is an LLM, render the reference document instead of handing it the SQLite:
   `python3 scripts/ai_inventory.py --root /path/to/project` → `AI_INVENTORY.md` (one file, ~1 line
   per symbol) plus `ai_inventory.json`. See *AI-facing reference* below.

Missing analyzers are not an error: those files keep `semantic_status='unavailable'` and are
retried automatically once the analyzer appears.

## AI-facing reference

`scripts/ai_inventory.py` merges every index under `--root` and writes a document an LLM can work
from instead of the repository:

- **one line per symbol**, grouped by file: `Lnn`, kind, qualified name, parameters/return,
  fields or state, doc-comment purpose, `→ n (callees)` and `← n` (callers)
- **call graph** and **impact** (reverse edges, highest fan-in first) so a signature change can be
  traced to every caller; module-level and object-scope calls are kept and labelled `<module>`
- **HTTP routes** from C# attributes, including endpoints inherited from a generic base controller
- **framework calls** (unresolved targets) per file — dependency info, never impact
- **coverage** block: index states, resolved-edge ratio, per-language blind spots, analyzer warnings

`--context SYMBOL` prints a pack instead of the document: the symbol's code block plus its callers
and callees by `path:line` — that is the unit to hand a model together with the task. `--budget`
caps it.

### Committing the document

`--no-stamp` drops the timestamp/commit line from the header, so rendering the same code twice
produces a byte-identical file. Without it every re-render diffs (`Generated 2026-… from <sha>`), and
a refresh-before-commit can never leave the tree clean.

When `AI_INVENTORY.md` is committed, refresh it from the **staged** files in a pre-commit hook, so
the document in a diff describes the code in that same diff. `templates/pre-commit-hook.sh` is a
working hook — copy it to `.githooks/pre-commit`, `chmod +x`, and
`git config core.hooksPath .githooks` once per clone. What it has to get right:

- **Scope the sync to the staged languages** (`*.ts/*.vue/*.js` → the JS root, `*.cs` → each C#
  root). Syncing everything on every commit pays the Roslyn cost (~5 s per file) for a docs edit.
- **`git add` the rendered file.** A hook that regenerates without staging leaves the refresh out of
  the commit and the worktree dirty.
- **Pass `--no-stamp`**, or the header moves on every run and the commit is never clean.
- **Use `--diff-filter=ACMRD`** so a deleted or renamed file also triggers the sync that drops its
  rows.
- **Missing skill → warn and `exit 0`.** A clone without the tool must still be able to commit. The
  per-commit escape is `git commit --no-verify`; do not invent a second bypass switch.
- **Export `DOTNET_ROOT`/`PATH` inside the hook.** SDKs installed with `dotnet-install.sh` are not on
  a fresh shell's PATH, and the hook runs in whatever environment the commit was typed in.
- **Say when the working tree is dirty.** The index is built from files on disk, so a partially
  staged commit embeds the unstaged edits and the document ends up slightly ahead of the commit that
  carries it. Print a one-line warning; rendering the index itself (a `git checkout-index` copy in a
  temp directory) is only worth it where partial staging is the norm.

Expect the document to move on every code commit — line numbers shift, so a one-line edit rewrites a
file's entries. That is the cost of committing it; a team that finds the noise loud should refresh on
release instead and say so.

**Large repos split automatically.** When the single document would exceed `--max-chars`, the same
path becomes a router index and each module gets `inventory/<module>.md`: over `--depth 2` path
segments, refined by sub-folder, then per file whenever a group still holds more than ~120 symbols.
Parts are rendered with the full symbol map as context, so a symbol's *Impact* line still names the
callers that live in other modules. Read the index, then one part — never the whole set.

Rules the generator follows, keep them if you change it:

- **Never invent a purpose.** Only comments in the source are used — C# `///`, JS `/** */` or `//`,
  Python PEP 257 docstrings — and a blank stays blank.
- **Never fill an edge.** Impact comes from resolved symbol keys only.
- **Keep the entry point stable.** `AI_INVENTORY.md` is the document for a small repo and the
  index for a large one; `--split never` forces one file, `--max-chars` decides.
- Type declarations live in `nodes`, not `functions`: `ai_inventory.py` pulls them from `nodes` and
  resolves their line from source, so do not assume a symbol is missing just because it has no
  `functions` row.
- **Derive the language from the symbol key, not from a cs/js boolean.** Three namespaces coexist
  (`py:`, `js:`, Roslyn doc IDs like `M:`); a two-way test silently labels every Python symbol as
  JS/TS/Vue in the header and in the split index.
- **External edges may carry a callee key that is not in `functions`.** Filter by
  `callee_symbol_key IS NOT NULL` before listing callees in `--context`, or the pack crashes on a
  framework call while the document path keeps working.

## Symbol keys

| Language | Format | Example |
|---|---|---|
| C# | Roslyn documentation IDs | `M:Sample.Services.OrderService.Save(System.Int32)` |
| Python | `py:<dotted module>:<qualname>` | `py:app.service:Service.save` |
| JS/TS/Vue | `js:<path without ext>:<qualname>` | `js:src/components/Button:Button` |

They are stable while the qualified name is unchanged, which is what lets manual descriptions
survive a rescan.

## Prerequisites

- Python 3.10+ for the coordinator. No third-party packages.
- C#: a .NET SDK **matching or newer than the project's target framework**. `dotnet` is looked up on
  `PATH`, then `$DOTNET_ROOT`, then `~/.dotnet`; `references/setup-dotnet.md` has the no-root install.
  A net9.0 project analysed with the 8.0 SDK loads with **zero metadata references**: declarations
  still appear, but every call into the framework stays unresolved and the analyzer records a
  `zero metadata references` warning. Install the matching SDK and `sync --full` rather than
  interpreting that as "the code has no calls".
- JS/TS: Node 18+ and `npm install --prefix analyzers/js`. Without it those files stay `unavailable`
  with a clear warning rather than silently indexing nothing.

## Reusing an index from an earlier session

Do not trust a map you did not just re-check. On returning to an indexed project:

1. `sync` on each root you rely on. `changed_files: 0` **and** `retried_files: 0` is the evidence the
   rows match the working tree; anything else means re-read the affected symbols before advising.
2. `verify` — `ok: true` with no orphans or dangling edges. A `files_without_semantics` list is not
   a failure, but those files are unindexed: say so instead of counting them as covered.
3. For C#, compare the project-local `analyzer/CodeInventory.Analyzer.dll` against the skill's
   `analyzers/csharp/Program.cs`. An older DLL still reports `analyzer_status: ok` while resolving
   less, so a rebuild means copy + `sync --lang cs --full`.

## Incremental sync

Every discovered source file of a supported language is hashed, including staged, unstaged and
untracked content. Unchanged files are skipped, changed files re-analyzed, deleted files removed.
Git is never modified. `sync --full` re-analyzes everything; `--lang` scopes one language.

## Cost

- *Python and JS/TS*: the analyzer runs **once per language per sync** and resolves the whole
  project in one pass, so hundreds of files take seconds.
- *C#*: the Roslyn analyzer opens the project **per file**, ~5 s each. A 200-file solution is
  15-20 minutes on first sync; incremental syncs only pay for what changed. Say so before starting.

## Descriptions

`functions.description` and `description_origin` are the AI-facing layer. A rescan captures the
existing description **by symbol key before it replaces the file's rows**, so manual text survives
every re-sync. Mark generated summaries `ai`; a user correction makes it `manual`. Nothing in `sync`
rewrites a description.

## Failure behaviour

A file is only marked `semantic_status='ok'` after its analyzer output has been applied. Everything
else leaves the previous rows intact and is retried by the next `sync` without needing a content
change:

- analyzer or runtime missing → hash stored, status `unavailable`
- analyzer exits non-zero, or emits JSON that cannot be parsed → status `failed`, diagnostics row,
  last good rows kept
- the apply step itself fails → rolled back per file (one transaction per file)

`sync` reports `changed_files` (content changed) and `retried_files` (content identical, semantics
were incomplete) separately, so a retry does not masquerade as an edit.

`verify` fails on real corruption only: orphan functions, call edges whose caller no longer exists,
and non-external edges pointing at symbols that are gone. Files still missing semantics are listed
under `files_without_semantics` rather than failing the run.

## Never claim more coverage than you have

- `analyzer_status` is `ok`, `partial` or `unavailable`. `partial` means at least one file failed;
  the warnings name it. Report that instead of "the inventory is complete".
- Unresolved calls keep `callee_symbol_key` NULL and `external: 1`. Never fill one in by hand.
- *C#*: no top-level statements, `new`-constructor edges, property-access edges, fields/events as
  callables, reflection, `dynamic`, source generators or linked files. Without a loaded MSBuild
  project there is no semantic model at all — structure only. A call through an interface binds to
  the **interface member**, not to the implementing class.
- *Python*: dynamic attribute access, `getattr`, monkey-patching, `eval`, star-imports and
  decorator-rewritten signatures are not resolved. Unresolved receivers stay external.
- *JS/TS*: member calls on unknown receivers, computed property access and re-exports through
  barrels are unresolved. Object-literal methods (`export const svc = { getAll() {} }`) are indexed
  as `object` + `method` children, and non-relative imports are matched by alias tail
  (`@services/items` → `…/services/items`) — ambiguous tails resolve to the shortest match. Vue
  template expressions are not parsed — only `<script>` / `<script setup>`. Angular templates
  likewise.
- *All*: cross-file invalidation is per file for C#, project-wide for Python/JS. After changing a
  public signature in C#, `sync --full`.

## Verify before trusting

```
python3 -m unittest discover -s tests -v
python3 scripts/inventory.py verify --root <project>
```

Coordinator tests need no SDK: they put a stub `dotnet` on `PATH` that emits analyzer-shaped JSON
(`FAKE_DOTNET_FAIL=1` makes it fail), so description preservation, transactional apply, retry status
and call cleanup are deterministic. Reuse that pattern rather than skipping — a wrapper that shells
out can always be tested by shadowing the binary.

Per-language suites skip themselves when the analyzer is absent: `test_integration_dotnet.py`,
`test_python_analyzer.py`, `test_js_analyzer.py`. Skips are expected before setup; a `FAIL` never is.

`references/schema.md` documents the tables and `semantic_status`; `references/roadmap.md` separates
verified behaviour from deliberate gaps; `references/analyzer-contract.md` is the interface a new
language must implement.

## Maintaining this skill

```
scripts/            inventory.py (coordinator), ai_inventory.py (renderer), skill_updates.py
analyzers/csharp/   Program.cs + csproj, built as net8.0
analyzers/python/   python_analyzer.py (stdlib ast)
analyzers/js/       analyze.js + package.json/lock
test-scenarios/     csharp/, python/, js/ — the sample trees the integration tests index
references/         schema, roadmap, setup-dotnet, analyzer-contract
templates/          pre-commit-hook.sh — refresh + stage AI_INVENTORY.md before a commit
tests/              coordinator, one suite per analyzer, ai_inventory
```

One folder per language in both trees; a new language adds `analyzers/<lang>/` and
`test-scenarios/<lang>/` rather than dropping files beside the coordinator.

After touching an analyzer, the coordinator or the renderer:

1. `python3 -m unittest discover -s tests` — the whole suite, not only the suite you touched; the
   coordinator tests drive the same paths through a stub `dotnet`.
2. Any analyzer edit needs `sync --full` (or `--lang <l> --full`) on every project you rely on:
   the hashes are of **source files**, not of the analyzer, so a plain `sync` reports
   `changed_files: 0` and re-analyses nothing while the old rows keep looking healthy.
3. For C#, rebuild and re-copy the project-local DLL (workflow step 2) before that `--full sync` —
   a stale copy still reports `analyzer_status: ok` while resolving less.
4. The published mirror is `github.com/dyeggoM/hermes-code-inventory-skill`; copy the changed files
   over and push, then run the suite from a **fresh clone**. A green run in the working copy is not
   proof that the published tree is complete — fixtures and lockfiles are exactly what goes missing.

### Updating an installed copy

An installed skill is plain files, so it drifts from `main` silently. `scripts/skill_updates.py`
answers "is this copy behind?" and writes nothing without `--apply`, which is the approval step:

```
python3 scripts/skill_updates.py                 # list what is behind (exit 10 when it is)
python3 scripts/skill_updates.py --apply         # install it, then re-run the suite
python3 scripts/skill_updates.py --json          # machine-readable, for a scheduler
```

- The install records where it came from in `.skill-install.json` (commit + sha256 per file). Without
  that marker the script falls back to comparing file contents against `main`.
- `behind` lists added/modified/deleted files from `git diff <installed>..<ref>`; deletions remove the
  file rather than leaving a stale copy behind.
- A file edited after installation is reported as a *local edit* and blocks `--apply` until `--force`
  — that is how a hand patch on the installed copy survives an update instead of being lost.
- An install taken from a branch (not `main`) reports **ahead**, not behind: offering to "update"
  it would revert the branch. Compare it with `--rev <branch>` instead.
- If the recorded commit is not in the target's history at all (the branch was rebased, so the
  marker points at a rewritten commit), the check reconciles by **file content** and only deletes a
  file when the target's history shows a real deletion — a file that only existed on the branch
  survives.
- After applying, run the suite from the installed directory: the copy is what the next session
  loads, and a broken copy fails later, in a task, not here.