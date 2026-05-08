from __future__ import annotations

import os
import re
from pathlib import Path

from sim_plugin_dymola import DymolaDriver


def _fake_install(tmp_path: Path) -> Path:
    install = tmp_path / "Dymola 2026x Refresh 1"
    launcher = install / "bin" / ("Dymola.exe" if os.name == "nt" else "dymola")
    launcher.parent.mkdir(parents=True)
    launcher.write_text("", encoding="utf-8")
    wheel_dir = install / "Modelica" / "Library" / "python_interface"
    wheel_dir.mkdir(parents=True)
    (wheel_dir / "dymola-2026.1-py3-none-any.whl").write_text("", encoding="utf-8")
    (wheel_dir / "doc").mkdir()
    (wheel_dir / "doc" / "index.html").write_text("<html></html>", encoding="utf-8")
    docs_dir = install / "Documentation"
    docs_dir.mkdir()
    (docs_dir / "index.html").write_text("<html></html>", encoding="utf-8")
    return install


class _FakeDymola:
    log_text = "DymolaVersion();\n = true\n"
    script_result = True
    model_open_result = True
    check_model_result = True
    last_error_log = ""

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.commands: list[str] = []

    def ExecuteCommand(self, command: str):
        self.commands.append(command)
        match = re.match(r'savelog\("(.+)"\)', command)
        if match:
            Path(match.group(1)).write_text(self.log_text, encoding="utf-8")
        return True

    def RunScript(self, script: str, silent: bool = False):
        return self.script_result

    def openModel(self, path: str, mustRead: bool = True, changeDirectory: bool = True):
        return self.model_open_result

    def checkModel(self, problem: str):
        return self.check_model_result

    def getLastErrorLog(self):
        return self.last_error_log

    def DymolaVersion(self):
        return "Dymola Version 2026x Refresh 1, 2026-04-08"

    def DymolaVersionNumber(self):
        return 2026.1

    def DymolaLicenseInfo(self):
        return "license ok"

    def close(self):
        return 0


def _patch_fake_dymola(monkeypatch, tmp_path: Path, fake_cls=_FakeDymola) -> Path:
    from sim_plugin_dymola import driver as drv

    install = _fake_install(tmp_path)
    monkeypatch.setattr(drv, "_INSTALL_FINDERS", [lambda: [(install, "test:synth")]])
    monkeypatch.setattr(drv, "_import_dymola_interface", lambda install: fake_cls)
    return install


class TestDetect:
    def test_detects_mos_and_mo_files(self, tmp_path: Path) -> None:
        driver = DymolaDriver()
        mos = tmp_path / "run.mos"
        mo = tmp_path / "Model.mo"
        mos.write_text('simulateModel("Demo.Model");\n', encoding="utf-8")
        mo.write_text("model Demo\nend Demo;\n", encoding="utf-8")

        assert driver.detect(mos) is True
        assert driver.detect(mo) is True

    def test_detects_python_dymola_snippet(self, tmp_path: Path) -> None:
        script = tmp_path / "run_dymola.py"
        script.write_text(
            "from dymola.dymola_interface import DymolaInterface\n"
            "dymola = DymolaInterface()\n",
            encoding="utf-8",
        )

        assert DymolaDriver().detect(script) is True

    def test_rejects_unrelated_file(self, tmp_path: Path) -> None:
        script = tmp_path / "plain.py"
        script.write_text("print('hello')\n", encoding="utf-8")

        assert DymolaDriver().detect(script) is False


class TestLint:
    def test_lint_missing_and_empty_files(self, tmp_path: Path) -> None:
        driver = DymolaDriver()

        missing = driver.lint(tmp_path / "missing.mos")
        assert missing.ok is False
        assert missing.diagnostics[0].level == "error"

        empty_path = tmp_path / "empty.mos"
        empty_path.write_text("", encoding="utf-8")
        empty = driver.lint(empty_path)
        assert empty.ok is False
        assert empty.diagnostics[0].message == "script is empty"

    def test_lint_python_syntax_error(self, tmp_path: Path) -> None:
        script = tmp_path / "bad.py"
        script.write_text("from dymola import (\n", encoding="utf-8")

        result = DymolaDriver().lint(script)

        assert result.ok is False
        assert result.diagnostics[0].level == "error"
        assert result.diagnostics[0].line == 1


class TestConnect:
    def test_connect_without_solver_is_clear(self, monkeypatch) -> None:
        from sim_plugin_dymola import driver as drv

        monkeypatch.setattr(drv, "_INSTALL_FINDERS", [lambda: []])

        info = DymolaDriver().connect()

        assert info.status == "not_installed"
        assert info.solver == "dymola"
        assert "No Dymola" in info.message

    def test_connect_with_local_api_wheel_is_ok(self, tmp_path: Path, monkeypatch) -> None:
        _patch_fake_dymola(monkeypatch, tmp_path)

        info = DymolaDriver().connect()

        assert info.status == "ok"
        assert info.solver_version == "2026x Refresh 1"
        assert "API wheel 2026.1" in info.message


