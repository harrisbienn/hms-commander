"""Real isolated Python children with synthetic packages; no native I/O."""

import importlib
import json
from pathlib import Path
import subprocess

import pytest
import test_forecast_grid

from hms_commander import _process

bundle = test_forecast_grid.bundle
grid_module = importlib.import_module("hms_commander.HmsForecastGrid")
flow_module = importlib.import_module("hms_commander.HmsFlowBoundaryWorker")


class _CommandCaptured(RuntimeError):
    pass


def _capture_command(monkeypatch, bundle, package, stage):
    captured = {}
    # Each launcher must bind its own package, including a source checkout that
    # differs from the version normally visible to the selected interpreter.
    for module in (grid_module, flow_module, _process):
        monkeypatch.setattr(
            module, "__file__", str(package / Path(module.__file__).name)
        )

    def capture(command, **settings):
        captured.update(command=command, settings=settings)
        raise _CommandCaptured()

    if stage == "grid":
        monkeypatch.setattr(grid_module, "run_owned", capture)
        result = bundle / "result with spaces.json"
        with pytest.raises(_CommandCaptured):
            grid_module.HmsForecastGrid.materialize(bundle / "request.json", result)
        captured["arguments"] = [
            "--request",
            str(bundle / "request.json"),
            "--result",
            str(result.with_name(result.name + ".worker.json")),
        ]
        captured["module"] = "HmsGridWriterWorker"
    else:
        monkeypatch.setattr(flow_module, "run_owned", capture)
        output = bundle / "flow stage" / "boundary.dss"
        output.parent.mkdir()
        with pytest.raises(_CommandCaptured):
            flow_module.HmsFlowBoundaryWorker._run_native({}, output)
        captured["arguments"] = [
            str(output.parent / "request.json"),
            str(output),
            str(output.parent / "readback.json"),
        ]
        captured["module"] = "HmsFlowBoundaryWorker"
    return captured


def _synthetic_package(root):
    package = root / "hms_commander"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        "import pathlib, calendar\nfrom ._origin import MARKER\n", encoding="utf-8"
    )
    (package / "_origin.py").write_text("MARKER = 'selected owner'\n", encoding="utf-8")
    for name, entry in (
        ("HmsGridWriterWorker", "main"),
        ("HmsFlowBoundaryWorker", "_child_main"),
    ):
        (package / f"{name}.py").write_text(
            "import calendar, json, pathlib, sys\n"
            "import hms_commander\n"
            f"def {entry}():\n"
            "    print(json.dumps({\n"
            "        'marker': hms_commander.MARKER,\n"
            "        'package': hms_commander.__file__,\n"
            "        'package_path': list(hms_commander.__path__),\n"
            "        'worker': __file__,\n"
            "        'arguments': sys.argv[1:],\n"
            "        'isolated': sys.flags.isolated,\n"
            "        'path_unchanged': sys.path == "
            "sys.modules['__main__'].original_paths,\n"
            "        'pathlib': pathlib.__file__,\n"
            "        'calendar': calendar.__file__,\n"
            "    }))\n"
            "    return 0\n",
            encoding="utf-8",
        )
    return package


def _run_command(captured, prefix):
    command = list(captured["command"])
    code_index = command.index("-c") + 1
    command[code_index] = prefix + command[code_index]
    return subprocess.run(
        command,
        cwd=captured["settings"]["cwd"],
        env=captured["settings"]["env"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


@pytest.mark.parametrize("stage", ["grid", "flow-boundary"])
@pytest.mark.parametrize("layout", ["source checkout", "site-packages"])
@pytest.mark.parametrize("shadow", ["pathlib", "calendar"])
def test_child_preserves_stdlib_and_selected_package(
    monkeypatch, bundle, tmp_path, stage, layout, shadow
):
    selected_root = tmp_path / layout / "selected owner's files"
    package = _synthetic_package(selected_root)
    (selected_root / f"{shadow}.py").write_text(
        "from collections import Sequence\n"
        "raise RuntimeError('third-party standard-library shadow imported')\n",
        encoding="utf-8",
    )
    competing_root = tmp_path / "competing installation"
    competitor = competing_root / "hms_commander"
    competitor.mkdir(parents=True)
    (competitor / "__init__.py").write_text(
        "raise RuntimeError('wrong owner selected')\n", encoding="utf-8"
    )
    captured = _capture_command(monkeypatch, bundle, package, stage)
    # A competing owner is importable without executing it. Merely appending the
    # selected root would choose that owner; promoting the selected root would
    # instead import its unrelated standard-library shadow.
    prefix = f"import sys; sys.path.insert(0, {str(competing_root)!r}); "
    if layout == "site-packages":
        prefix += f"sys.path.append({str(selected_root)!r}); "
    # Editable-install startup hooks may already import pathlib. Exercise a
    # fresh import, as occurs in the affected noneditable installation.
    prefix += f"sys.modules.pop({shadow!r}, None); "
    prefix += "original_paths = list(sys.path); "
    completed = _run_command(captured, prefix)
    assert completed.returncode == 0, completed.stderr
    evidence = json.loads(completed.stdout)
    assert evidence["marker"] == "selected owner"
    assert Path(evidence["package"]) == package / "__init__.py"
    assert evidence["package_path"] == [str(package)]
    assert Path(evidence["worker"]) == package / (captured["module"] + ".py")
    assert evidence["arguments"] == captured["arguments"]
    assert evidence["isolated"] == 1 and evidence["path_unchanged"]
    for name in ("pathlib", "calendar"):
        assert not Path(evidence[name]).is_relative_to(selected_root)


@pytest.mark.parametrize("stage", ["grid", "flow-boundary"])
def test_missing_selected_package_does_not_fall_back(
    monkeypatch, bundle, tmp_path, stage
):
    package = tmp_path / "missing owner" / "hms_commander"
    captured = _capture_command(monkeypatch, bundle, package, stage)
    completed = _run_command(captured, "")
    assert completed.returncode != 0
    assert "FileNotFoundError" in completed.stderr
    assert str(package / "__init__.py") in completed.stderr.replace("\\\\", "\\")


@pytest.mark.parametrize("stage", ["grid", "flow-boundary"])
def test_preloaded_owner_is_refused(monkeypatch, bundle, tmp_path, stage):
    package = _synthetic_package(tmp_path / "selected owner")
    captured = _capture_command(monkeypatch, bundle, package, stage)
    prefix = (
        "import sys, types; "
        "sys.modules['hms_commander.HmsGridWriterWorker'] = types.ModuleType('other'); "
    )
    completed = _run_command(captured, prefix)
    assert completed.returncode != 0
    assert "HMS Commander was imported before worker bootstrap" in completed.stderr


def test_unsupported_worker_is_refused():
    with pytest.raises(ValueError, match="Unsupported isolated HMS worker"):
        _process._isolated_worker_command("unselected.module")
