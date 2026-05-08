# sim-plugin-dymola

Use Codex, Claude Code, or another AI agent to run and inspect
[Dymola](https://www.3ds.com/products/catia/dymola) automation through `sim`.

> **Status: real-install alpha.** This plugin is under development and not
> intended for out-of-the-box production use. The initial implementation has
> been smoke-tested against one local Dymola 2026x Refresh 1 installation, but
> it has not been broadly validated across license types, compiler setups,
> Linux installs, or production simulation workloads.

Dymola is a commercial Dassault Systemes product. The Dymola solver and its
Python/API bindings are not bundled; you supply and license Dymola yourself.
See [LICENSE-NOTICE.md](LICENSE-NOTICE.md).

## What Works Today

- The package installs as a normal `sim` plugin.
- `sim check dymola` discovers local installs from environment variables,
  `PATH`, Windows registry entries, and common install paths.
- Discovery reports the bundled Dymola Python interface wheel, local manuals,
  Python API docs, Java/JavaScript docs, and DymolaCommands docs when present.
- File detection recognizes `.mos`, `.mo`, and Python files that mention
  Dymola automation APIs.
- Linting performs conservative local checks.
- `.mos` files execute through `dymola.dymola_interface.DymolaInterface.RunScript`.
- `.mo` files open through `openModel()` and, when the top-level class name can
  be inferred, run `checkModel()`.
- Python scripts run in a subprocess with the local Dymola API wheel prepended
  to `PYTHONPATH`.
- Persistent sessions work through:
  - `launch()` / `sim connect --solver dymola`
  - `run()` / `sim exec --solver dymola 'DymolaVersion()'`
  - `disconnect()`
- Dymola logs are saved under `.sim/dymola/...`, attached as artifacts, and
  scanned for clear failure signals such as `= false`, compiler failures, and
  license errors.

## Current Validation Evidence

Tested locally on Windows with:

- Install root: `C:\Program Files\Dymola 2026x Refresh 1`
- Launcher: `bin64\Dymola.exe`
- API wheel: `Modelica\Library\python_interface\dymola-2026.1-py3-none-any.whl`
- `DymolaVersion()` returned `Dymola Version 2026x Refresh 1, 2026-04-08`
- A tiny `.mos` script containing `DymolaVersion();` executed successfully.
- A tiny `.mo` model opened and passed `checkModel()`.
- A bundled example `simulateModel(...)` reached translation but failed because
  no supported C compiler was selected on this machine. That means the alpha
  has verified API launch, scripting, model checking, log capture, and compiler
  failure reporting, but not a full compiled simulation result.

## Install

```bash
pip install git+https://github.com/svd-ai-lab/sim-plugin-dymola@main
```

After install:

```bash
sim --json plugin list
sim plugin doctor dymola
sim plugin sync-skills
sim --json check dymola
```

## Usage

```bash
sim lint --solver dymola path/to/script.mos
sim run --solver dymola path/to/script.mos
sim run --solver dymola path/to/Model.mo
sim run --solver dymola path/to/dymola_script.py

sim connect --solver dymola
sim exec 'DymolaVersion()'
sim inspect session.summary
sim disconnect
```

For a visible Dymola window:

```bash
sim connect --solver dymola --ui-mode gui
```

## Local Documentation

Dymola 2026x Refresh 1 includes substantial local documentation. The plugin
reports these paths in `detect_installed()`, `sim --json check dymola`, and
the driver query `docs.paths`:

- `Documentation\index.html`: local documentation hub
- `Documentation\Dymola Full User Manual.pdf`
- `Documentation\Dymola User Manual 2B.pdf`: simulation interfaces and export
- `Modelica\Library\python_interface\doc\index.html`: Python interface docs
- `Modelica\Library\python_interface\doc\searchindex.js`: searchable Sphinx index
- `Modelica\Library\java_interface\doc\index.html`
- `Modelica\Library\javascript_interface\doc\index.html`
- `Modelica\Library\DymolaCommands 1.21\package.mo`
- `Modelica\Library\DymolaCommands 1.21\help\DymolaCommands_SimulatorAPI.html`

The driver does not vendor any of these files. It only discovers and reports
paths from the user's local Dymola installation.

## How It Works

The plugin registers via three entry-point groups:

```toml
[project.entry-points."sim.drivers"]
dymola = "sim_plugin_dymola:DymolaDriver"

[project.entry-points."sim.skills"]
dymola = "sim_plugin_dymola:skills_dir"

[project.entry-points."sim.plugins"]
dymola = "sim_plugin_dymola:plugin_info"
```

`sim.drivers` exposes the driver class, `sim.skills` exposes the bundled skill
files, and `sim.plugins` exposes catalogue-style metadata for local discovery.

At runtime, the driver discovers Dymola's bundled Python wheel and inserts that
wheel into `sys.path` only when execution starts. This keeps normal plugin
discovery cheap and safe on machines without Dymola installed.

## Development

```bash
git clone https://github.com/svd-ai-lab/sim-plugin-dymola
cd sim-plugin-dymola
python -m pip install -e ".[test]"
python -m pytest
python -m build --wheel
```

## Documentation Basis

The scaffold started from public product documentation:

- Dymola overview: <https://www.3ds.com/products/catia/dymola>
- Export capabilities and interfacing: <https://www.3ds.com/products/catia/dymola/export-capabilities-interfacing-other-software>
- Latest release: <https://www.3ds.com/products/catia/dymola/latest-release>

The implementation was then updated from local Dymola 2026x Refresh 1 docs and
the bundled Python interface wheel.

## Contributor Validation Checklist

- Validate Windows paths on more than one Dymola release.
- Validate Linux install discovery and launcher behavior.
- Validate full `simulateModel()` result generation with a configured compiler.
- Confirm `.mat` result file names, logs, and failure modes for tiny smoke cases.
- Confirm how license failures surface for expired, unavailable, and network
  license seats.
- Add gated integration tests that skip cleanly without Dymola installed.
- Decide whether to expose higher-level helpers for `simulateModel`,
  `translateModelFMU`, and trajectory reads.

## License

Apache-2.0. See [LICENSE](LICENSE) and [LICENSE-NOTICE.md](LICENSE-NOTICE.md).
