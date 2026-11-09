# Analyzer contract

Every language analyzer is a small program that reports structural facts for a project. The
coordinator (`scripts/inventory.py`) owns storage, hashing, hierarchy and incremental policy; the
analyzer owns parsing and symbol resolution. Nothing else is shared.

## Invocation

```
<analyzer-cmd> analyze-project --root <absolute project dir>
```

One invocation per language per sync. stdout is a single JSON object, nothing else. Non-zero exit
means the whole language failed; per-file problems go in `diagnostics` instead.

```json
{
  "files": [
    {
      "path": "src/app/service.py",
      "declarations": [
        {"kind": "class",  "name": "Service", "symbol_key": "py:src.app.service:Service",
         "parent_symbol_key": null, "signature": "class Service", "line": 4},
        {"kind": "method", "name": "save", "symbol_key": "py:src.app.service:Service.save",
         "parent_symbol_key": "py:src.app.service:Service",
         "signature": "def save(self, id: int) -> None", "line": 9}
      ],
      "calls": [
        {"caller_symbol_key": "py:src.app.service:Service.save",
         "callee_symbol_key": "py:src.app.validator:check",
         "callee_name": "check", "line": 10, "external": 0}
      ],
      "diagnostics": []
    }
  ]
}
```

`path` is relative to `--root`, POSIX separators, always present. Files with no declarations still
appear (so the coordinator can mark them semantically complete).

## Symbol keys

Stable across edits as long as the qualified name does not change, and unique per project.

```
<lang>:<module>:<qualname>
```

- `<lang>` — `cs`, `py`, `js` (JS, JSX, TS, TSX and Vue SFCs all use `js`)
- `<module>` — for Python the dotted module path (`src.app.service` for `src/app/service.py`); for
  JS/TS the root-relative path without extension (`src/components/Button`)
- `<qualname>` — dotted, outermost first: `Service.save`, `App`, `helpers.formatDate`

Overloads are not a thing in Python or JS; TypeScript overload signatures collapse to the
implementation, so one key per name is correct.

## Declaration kinds

`module`, `class`, `interface`, `type`, `function`, `method`, `constructor`, `component`,
`property`, `field`, `local_function`, `decorator`.

`parent_symbol_key` links a member to its class/module. `null` means top level of the file.

## Calls

One entry per call site. `callee_symbol_key` is filled **only** when the target resolves to a symbol
declared in this project; otherwise `null` and `external: 1`. Never invent an edge: an unresolved
`obj.method()` on an unknown receiver stays `null`/`external: 1`.

`caller_symbol_key` must be a declaration the same analyzer emitted, or the coordinator drops the
edge. Calls at module top level (outside any function) are attributed to the `<module>` declaration.

## Framework notes

- React: function components are `component` declarations; hooks are ordinary calls.
- Vue: a `.vue` SFC contributes the `default` component from `<script>` / `<script setup>`, plus its
  methods; template-only files contribute just the component.
- Angular: `@Component` / `@Injectable` classes are `class` declarations with their methods; the
  decorator is a `decorator` declaration whose parent is the class.

## Diagnostics

`{"severity": "error"|"warning", "message": "..."}`. Report a file that failed to parse as an error
diagnostic on that file — never drop it silently.

## Failure modes when writing one

- **Declarations without edges is the classic half-failure, and it looks like success.** A walker that
  recurses into the wrong child field emits a perfect declaration list and zero calls, and the sync
  still reports `analyzer_status: ok`. Assert a specific cross-file edge in the fixture — never just a
  count of declarations.
- **Babel puts a class method's body on `member.body`, not `member.value`.** `value` exists on class
  properties; visiting it for `ClassMethod` drops every call inside every method with no error. The
  symptom is an empty `calls` array for exactly the class members that were declared correctly.
- **Project-wide, even when the coordinator seems file-oriented.** Cross-file resolution needs the
  whole symbol table before any call is resolved: parse every file first, then resolve in a second
  pass. A per-file analyzer can only ever produce `external: 1` for imports.
- **Emit an entry for every discovered file**, including ones with no declarations. A missing entry is
  `analyzer returned no entry`, which the coordinator marks failed and retries forever.
- **Resolve through the import table, not by name.** Matching a call to any declaration with the same
  name invents edges between unrelated modules that happen to share a `check()` or `save()`. Inventing
  an edge is worse than leaving it external — this is the one rule the contract will not bend on.
- **The coordinator owns `language`, paths and hierarchy.** Report `path` relative to `--root` with
  POSIX separators and nothing else about location; a language field from the analyzer is ignored.
