# Installing .NET (no root)

The analyzer targets `net8.0`, but **the SDK must be at least as new as the project you index**: MSBuild
loads the project and resolves its target framework. A net9.0 project analysed by the 8.0 SDK loads
with zero metadata references — declarations appear, every framework call stays unresolved, and the
analyzer records a `zero metadata references` warning.

```bash
curl -fsSL https://dot.net/v1/dotnet-install.sh -o /tmp/dotnet-install.sh
chmod +x /tmp/dotnet-install.sh
/tmp/dotnet-install.sh --channel 9.0 --install-dir "$HOME/.dotnet" --no-path   # or 8.0, 10.0…
$HOME/.dotnet/dotnet --list-sdks        # side-by-side installs are fine and expected
```

`--no-path` is deliberate: it leaves the shell alone. The coordinator resolves the binary itself, in
this order:

1. `dotnet` on `PATH`
2. `$DOTNET_ROOT/dotnet`
3. `$HOME/.dotnet/dotnet`

So nothing needs exporting for `inventory.py`. Set them anyway for `dotnet build` and for tools
you drive by hand:

```bash
export DOTNET_ROOT="$HOME/.dotnet"
export PATH="$DOTNET_ROOT:$PATH"
export DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1
```

`MSBuildLocator.RegisterDefaults()` picks the newest installed SDK, so installing 9.0 next to 8.0
is enough to analyse net9.0 solutions — the analyzer itself keeps running on the 8.0 runtime.

## Build the analyzer

```bash
dotnet build analyzers/csharp/CodeInventory.Analyzer.csproj -c Release
mkdir -p <project>/.code-inventory/analyzer
cp -a analyzers/csharp/bin/Release/net8.0/. <project>/.code-inventory/analyzer/
```

First build restores three NuGet packages (`Microsoft.Build.Locator 1.7.8`,
`Microsoft.CodeAnalysis.CSharp.Workspaces 4.11.0`, `Microsoft.CodeAnalysis.Workspaces.MSBuild 4.11.0`)
and needs network. Measured cold: ~16 s including restore. Warnings are clean.

Re-copy the DLL after every `dotnet build`: `sync` uses the copy inside the project, so rebuilding
the skill leaves projects indexing with the old analyzer until the copy is refreshed. After
replacing it, `sync --lang cs --full` once — existing rows were produced by the previous build.

## What goes wrong

- `dotnet: command not found` inside a sync warning → the coordinator found no binary in the three
  locations above. Install the SDK or point `DOTNET_ROOT` at it.
- `Analyzer DLL missing; structural semantic indexing skipped` → step 3 of the workflow was
  skipped; the DLL must live in `<project>/.code-inventory/analyzer/`.
- `zero metadata references` warning → SDK older than the project's target framework. Install the
  matching SDK and `sync --lang cs --full`; do not read the 6 or 7 surviving edges as "this project
  has no calls".
- MSBuild load failure for a solution → the analyzer only looks for `*.sln` / `*.csproj` at the
  project root, non-recursively. Point `--root` at the directory holding the solution file.
- Reinstalling is safe: the SDK is a self-contained directory, delete `~/.dotnet` to undo.
