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

`MSBuildLocator.RegisterDefaults()` picks the newest installed SDK, and **the process must run on a
runtime at least as new as that SDK**: the analyzer targets `net8.0` and its csproj sets
`<RollForward>LatestMajor</RollForward>` for exactly this reason. With an older runtime selected
(the 8.0 runtime is installed alongside a 9.x SDK and roll-forward is disabled), Locator refuses the
newer SDK and 100% of the C# files fail — see *What goes wrong*.

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
- `analyzer exited -6` / exit 134 with `System.InvalidOperationException: No instances of MSBuild
  could be detected` → the analyzer process is running on a runtime older than the installed SDK
  (typical: the 8.0 runtime present next to a 9.x SDK), so `MSBuildLocator` cannot load that SDK's
  MSBuild. Keep `<RollForward>LatestMajor</RollForward>` in
  `analyzers/csharp/CodeInventory.Analyzer.csproj` (it writes `rollForward` into the built
  `*.runtimeconfig.json`) or export `DOTNET_ROLL_FORWARD=LatestMajor` for whoever runs `sync`.
  Verify with `grep rollForward <project>/.code-inventory/analyzer/CodeInventory.Analyzer.runtimeconfig.json`
  before blaming the project.
- MSBuild load failure for a solution → the analyzer searches `*.sln` / `*.csproj` **recursively**
  under `--root` (skipping `bin`, `obj`, `.git`, `node_modules`, `.code-inventory`) and prefers the
  first `.sln`. When a root holds several solutions, point `--root` at the subtree you mean.
- Reinstalling is safe: the SDK is a self-contained directory, delete `~/.dotnet` to undo.
