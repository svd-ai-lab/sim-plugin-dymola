---
name: dymola
description: Use Dymola through sim with the locally installed Dymola Python interface for .mos scripts, .mo model checks, and persistent Dymola commands.
---

# Dymola Skill

This skill is bundled with `sim-plugin-dymola`, an under-development real-install
alpha. It has been smoke-tested against one local Dymola 2026x Refresh 1
installation, but broad production validation is still pending.

## Safe Use

- Use `sim check dymola` to confirm Dymola install discovery and local API docs.
- Use `sim lint --solver dymola <file>` for conservative local checks on `.mos`,
  `.mo`, and Dymola-oriented Python files.
- Use `sim run --solver dymola <script.mos>` for Dymola `.mos` scripts.
- Use `sim run --solver dymola <model.mo>` to open the file and run `checkModel`
  when the top-level Modelica class can be inferred.
- Use `sim connect --solver dymola` followed by `sim exec 'DymolaVersion()'`
  for a persistent Dymola API session.
- Do not claim a full simulation is production-valid until the Dymola log and
  generated artifacts have been inspected.

## Local Documentation Retrieval

If Dymola is installed, inspect `sim check dymola --json` or query
`docs.paths`. Useful local sources include:

- `Documentation/index.html`: local documentation hub
- `Documentation/Dymola User Manual 2B.pdf`: simulation interfaces and export
- `Modelica/Library/python_interface/doc/index.html`: Python interface docs
- `Modelica/Library/python_interface/doc/searchindex.js`: searchable Sphinx index
- `Modelica/Library/DymolaCommands 1.21/package.mo`
- `Modelica/Library/DymolaCommands 1.21/help/DymolaCommands_SimulatorAPI.html`

Prefer these local docs over memory when choosing Dymola API calls.

## Workflow

- Author or receive Modelica models in `.mo` files.
- Drive Dymola automation with `.mos` scripts or `sim exec` snippets.
- Run tiny smoke checks first, then inspect `.sim/dymola/.../dymola.log`.
- If `simulateModel()` fails with a compiler message, configure a supported
  Visual C++, MinGW GCC, or WSL compiler in Dymola before retrying.
- Prefer script/API execution over GUI automation.
- For GUI collaboration, launch with `sim connect --solver dymola --ui-mode
  gui`, then open/reload edited files with
  `sim exec 'openModel("C:/path/to/Model.mo", true, true)'`. Local testing
  showed that Dymola does not auto-refresh externally saved `.mo` changes, but
  this forced reload updates the visible Diagram view. Avoid it when there are
  unsaved GUI-side edits you need to preserve.

## Validation Gaps

- Full compiled simulations need validation on a machine with a configured
  compiler.
- License failures need examples from expired, unavailable, and network seats.
- Linux launcher behavior still needs real host evidence.
- Result artifact naming and trajectory-read helpers are not yet wrapped.

## Public Documentation

- Dymola overview: https://www.3ds.com/products/catia/dymola
- Interfacing/export: https://www.3ds.com/products/catia/dymola/export-capabilities-interfacing-other-software
- Latest release: https://www.3ds.com/products/catia/dymola/latest-release
