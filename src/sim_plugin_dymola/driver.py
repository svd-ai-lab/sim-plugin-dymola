"""Dymola driver for sim.

The driver stays SDK-free at import time. Dymola's Python package is normally
bundled inside a licensed Dymola install as a wheel, so the driver discovers
that wheel and injects it into ``sys.path`` only when execution starts.
"""
from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from sim.driver import ConnectionInfo, Diagnostic, LintResult, RunResult, SolverInstall
from sim.runner import detect_output_errors


_DYMOLA_MARKERS = re.compile(
    r"(?:\bDymolaInterface\b|\bdymola\b|DymolaVersion\s*\(|DymolaVersionNumber\s*\(|"
    r"simulateModel\s*\(|translateModel\s*\(|"
    r"openModel\s*\(|checkModel\s*\(|RunScript\s*\(|ExecuteCommand\s*\(|"
    r"DymolaCommands|Modelica)",
    re.IGNORECASE,
)

_MODELICA_DECL = re.compile(
    r"^\s*(within|model|package|class|block|connector|record|function)\b",
    re.MULTILINE,
)

_TOP_LEVEL_MODELICA_NAME = re.compile(
    r"^\s*(?:model|package|class|block|connector|record|function)\s+([A-Za-z_]\w*)\b",
    re.MULTILINE,
)

_DYMOLA_LOG_ERROR_PATTERNS = [
    re.compile(r"^\s*(?:Error|ERROR|Fatal error|FATAL)\b.*"),
    re.compile(r"^\s*=\s*false\s*$"),
    re.compile(r"^\s*No compiler selected\.?.*", re.IGNORECASE),
    re.compile(r".*\b(?:license|licensing)\b.*\b(?:error|failed|denied|expired|unavailable)\b.*", re.IGNORECASE),
    re.compile(r".*\b(?:failed|failure)\b.*", re.IGNORECASE),
]

_DYMOLA_LOG_WARNING_PATTERNS = [
    re.compile(r"^\s*Warning\b.*", re.IGNORECASE),
]

_WORKSPACE_MAX_FILES = 50000


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _launcher_names() -> tuple[str, ...]:
    if os.name == "nt":
        return ("Dymola.exe", "dymola.exe")
    return ("dymola", "Dymola")


def _version_from_path(path: Path) -> str | None:
    """Extract a Dymola release label from a filesystem path when possible."""
    text = str(path)
    for pattern in (
        r"Dymola\s*([0-9]{4}x(?:\s*Refresh\s*\d+)?)",
        r"Dymola\s*([0-9]{4})",
        r"([0-9]{4}x(?:\s*Refresh\s*\d+)?)",
    ):
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return re.sub(r"\s+", " ", match.group(1)).strip()
    return None


def _find_launcher(path: Path) -> Path | None:
    """Return a Dymola launcher if *path* is an install root or executable."""
    names = {name.lower() for name in _launcher_names()}
    if path.is_file() and path.name.lower() in names:
        return path
    if not path.is_dir():
        return None

    for name in _launcher_names():
        for rel in (name, f"bin/{name}", f"bin64/{name}"):
            candidate = path / rel
            if candidate.is_file():
                return candidate
    return None


def _install_root_from_launcher(launcher: Path) -> Path:
    parent = launcher.parent
    if parent.name.lower() in {"bin", "bin64"}:
        return parent.parent
    return parent


def _api_wheel_for_root(root: Path) -> Path | None:
    wheel_dir = root / "Modelica" / "Library" / "python_interface"
    if not wheel_dir.is_dir():
        return None
    wheels = sorted(wheel_dir.glob("dymola-*.whl"), reverse=True)
    return wheels[0] if wheels else None


def _api_version_from_wheel(wheel: Path | None) -> str | None:
    if wheel is None:
        return None
    match = re.match(r"dymola-([^-]+)-", wheel.name, re.IGNORECASE)
    return match.group(1) if match else None