class TestParseOutput:
    def test_parse_output_prefers_last_json_object(self) -> None:
        parsed = DymolaDriver().parse_output('progress\n{"temperature": 42}\n')

        assert parsed == {"temperature": 42}

    def test_parse_output_returns_empty_dict_without_json(self) -> None:
        assert DymolaDriver().parse_output("first\nlast\n") == {}


class TestRunFile:
    def test_run_file_without_solver_is_clear(self, tmp_path: Path, monkeypatch) -> None:
        from sim_plugin_dymola import driver as drv

        monkeypatch.setattr(drv, "_INSTALL_FINDERS", [lambda: []])
        script = tmp_path / "run.mos"
        script.write_text('simulateModel("Demo.Model");\n', encoding="utf-8")

        result = DymolaDriver().run_file(script)

        assert result.exit_code != 0
        assert result.ok is False
        assert result.solver == "dymola"
        assert "no Dymola installation detected" in result.stderr

    def test_run_file_executes_mos_with_dymola_api(self, tmp_path: Path, monkeypatch) -> None:
        _patch_fake_dymola(monkeypatch, tmp_path)
        script = tmp_path / "run.mos"
        script.write_text("DymolaVersion();\n", encoding="utf-8")

        result = DymolaDriver().run_file(script)

        assert result.exit_code == 0
        assert result.ok is True
        parsed = DymolaDriver().parse_output(result.stdout)
        assert parsed["ok"] is True
        assert parsed["kind"] == "mos"
        assert any(entry["path"].endswith("dymola.log") for entry in result.artifacts)

    def test_run_file_fails_on_false_dymola_command(self, tmp_path: Path, monkeypatch) -> None:
        class FailingDymola(_FakeDymola):
            log_text = "simulateModel(\"Demo.Model\");\n = false\nNo compiler selected.\n"

        _patch_fake_dymola(monkeypatch, tmp_path, FailingDymola)
        script = tmp_path / "run.mos"
        script.write_text('simulateModel("Demo.Model");\n', encoding="utf-8")

        result = DymolaDriver().run_file(script)

        assert result.exit_code == 1
        assert result.ok is False
        assert "Dymola command returned false" in result.errors
        assert any("No compiler selected" in error for error in result.errors)

    def test_run_file_opens_and_checks_modelica_file(self, tmp_path: Path, monkeypatch) -> None:
        _patch_fake_dymola(monkeypatch, tmp_path)
        script = tmp_path / "Hello.mo"
        script.write_text(
            "model Hello\n"
            "  Real x(start=1);\n"
            "equation\n"
            "  der(x) = -x;\n"
            "end Hello;\n",
            encoding="utf-8",
        )

        result = DymolaDriver().run_file(script)

        assert result.exit_code == 0
        parsed = DymolaDriver().parse_output(result.stdout)
        assert parsed["kind"] == "modelica"
        assert parsed["opened"] is True
        assert parsed["checked"] is True
        assert parsed["class_name"] == "Hello"


class TestInstallScan:
    def test_scan_uses_strategy_chain(self, tmp_path: Path, monkeypatch) -> None:
        from sim_plugin_dymola import driver as drv

        install = tmp_path / "Dymola 2026x"
        (install / "bin").mkdir(parents=True)
        launcher = install / "bin" / ("Dymola.exe" if os.name == "nt" else "dymola")
        launcher.write_text("#!/bin/sh\n", encoding="utf-8")

        monkeypatch.setattr(drv, "_INSTALL_FINDERS", [lambda: [(install, "test:synth")]])

        installs = drv._scan_dymola_installs()

        assert len(installs) == 1
        assert installs[0].name == "dymola"
        assert installs[0].version == "2026x"
        assert installs[0].source == "test:synth"

    def test_scan_reports_api_wheel_and_docs(self, tmp_path: Path, monkeypatch) -> None:
        from sim_plugin_dymola import driver as drv

        install = _fake_install(tmp_path)
        monkeypatch.setattr(drv, "_INSTALL_FINDERS", [lambda: [(install, "test:synth")]])

        installs = drv._scan_dymola_installs()

        assert installs[0].extra["api_wheel_version"] == "2026.1"
        assert installs[0].extra["api_wheel"].endswith("dymola-2026.1-py3-none-any.whl")
        docs_index = installs[0].extra["docs_index"].replace("\\", "/")
        assert docs_index.endswith("Documentation/index.html")
