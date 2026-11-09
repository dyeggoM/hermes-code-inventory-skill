# SQLite schema

Database: `<project>/.code-inventory/inventory.sqlite3`.

- `nodes`: containment hierarchy: project → folders → files → namespaces/classes → members.
- `functions`: callable symbol key, signature, path/line, `language`, description, description origin.
- `calls`: caller → callee symbol key, display name, source path/line, external flag.
- `file_state`: per-file SHA-256, indexed timestamp, `language`, and `semantic_status`.
- `sync_runs`: timestamp, Git HEAD, dirty flag, changed/deleted counts, mode, analyzer status.
- `diagnostics`: warnings and errors tied to a sync run.

`language` is one of `cs`, `py`, `js` (JS, JSX, TS, TSX and Vue all report `js`). It is derived from
the file extension by the coordinator, not trusted from the analyzer.

## `file_state.semantic_status`

Added after v0.2; existing databases get the column by `ALTER TABLE` on open.

| value | meaning | retried automatically |
|---|---|---|
| `ok` | analyzer output for this content was applied | no |
| `none` | initial state, no attempt yet | yes |
| `unavailable` | analyzer DLL or `dotnet` was missing; hash stored | yes |
| `failed` | analyzer exited non-zero, emitted bad JSON, or the apply rolled back | yes |

Anything that is not `ok` is picked up by the next `sync` even when the file's hash is unchanged,
which is what makes installing the analyzer (or fixing `dotnet`) enough to finish earlier work. A
retry never counts as `changed_files`.

## Invariants

Partial type declarations should resolve to one logical type identity while declaration locations
remain associated with their files. Calls to symbols outside the project remain external edges.
Unresolved/dynamic calls are not fabricated — `callee_symbol_key` stays NULL.

An external edge **may** carry a `callee_symbol_key` that points outside `functions` (the C# analyzer
emits the framework method's doc ID and sets `external=1`). Therefore "in-project" means
`callee_symbol_key IN (SELECT symbol_key FROM functions)`, never `callee_symbol_key IS NOT NULL`.

`cleanup_calls()` runs at the end of every sync and removes edges whose **caller** is not a declared
node (checked against `nodes`, so module-level and object-scope callers survive), plus non-external
edges whose callee symbol no longer exists. External edges are exempt because their callee is
deliberately outside `functions`.