def _docs_for_root(root: Path) -> dict[str, str]:
    candidates = {
        "index": root / "Documentation" / "index.html",
        "release_notes": root / "Documentation" / "Dymola Release Notes.pdf",
        "referential": root / "Documentation" / "Dymola Referential.pdf",
        "full_manual": root / "Documentation" / "Dymola Full User Manual.pdf",
        "user_manual_2b_interfaces": root / "Documentation" / "Dymola User Manual 2B.pdf",
        "python_interface": root / "Modelica" / "Library" / "python_interface" / "doc" / "index.html",
        "python_interface_search": root / "Modelica" / "Library" / "python_interface" / "doc" / "searchindex.js",
        "java_interface": root / "Modelica" / "Library" / "java_interface" / "doc" / "index.html",
        "javascript_interface": root / "Modelica" / "Library" / "javascript_interface" / "doc" / "index.html",
        "dymola_commands": root / "Modelica" / "Library" / "DymolaCommands 1.21" / "package.mo",
        "dymola_commands_simulator_api": root / "Modelica" / "Library" / "DymolaCommands 1.21" / "help" / "DymolaCommands_SimulatorAPI.html",
    }
    return {name: str(path) for name, path in candidates.items() if path.exists()}


def _make_install(path: Path, source: str) -> SolverInstall | None:
    launcher = _find_launcher(path)
    if launcher is None:
        return None
    root = _install_root_from_launcher(launcher)
    wheel = _api_wheel_for_root(root)
    version = _version_from_path(root) or _api_version_from_wheel(wheel) or "?"
    docs = _docs_for_root(root)
    return SolverInstall(
        name="dymola",
        version=version,
        path=str(root),
        source=source,
        extra={
            "launcher": str(launcher),
            "api_wheel": str(wheel) if wheel else None,
            "api_wheel_version": _api_version_from_wheel(wheel),
            "python_interface_doc": docs.get("python_interface"),
            "docs_index": docs.get("index"),
            "docs": docs,
        },
    )


def _candidates_from_env() -> list[tuple[Path, str]]:
    out: list[tuple[Path, str]] = []
    for var in ("SIM_DYMOLA_ROOT", "SIM_DYMOLA_PATH", "DYMOLA_HOME", "DYMOLA_PATH"):
        value = os.environ.get(var)
        if value:
            out.append((Path(value), f"env:{var}"))
    return out


def _candidates_from_path() -> list[tuple[Path, str]]:
    out: list[tuple[Path, str]] = []
    for name in _launcher_names():
        found = shutil.which(name)
        if found:
            out.append((Path(found).resolve(), f"which:{name}"))
    return out


def _candidates_from_windows_registry() -> list[tuple[Path, str]]:
    if os.name != "nt":
        return []
    try:
        import winreg  # type: ignore[attr-defined]  # noqa: PLC0415
    except ImportError:
        return []

    out: list[tuple[Path, str]] = []
    views = [0]
    for flag in ("KEY_WOW64_64KEY", "KEY_WOW64_32KEY"):
        value = getattr(winreg, flag, None)
        if value is not None:
            views.append(value)

    for view in views:
        try:
            base = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Dassault Systemes",
                0,
                winreg.KEY_READ | view,
            )
        except OSError:
            continue
        with base:
            index = 0
            while True:
                try:
                    subkey_name = winreg.EnumKey(base, index)
                except OSError:
                    break
                index += 1
                if "dymola" not in subkey_name.lower():
                    continue
                try:
                    subkey = winreg.OpenKey(base, subkey_name, 0, winreg.KEY_READ | view)
                except OSError:
                    continue
                with subkey:
                    try:
                        install_dir, _ = winreg.QueryValueEx(subkey, "InstallDir")
                    except OSError:
                        continue
                if install_dir:
                    out.append((Path(str(install_dir)), f"registry:{subkey_name}"))
    return out


