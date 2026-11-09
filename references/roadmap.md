# Scope and limitations

## Languages

| Language | Analyzer | Mode | Notes |
|---|---|---|---|
| C# | `analyzer/` (Roslyn/MSBuild, built as net8.0) | per file | SDK must match the project TFM; recursive `.sln`/`.csproj` discovery |
| Python | `scripts/analyzers/python_analyzer.py` (stdlib `ast`) | project-wide | no dependencies; resolves imports, `self.`, module aliases |
| JS/TS/JSX/TSX | `analyzers/js/analyze.js` (`@babel/parser`) | project-wide | `npm install --prefix analyzers/js` once |
| Vue SFC | same JS analyzer | project-wide | `<script>` / `<script setup>` only; templates unparsed |
| Angular | same JS analyzer | project-wide | decorators emitted as declarations |

Adding a language means implementing `references/analyzer-contract.md`; the coordinator needs no
change beyond the extension map.

## Done and verified (v0.4)

- Recursive discovery, folder/file hierarchy, SHA-256 incremental sync, Git HEAD/dirty metadata,
  SQLite persistence, one transaction per file.
- C# end-to-end: declarations, overloads with distinct symbol keys, cross-file edges, interface
  dispatch resolved to the **interface member** (`BaseController.Get → IRepository.GetAll`), and
  framework calls carrying their doc ID with `external=1` rather than a NULL callee.
- Symbol keys are normalised through `OriginalDefinition`, so a call through a constructed generic
  (`IRepository<Item>.GetAll`) lands on the declared symbol (`IRepository\`1.GetAll`).
- Python: classes, functions, nested methods, `self.` calls, module aliases, local functions.
- JS/TS/Vue: SFC scripts, classes, arrow/function declarations, **object-literal methods**
  (`export const svc = { getAll() {} }`) as `object` + `method`, and non-relative import aliases
  resolved by path tail (`@services/items`).
- Module-level and object-scope callers are kept (`<module>`, `<object>`); call cleanup validates
  callers against `nodes`, not `functions`.
- `ai_inventory.py`: symbol/impact document, HTTP routes (with inherited endpoints), context packs,
  and automatic splitting into an index plus per-module files over `--max-chars`.
- Manual descriptions survive rescan (captured by symbol key before replacement).
- `verify` reports dangling caller/callee edges; non-`ok` files are retried without a content change.

## Not implemented — do not claim coverage for these

- **Cross-file invalidation.** The invalidation unit is one source file (C#) or the whole project
  (Python/JS). A changed public signature can leave callers in unchanged files stale.
  `sync --full` after a signature change; a dependency-driven policy is the real fix.
- **Hierarchy fidelity.** Namespace and nested-type parents, partial types split across files, and
  multi-file logical types are not unified. Fields and events are enumerated as syntax but their
  individual variable symbols are not resolved.
- **Call kinds.** Only invocation expressions are collected. Object creation (`new`), property and
  indexer access, operators, delegates, events, reflection and `dynamic` are out of scope, so a
  constructor edge is missing unless something invokes it explicitly.
- **Interface dispatch to implementations.** A call through an interface resolves to the interface
  member; the implementing class is not inferred (that would need whole-solution type-hierarchy
  analysis).
- **Top-level statements** and local-function attribution are approximated.
- **Description lifecycle.** No AI generation command, no CLI for editing descriptions, no staleness
  review. The schema supports it (`description`, `description_origin`); the workflow does not.
- **Migrations.** Only the additive `semantic_status` column is migrated. No versioned schema
  migration path.
- **Multi-project solutions, custom MSBuild imports, linked files, source generators** are untested.
- **Vendored/generated trees** are skipped by directory name (`build`, `dist`, `vendor`, `obj`, …).
  A real package called `build/` will be missed — pass a root that does not contain it, or extend
  `IGNORE` deliberately.

## Cost

C# opens the project once per file, measured at ~5 s per invocation on a warm SDK (12 files ≈ 75 s).
Python and JS run one pass per sync and index hundreds of files in seconds. `ai_inventory.py` renders
~1k symbols in about a second.

This is not production-certified. Do not label an inventory complete when analyzer loading or
compilation fails.
