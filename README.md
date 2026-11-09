# code-inventory

A symbol-level index of a codebase — who calls what, what exists where, what a change would break —
built for **feeding an LLM the map instead of the repo**.

The indexer is a small Python coordinator plus one analyzer per language. It stores everything in a
single SQLite file per project, updates incrementally, and can render an AI-facing document in which
every symbol is one line and every call edge is either resolved or honestly marked unresolved.

Supported: **C#** (Roslyn + MSBuild), **Python** (stdlib `ast`), **JavaScript/TypeScript** including
**React, Vue SFC and Angular** (`@babel/parser`).

```
.project/.code-inventory/inventory.sqlite3     # the index
               AI_INVENTORY.md                 # the document an LLM reads
               ai_inventory.json               # the same, machine-readable
```

## Why

An LLM asked to change a function usually receives either the whole repo (too much) or one file
(too little: it can't see the callers). This tool answers the two questions that matter before an
edit — *what already exists* and *who calls it* — in a few thousand tokens, with `path:line` anchors
so the model can request exactly the code it needs.

## Requirements

| Part | Needs |
|---|---|
| Coordinator, Python + AI document | Python 3.10+, no third-party packages |
| Python projects | nothing |
| JS/TS/Vue/Angular projects | Node 18+, `npm install --prefix analyzers/js` (once, ~5 MB) |
| C# projects | a .NET SDK **at least as new as the project's target framework** |

For C#, build the analyzer once and copy it into the project you index:

```bash
dotnet build analyzer/CodeInventory.Analyzer.csproj -c Release
mkdir -p <project>/.code-inventory/analyzer
cp -a analyzer/bin/Release/net8.0/. <project>/.code-inventory/analyzer/
```

`references/setup-dotnet.md` covers a no-root SDK install and what happens when the SDK is older than
the project's TFM (declarations still index, but every call stays unresolved — the analyzer says so).

## Quick start

```bash
# 1. index
python3 scripts/inventory.py init --root /path/to/project
python3 scripts/inventory.py sync --root /path/to/project          # --lang cs|py|js to limit
python3 scripts/inventory.py verify --root /path/to/project

# 2. read it
python3 scripts/inventory.py status --root /path/to/project
python3 scripts/inventory.py search   --root /path/to/project --query Delete
python3 scripts/inventory.py callers  --root /path/to/project --symbol 'M:App.Repo.Delete(System.Int32)'
python3 scripts/inventory.py callees  --root /path/to/project --symbol 'py:app.service:Service.save'

# 3. render the document an LLM can work from
python3 scripts/ai_inventory.py --root /path/to/project
```

Then give a model three things: `AI_INVENTORY.md`, the task, and for anything surgical:

```bash
python3 scripts/ai_inventory.py --root . --context Service.save --budget 8000
```

`--context` prints just that symbol's code block plus its callers and callees by `path:line` — the
smallest unit that still carries impact information.

## What the document contains

- one line per symbol, grouped by file: `Lnn`, kind, qualified name, parameters/return, fields or
  component state, doc-comment/docstring purpose, `→ n` callees, `← n` callers
- **call graph** and **impact** (reverse edges, highest fan-in first) — read the impact line before
  changing a signature; every listed caller is a file that must change too
- **HTTP routes** for C# (`[Route]`/`[HttpGet]`), including endpoints inherited from a generic base
  controller
- **unresolved calls** per file (framework APIs, dynamic dispatch) — dependency information, never
  presented as impact
- a **coverage** block: index states, resolved-edge ratio, per-language blind spots, analyzer warnings

Large repos **split automatically**: past `--max-chars` (default 160k) `AI_INVENTORY.md` becomes a
router index and each module gets `inventory/<module>.md`, refined by sub-folder and finally per
file. Parts are rendered with the full symbol map in scope, so a symbol's impact still names callers
from other modules. Read the index, then one part — never the whole set.

Rules the renderer keeps, by design:

- purposes come from doc comments only — never invented;
- impact comes from resolved symbol keys only — never guessed;
- a symbol that is not indexed is not thereby proven unused (see *Limitations*).

## Design

```
scripts/inventory.py            coordinator: discovery, hashing, SQLite, hierarchy, incremental sync
scripts/ai_inventory.py         renderer: Markdown + JSON, routes, impact, context packs, splitting
scripts/analyzers/*.py          Python analyzer (stdlib)
analyzers/js/analyze.js         JS/TS/Vue analyzer (project-wide, one pass)
analyzer/Program.cs             C# analyzer (Roslyn/MSBuild, one project load per file)
```

One coordinator, one analyzer per language. Each analyzer parses its language and resolves edges;
the coordinator owns storage and policy. Adding a language means implementing
`references/analyzer-contract.md` and extending the extension map.

Symbol keys are stable identifiers, so hand-written descriptions survive re-indexing:

| Language | Format |
|---|---|
| C# | Roslyn documentation IDs — `M:Sample.Services.OrderService.Save(System.Int32)` |
| Python | `py:<dotted module>:<qualname>` — `py:app.service:Service.save` |
| JS/TS/Vue | `js:<path without ext>:<qualname>` — `js:src/components/Button:Button` |

## Incremental and honest

Every source file is hashed (staged, unstaged and untracked content alike); unchanged files are
skipped, changed ones re-analyzed, deleted ones removed. A file is only `semantic_status='ok'` after
its analyzer output was applied; anything else is retried on the next `sync` without a content
change, and a retry is reported separately from an edit.

`verify` fails on real corruption only: orphan functions, call edges whose caller is gone, and
non-external edges pointing at symbols that no longer exist.

## Cost

- Python and JS/TS: one pass per sync — hundreds of files in seconds.
- C#: the analyzer opens the project **per file**, ~5 s each. A 12-file project takes ~75 s cold;
  incremental syncs only pay for what changed. A 200-file solution is 15–20 minutes on first sync.
- Rendering ~1,200 symbols into a document takes about a second.

## Limitations

Not resolved, by design: `new`-constructor edges, property/indexer access, operators, delegates,
events, reflection, `dynamic`; interface calls bind to the **interface member**, not to the
implementing class; Vue/Angular **templates** are not parsed (only `<script>`); Python `getattr`,
monkey-patching and decorator-rewritten signatures; JS computed access and barrel re-exports.

Directory names such as `build`, `dist`, `vendor`, `obj`, `bin`, `node_modules` are skipped — a real
package called `build/` will be missed.

`references/roadmap.md` lists what is verified versus deliberately out of scope. Do not treat an
inventory as complete when analyzer loading or compilation failed; the analyzer status and the
coverage block say when that happened.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

Coordinator tests need no SDK: they shadow `dotnet` with a stub that emits analyzer-shaped JSON, so
hashing, description preservation, transactional apply, retry status and call cleanup are
deterministic. Per-language suites skip themselves when their analyzer is absent.

## Using it as an agent skill

`SKILL.md` is the agent-facing instruction file (front-matter + workflow) — drop this repository in
your agent's skills directory, or just call the scripts directly as shown above. The scripts are
plain CLI tools with no framework dependency.

## License

MIT — see `LICENSE`.