def _candidates_from_windows_defaults() -> list[tuple[Path, str]]:
    out: list[tuple[Path, str]] = []
    bases: list[Path] = []
    for env_name in ("ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(env_name)
        if not base:
            continue
        root = Path(base)
        bases.extend([
            root,
            root / "Dymola",
            root / "Dassault Systemes",
        ])

    for base in bases:
        if not base.is_dir():
            continue
        if _find_launcher(base):
            out.append((base, f"default-path:{base.parent}"))
        try:
            children = sorted(base.iterdir(), reverse=True)
        except OSError:
            continue
        for child in children:
            if "dymola" in child.name.lower() or _find_launcher(child):
                out.append((child, f"default-path:{base}"))
    return out


def _candidates_from_linux_defaults() -> list[tuple[Path, str]]:
    bases = [Path("/opt"), Path("/usr/local"), Path.home()]
    out: list[tuple[Path, str]] = []
    for base in bases:
        if not base.is_dir():
            continue
        try:
            children = sorted(base.iterdir(), reverse=True)
        except OSError:
            continue
        for child in children:
            if "dymola" in child.name.lower() or _find_launcher(child):
                out.append((child, f"default-path:{base}"))
    return out


_INSTALL_FINDERS: list[Callable[[], list[tuple[Path, str]]]] = [
    _candidates_from_env,
    _candidates_from_path,
    _candidates_from_windows_registry,
    _candidates_from_windows_defaults,
    _candidates_from_linux_defaults,
]
"""Strategy chain. APPEND new finders for new Dymola layouts; do not edit."""


def _scan_dymola_installs() -> list[SolverInstall]:
    """Find Dymola installations visible on this host. Pure stdlib."""
    found: dict[str, SolverInstall] = {}
    for finder in _INSTALL_FINDERS:
        try:
            candidates = finder()
        except Exception:
            continue
        for path, source in candidates:
            inst = _make_install(path, source=source)
            if inst is None:
                continue
            key = str(Path(inst.path).resolve())
            found.setdefault(key, inst)
    return sorted(found.values(), key=lambda i: i.version, reverse=True)


def _dymola_string(path: Path | str) -> str:
    text = str(path).replace("\\", "/")
    return text.replace('"', r'\"')


def _local_importable_dymola() -> bool:
    try:
        import importlib.util

        return importlib.util.find_spec("dymola") is not None
    except Exception:
        return False


def _import_dymola_interface(install: SolverInstall):
    wheel = install.extra.get("api_wheel")
    if wheel and Path(str(wheel)).is_file() and str(wheel) not in sys.path:
        sys.path.insert(0, str(wheel))
    from dymola.dymola_interface import DymolaInterface  # noqa: PLC0415

    return DymolaInterface


def _snapshot_workspace(root: Path) -> dict[str, tuple[float, int]]:
    out: dict[str, tuple[float, int]] = {}
    if not root.is_dir():
        return out
    try:
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            out[str(p)] = (st.st_mtime, st.st_size)
            if len(out) >= _WORKSPACE_MAX_FILES:
                break
    except OSError:
        pass
    return out


def _diff_workspace(before: dict[str, tuple[float, int]], after: dict[str, tuple[float, int]]) -> list[dict]:
    delta: list[dict] = []
    for path, (mtime, size) in after.items():
        old = before.get(path)
        if old is None:
            delta.append({"path": path, "kind": "added", "size": size})
        elif old[0] != mtime or old[1] != size:
            delta.append({"path": path, "kind": "modified", "size": size})
    delta.sort(key=lambda d: -d["size"])
    return delta


def _artifact_entries(delta: list[dict]) -> list[dict]:
    artifacts: list[dict] = []
    for entry in delta:
        artifacts.append({
            "path": entry["path"],
            "kind": entry["kind"],
            "size": entry["size"],
            "type": "file",
        })
    return artifacts


def _log_findings(log_text: str) -> tuple[list[str], list[dict]]:
    errors: list[str] = []
    diagnostics: list[dict] = []
    seen_errors: set[str] = set()
    seen_warnings: set[str] = set()

    for line_no, line in enumerate(log_text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        for pattern in _DYMOLA_LOG_ERROR_PATTERNS:
            if not pattern.match(stripped):
                continue
            message = "Dymola command returned false" if stripped == "= false" else stripped
            if message not in seen_errors:
                errors.append(message)
                diagnostics.append({"level": "error", "message": message, "line": line_no})
                seen_errors.add(message)
            break
        else:
            for pattern in _DYMOLA_LOG_WARNING_PATTERNS:
                if pattern.match(stripped) and stripped not in seen_warnings:
                    diagnostics.append({"level": "warning", "message": stripped, "line": line_no})
                    seen_warnings.add(stripped)
                    break
    return errors, diagnostics


def _tail(text: str, max_chars: int = 4000) -> str:
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]


def _collect_generic_diagnostics(
    *,
    stdout: str,
    stderr: str,
    workdir: Path,
    wall_time_s: float,
    exit_code: int,
    driver_name: str,
) -> tuple[list[dict], list[dict]]:
    try:
        from sim.inspect import InspectCtx, collect_diagnostics, generic_probes  # noqa: PLC0415

        ctx = InspectCtx(
            stdout=stdout,
            stderr=stderr,
            workdir=str(workdir),
            wall_time_s=wall_time_s,
            exit_code=exit_code,
            driver_name=driver_name,
            session_ns={},
            workdir_before=None,
        )
        diags, arts = collect_diagnostics(generic_probes(), ctx)
        return [d.to_dict() for d in diags], [a.to_dict() for a in arts]
    except Exception:
        return [], []


def _maybe_modelica_class_name(text: str) -> str | None:
    within_match = re.search(r"^\s*within\s+([^;]+)\s*;", text, re.MULTILINE)
    within = within_match.group(1).strip() if within_match else ""
    name_match = _TOP_LEVEL_MODELICA_NAME.search(text)
    if not name_match:
        return None
    name = name_match.group(1)
    return f"{within}.{name}" if within else name


class DymolaDriver:
    """Dymola driver using the locally installed Dymola Python interface."""

    def __init__(self) -> None:
        self._dymola = None
        self._install: SolverInstall | None = None
        self._session_id: str | None = None
        self._ui_mode: str = "headless"
        self._connected_at: float | None = None
        self._last_run: dict | None = None
        self._last_error_log: str = ""
        self._sim_dir: Path = Path(os.environ.get("SIM_DIR") or (Path.cwd() / ".sim"))

    @property
    def name(self) -> str:
        return "dymola"

    @property
    def supports_session(self) -> bool:
        return True

    def detect(self, script: Path) -> bool:
        """Detect Dymola automation files."""
        suffix = script.suffix.lower()
        if suffix in (".mos", ".mo"):
            return script.is_file()
        if suffix != ".py" or not script.is_file():
            return False
        try:
            text = script.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        return bool(_DYMOLA_MARKERS.search(text))

    def lint(self, script: Path) -> LintResult:
        """Conservative local linting without launching Dymola."""
        try:
            text = script.read_text(encoding="utf-8", errors="replace")
        except (OSError, UnicodeDecodeError) as exc:
            return LintResult(
                ok=False,
                diagnostics=[Diagnostic(level="error", message=f"cannot read file: {exc}")],
            )

        if not text.strip():
            return LintResult(
                ok=False,
                diagnostics=[Diagnostic(level="error", message="script is empty")],
            )

        suffix = script.suffix.lower()
        if suffix == ".py":
            try:
                ast.parse(text)
            except SyntaxError as exc:
                return LintResult(
                    ok=False,
                    diagnostics=[
                        Diagnostic(
                            level="error",
                            message=f"syntax error: {exc.msg}",
                            line=exc.lineno,
                        )
                    ],
                )
            if not _DYMOLA_MARKERS.search(text):
                return LintResult(
                    ok=True,
                    diagnostics=[Diagnostic(level="warning", message="no Dymola API marker found")],
                )
            return LintResult(ok=True, diagnostics=[])

        if suffix == ".mos":
            diagnostics: list[Diagnostic] = []
            if not _DYMOLA_MARKERS.search(text):
                diagnostics.append(Diagnostic(
                    level="warning",
                    message="no common Dymola command marker found",
                ))
            return LintResult(ok=True, diagnostics=diagnostics)

        if suffix == ".mo":
            diagnostics = []
            if not _MODELICA_DECL.search(text):
                diagnostics.append(Diagnostic(
                    level="warning",
                    message="no top-level Modelica declaration marker found",
                ))
            return LintResult(ok=True, diagnostics=diagnostics)

        return LintResult(
            ok=False,
            diagnostics=[Diagnostic(level="error", message="not a Dymola input file")],
        )

    def connect(self) -> ConnectionInfo:
        """Report Dymola availability without launching Dymola or checking a license."""
        installs = self.detect_installed()
        if not installs:
            return ConnectionInfo(
                solver="dymola",
                version=None,
                status="not_installed",
                message="No Dymola installation detected on this host",
            )
        top = installs[0]
        api_available = bool(top.extra.get("api_wheel")) or _local_importable_dymola()
        if not api_available:
            return ConnectionInfo(
                solver="dymola",
                version=top.version,
                status="error",
                message=f"Dymola found at {top.path}, but the Python interface wheel was not found",
                solver_version=top.version,
            )
        api_note = f"; API wheel {top.extra.get('api_wheel_version')}" if top.extra.get("api_wheel_version") else ""
        docs_note = "; local docs found" if top.extra.get("docs_index") else ""
        return ConnectionInfo(
            solver="dymola",
            version=top.version,
            status="ok",
            message=f"Dymola {top.version} at {top.path}{api_note}{docs_note}",
            solver_version=top.version,
        )

    def detect_installed(self) -> list[SolverInstall]:
        """Enumerate likely Dymola installations visible on this host.

        Strategy chain, deduped by resolved install root:
          1. SIM_DYMOLA_ROOT / SIM_DYMOLA_PATH / DYMOLA_HOME / DYMOLA_PATH
          2. PATH probe via Dymola launcher names
          3. Windows registry under Dassault Systemes
          4. Common Windows install roots
          5. Common Linux/local install roots

        Pure stdlib. Does not import Dymola APIs and does not launch Dymola.
        """
        return _scan_dymola_installs()

    def parse_output(self, stdout: str) -> dict:
        """Parse the last JSON object printed by a Dymola script/snippet."""
        for line in reversed(stdout.strip().splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
        return {}

    def run_file(self, script: Path) -> RunResult:
        """Execute a `.mos`, `.mo`, or Dymola-oriented Python file."""
        suffix = script.suffix.lower()
        if suffix == ".py":
            return self._run_python_file(script)
        if suffix == ".mos":
            return self._run_mos_file(script)
        if suffix == ".mo":
            return self._open_modelica_file(script)
        return RunResult(
            exit_code=2,
            stdout="",
            stderr="not a Dymola input file",
            duration_s=0.0,
            script=str(script),
            solver=self.name,
            timestamp=_utc_now(),
            errors=["not a Dymola input file"],
        )

    def _resolve_install(self, **kwargs) -> SolverInstall:
        explicit = (
            kwargs.get("dymola_path")
            or kwargs.get("dymolapath")
            or kwargs.get("launcher")
            or kwargs.get("dymola_root")
            or kwargs.get("root")
        )
        if explicit:
            inst = _make_install(Path(str(explicit)), "option")
            if inst is not None:
                return inst
            raise RuntimeError(f"no Dymola launcher found at {explicit}")
        installs = self.detect_installed()
        if not installs:
            raise RuntimeError("no Dymola installation detected")
        return installs[0]

    def _new_api(self, install: SolverInstall, *, showwindow: bool = False, custom_args: str = "-nosettings"):
        DymolaInterface = _import_dymola_interface(install)
        launcher = str(install.extra.get("launcher") or "")
        return DymolaInterface(
            dymolapath=launcher,
            showwindow=showwindow,
            closeDymola=True,
            customArgs=custom_args,
        )

    def _save_log(self, dymola, log_path: Path) -> str:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            dymola.ExecuteCommand(f'savelog("{_dymola_string(log_path)}")')
        except Exception:
            pass
        try:
            return log_path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            return ""

    def _run_dir_for_file(self, script: Path) -> Path:
        return script.parent / ".sim" / "dymola" / f"{script.stem}-{uuid.uuid4().hex[:8]}"

    def _run_mos_file(self, script: Path) -> RunResult:
        start = time.monotonic()
        root = script.parent
        before = _snapshot_workspace(root)
        run_dir = self._run_dir_for_file(script)
        log_path = run_dir / "dymola.log"
        errors: list[str] = []
        diagnostics = [d.__dict__ for d in self.lint(script).diagnostics]
        stdout = ""
        stderr = ""
        dymola = None
        script_ok = False

        try:
            install = self._resolve_install()
            dymola = self._new_api(install, showwindow=False)
            dymola.ExecuteCommand(f'cd("{_dymola_string(root.resolve())}")')
            script_ok = bool(dymola.RunScript(str(script.resolve()), silent=False))
            error_log = str(dymola.getLastErrorLog() or "")
            if error_log.strip():
                error_log_errors, error_log_diags = _log_findings(error_log)
                errors.extend(error_log_errors)
                diagnostics.extend(error_log_diags)
            log_text = self._save_log(dymola, log_path)
            log_errors, log_diags = _log_findings(log_text)
            errors.extend(log_errors)
            diagnostics.extend(log_diags)
            payload = {
                "ok": script_ok and not errors,
                "kind": "mos",
                "script_result": script_ok,
                "log_path": str(log_path),
                "log_tail": _tail(log_text, 2000),
            }
            stdout = json.dumps(payload, ensure_ascii=False)
        except Exception as exc:
            errors.append(str(exc))
            stderr = str(exc)
            stdout = json.dumps({"ok": False, "kind": "mos", "error": str(exc)}, ensure_ascii=False)
        finally:
            if dymola is not None:
                try:
                    dymola.close()
                except Exception:
                    pass

        after = _snapshot_workspace(root)
        delta = _diff_workspace(before, after)
        exit_code = 0 if script_ok and not errors else 1
        duration = round(time.monotonic() - start, 3)
        generic_diags, generic_arts = _collect_generic_diagnostics(
            stdout=stdout,
            stderr=stderr or "\n".join(errors),
            workdir=root,
            wall_time_s=duration,
            exit_code=exit_code,
            driver_name=self.name,
        )
        return RunResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr or "\n".join(errors),
            duration_s=duration,
            script=str(script),
            solver=self.name,
            timestamp=_utc_now(),
            errors=errors,
            diagnostics=diagnostics + generic_diags,
            artifacts=_artifact_entries(delta) + generic_arts,
            workspace_delta=delta,
        )

    def _open_modelica_file(self, script: Path) -> RunResult:
        start = time.monotonic()
        root = script.parent
        before = _snapshot_workspace(root)
        run_dir = self._run_dir_for_file(script)
        log_path = run_dir / "dymola.log"
        errors: list[str] = []
        diagnostics = [d.__dict__ for d in self.lint(script).diagnostics]
        stdout = ""
        stderr = ""
        opened = False
        checked = None
        dymola = None

        try:
            install = self._resolve_install()
            dymola = self._new_api(install, showwindow=False)
            dymola.ExecuteCommand(f'cd("{_dymola_string(root.resolve())}")')
            opened = bool(dymola.openModel(str(script.resolve()), mustRead=True, changeDirectory=True))
            try:
                text = script.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            class_name = _maybe_modelica_class_name(text)
            if opened and class_name:
                checked = bool(dymola.checkModel(class_name))
            error_log = str(dymola.getLastErrorLog() or "")
            if error_log.strip():
                error_log_errors, error_log_diags = _log_findings(error_log)
                errors.extend(error_log_errors)
                diagnostics.extend(error_log_diags)
            log_text = self._save_log(dymola, log_path)
            log_errors, log_diags = _log_findings(log_text)
            errors.extend(log_errors)
            diagnostics.extend(log_diags)
            if not opened:
                errors.append("openModel returned false")
            if checked is False:
                errors.append(f"checkModel returned false for {class_name}")
            payload = {
                "ok": opened and checked is not False and not errors,
                "kind": "modelica",
                "opened": opened,
                "checked": checked,
                "class_name": class_name,
                "log_path": str(log_path),
                "log_tail": _tail(log_text, 2000),
            }
            stdout = json.dumps(payload, ensure_ascii=False)
        except Exception as exc:
            errors.append(str(exc))
            stderr = str(exc)
            stdout = json.dumps({"ok": False, "kind": "modelica", "error": str(exc)}, ensure_ascii=False)
        finally:
            if dymola is not None:
                try:
                    dymola.close()
                except Exception:
                    pass

        after = _snapshot_workspace(root)
        delta = _diff_workspace(before, after)
        exit_code = 0 if opened and checked is not False and not errors else 1
        duration = round(time.monotonic() - start, 3)
        generic_diags, generic_arts = _collect_generic_diagnostics(
            stdout=stdout,
            stderr=stderr or "\n".join(errors),
            workdir=root,
            wall_time_s=duration,
            exit_code=exit_code,
            driver_name=self.name,
        )
        return RunResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr or "\n".join(errors),
            duration_s=duration,
            script=str(script),
            solver=self.name,
            timestamp=_utc_now(),
            errors=errors,
            diagnostics=diagnostics + generic_diags,
            artifacts=_artifact_entries(delta) + generic_arts,
            workspace_delta=delta,
        )

    def _run_python_file(self, script: Path) -> RunResult:
        start = time.monotonic()
        root = script.parent
        before = _snapshot_workspace(root)
        errors: list[str] = []
        try:
            install = self._resolve_install()
            env = os.environ.copy()
            env["SIM_DYMOLA_ROOT"] = install.path
            env["SIM_DYMOLA_LAUNCHER"] = str(install.extra.get("launcher") or "")
            if install.extra.get("api_wheel"):
                wheel = str(install.extra["api_wheel"])
                existing = env.get("PYTHONPATH")
                env["PYTHONPATH"] = wheel if not existing else os.pathsep.join([wheel, existing])
            proc = subprocess.run(
                [sys.executable, str(script.resolve())],
                cwd=str(root),
                capture_output=True,
                text=True,
                env=env,
            )
            stdout = proc.stdout.strip()
            stderr = proc.stderr.strip()
            errors.extend(detect_output_errors(stdout, stderr))
            exit_code = proc.returncode
            if exit_code == 0 and errors:
                exit_code = 1
        except Exception as exc:
            stdout = ""
            stderr = str(exc)
            errors.append(str(exc))
            exit_code = 1

        after = _snapshot_workspace(root)
        delta = _diff_workspace(before, after)
        duration = round(time.monotonic() - start, 3)
        generic_diags, generic_arts = _collect_generic_diagnostics(
            stdout=stdout,
            stderr=stderr,
            workdir=root,
            wall_time_s=duration,
            exit_code=exit_code,
            driver_name=self.name,
        )
        return RunResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_s=duration,
            script=str(script),
            solver=self.name,
            timestamp=_utc_now(),
            errors=errors,
            diagnostics=generic_diags,
            artifacts=_artifact_entries(delta) + generic_arts,
            workspace_delta=delta,
        )

    def launch(self, ui_mode: str = "headless", **kwargs) -> dict:
        """Start a persistent Dymola session through DymolaInterface."""
        if self._dymola is not None:
            return {
                "ok": True,
                "session_id": self._session_id,
                "ui_mode": self._ui_mode,
                "message": "Dymola session already active",
            }

        visible = ui_mode in {"desktop", "gui", "visible", "window", "showwindow"}
        custom_args = str(kwargs.get("custom_args") or kwargs.get("customArgs") or "-nosettings")
        started = time.monotonic()
        try:
            install = self._resolve_install(**kwargs)
            dymola = self._new_api(install, showwindow=visible, custom_args=custom_args)
            version = str(dymola.DymolaVersion())
            version_number = dymola.DymolaVersionNumber()
            self._dymola = dymola
            self._install = install
            self._session_id = str(uuid.uuid4())
            self._ui_mode = ui_mode
            self._connected_at = time.time()
            return {
                "ok": True,
                "session_id": self._session_id,
                "ui_mode": ui_mode,
                "version": version,
                "version_number": version_number,
                "install": install.to_dict(),
                "duration_s": round(time.monotonic() - started, 3),
            }
        except RuntimeError as exc:
            return {
                "ok": False,
                "error_code": "SOLVER_NOT_INSTALLED",
                "message": str(exc)[:280],
                "duration_s": round(time.monotonic() - started, 3),
            }
        except Exception as exc:
            return {
                "ok": False,
                "error_code": "RUN_FAILED",
                "message": f"failed to launch Dymola: {exc}"[:280],
                "details": str(exc),
                "duration_s": round(time.monotonic() - started, 3),
            }

    def run(self, code: str, label: str = "snippet") -> dict:
        """Execute one Dymola command in the active session."""
        if self._dymola is None:
            return {
                "ok": False,
                "label": label,
                "error_code": "SESSION_NOT_FOUND",
                "message": "No active Dymola session. Run sim connect --solver dymola first.",
                "stdout": "",
                "stderr": "",
                "duration_s": 0.0,
            }

        run_id = uuid.uuid4().hex[:8]
        run_dir = self._sim_dir / "dymola" / f"session-{self._session_id or 'unknown'}"
        log_path = run_dir / f"{run_id}.log"
        before = _snapshot_workspace(run_dir)
        start = time.monotonic()
        errors: list[str] = []
        diagnostics: list[dict] = []
        result = None
        stderr = ""

        try:
            result = self._dymola.ExecuteCommand(code)
            if result is False:
                errors.append("Dymola command returned false")
                diagnostics.append({"level": "error", "message": "Dymola command returned false", "line": None})
            error_log = str(self._dymola.getLastErrorLog() or "")
            if error_log.strip():
                error_log_errors, error_log_diags = _log_findings(error_log)
                errors.extend(error_log_errors)
                diagnostics.extend(error_log_diags)
            log_text = self._save_log(self._dymola, log_path)
            log_errors, log_diags = _log_findings(log_text)
            errors.extend(log_errors)
            diagnostics.extend(log_diags)
            self._last_error_log = log_text
        except Exception as exc:
            errors.append(str(exc))
            stderr = str(exc)

        duration = round(time.monotonic() - start, 3)
        after = _snapshot_workspace(run_dir)
        delta = _diff_workspace(before, after)
        ok = not errors
        stdout = json.dumps(
            {
                "ok": ok,
                "label": label,
                "result": result,
                "run_id": run_id,
                "log_path": str(log_path),
            },
            ensure_ascii=False,
        )
        generic_diags, generic_arts = _collect_generic_diagnostics(
            stdout=stdout,
            stderr=stderr or "\n".join(errors),
            workdir=run_dir,
            wall_time_s=duration,
            exit_code=0 if ok else 1,
            driver_name=self.name,
        )
        payload = {
            "ok": ok,
            "label": label,
            "stdout": stdout,
            "stderr": stderr or "\n".join(errors),
            "error": "\n".join(errors) if errors else None,
            "result": result,
            "duration_s": duration,
            "diagnostics": diagnostics + generic_diags,
            "artifacts": _artifact_entries(delta) + generic_arts,
            "workspace_delta": delta,
        }
        if not ok:
            payload["error_code"] = "RUN_FAILED"
            payload["message"] = (errors[0] if errors else "Dymola command failed")[:280]
        self._last_run = payload
        return payload

    def query(self, name: str) -> dict:
        """Named query against local Dymola install metadata or the live session."""
        if name in {"docs.paths", "documentation.paths"}:
            installs = self.detect_installed()
            if not installs:
                return {"local_docs_available": False, "docs": {}, "retrieval": []}
            top = installs[0]
            return {
                "local_docs_available": bool(top.extra.get("docs")),
                "install": top.to_dict(),
                "docs": top.extra.get("docs") or {},
                "retrieval": [
                    "Open Documentation/index.html for the local documentation hub.",
                    "Use Modelica/Library/python_interface/doc/searchindex.js for Python API symbol search.",
                    "Search Modelica/Library/DymolaCommands 1.21/package.mo and help/*.html for command docs.",
                ],
            }

        if name == "session.summary":
            return {
                "connected": self._dymola is not None,
                "session_id": self._session_id,
                "ui_mode": self._ui_mode,
                "install": self._install.to_dict() if self._install else None,
                "connected_at": self._connected_at,
                "last_run": self._last_run,
            }

        if name == "version":
            if self._dymola is None:
                return {"connected": False}
            return {
                "connected": True,
                "version": self._dymola.DymolaVersion(),
                "version_number": self._dymola.DymolaVersionNumber(),
            }

        if name == "license":
            if self._dymola is None:
                return {"connected": False}
            try:
                return {"connected": True, "license": self._dymola.DymolaLicenseInfo()}
            except Exception as exc:
                return {"connected": True, "error": str(exc)}

        if name == "last_error":
            if self._dymola is None:
                return {"connected": False, "last_error_log": self._last_error_log}
            try:
                return {"connected": True, "last_error_log": self._dymola.getLastErrorLog()}
            except Exception:
                return {"connected": True, "last_error_log": self._last_error_log}

        return {"error": f"unknown query: {name}"}

    def disconnect(self) -> dict:
        """Close the active Dymola session. Idempotent."""
        sid = self._session_id
        if self._dymola is None:
            self._session_id = None
            return {"ok": True, "session_id": sid, "disconnected": True}
        try:
            self._dymola.close()
        except Exception:
            pass
        self._dymola = None
        self._install = None
        self._session_id = None
        self._connected_at = None
        return {"ok": True, "session_id": sid, "disconnected": True}


__all__ = ["DymolaDriver"]
