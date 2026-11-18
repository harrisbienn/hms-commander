"""Synthetic DSS runtime admission; no JVM, native library or network is used."""

import importlib
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from test_grid_writer_worker import _request

from hms_commander import HmsGridWriterWorker
from hms_commander._process import dss_environment, native_environment

POLICY = "ras-commander/dss-runtime-trust/1.0.0"
RAS_ENVIRONMENT = {
    "RAS_DSS_CACHE_DIR": "fixture-cache",
    "RAS_DSS_TRUST_MANIFEST": "fixture-trust.json",
    "RAS_DSS_ALLOW_DOWNLOADS": "0",
}


@pytest.mark.parametrize("download_policy", [None, "0", "1"])
def test_dss_environment_preserves_only_selected_ras_policy(
    monkeypatch, download_policy
):
    for name, value in RAS_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    if download_policy is None:
        monkeypatch.delenv("RAS_DSS_ALLOW_DOWNLOADS")
    else:
        monkeypatch.setenv("RAS_DSS_ALLOW_DOWNLOADS", download_policy)
    monkeypatch.setenv("HMS_DSS_ALLOW_DOWNLOADS", "1")
    monkeypatch.setenv("RAS_DSS_UNREVIEWED_SETTING", "excluded")
    monkeypatch.setenv("JAVA_TOOL_OPTIONS", "-Duntrusted=true")
    monkeypatch.setenv("CLASSPATH", "untrusted.jar")
    environment = dss_environment()
    assert environment["RAS_DSS_CACHE_DIR"] == str(Path("fixture-cache").absolute())
    assert environment["RAS_DSS_TRUST_MANIFEST"] == str(
        Path("fixture-trust.json").absolute()
    )
    assert environment.get("RAS_DSS_ALLOW_DOWNLOADS") == download_policy
    assert environment["HMS_DSS_ALLOW_DOWNLOADS"] == "1"
    assert "RAS_DSS_UNREVIEWED_SETTING" not in environment
    assert "JAVA_TOOL_OPTIONS" not in environment
    assert "CLASSPATH" not in environment
    assert not set(RAS_ENVIRONMENT).intersection(native_environment())


@pytest.mark.parametrize("policy", [None, "unsupported/2", POLICY])
def test_grid_worker_requires_ras_policy_before_native_io(
    tmp_path, monkeypatch, policy
):
    """An older package cannot silently ignore the forwarded sealed-cache policy."""
    package = ModuleType("ras_commander")
    module = ModuleType("ras_commander.dss._hec_monolith")
    module.HecMonolithDownloader = SimpleNamespace(TRUST_POLICY=policy)
    calls = []

    def write(path, pathname, frames, times, grid_info):
        calls.append(path)
        Path(path).write_bytes(b"synthetic grid, not a DSS file")
        return ["one", "two"]

    package.RasDss = SimpleNamespace(write_grid_timeseries=write)
    monkeypatch.setitem(sys.modules, "ras_commander", package)
    monkeypatch.setitem(sys.modules, "ras_commander.dss._hec_monolith", module)
    request, result, output = _request(tmp_path)
    status = HmsGridWriterWorker.run(request, result)
    payload = json.loads(result.read_text())
    if policy == POLICY:
        assert status == 0 and calls == [output]
    else:
        assert status == 2 and calls == []
        assert not output.exists()
        assert "DSS runtime trust policy" in payload["error"]["message"]


@pytest.mark.parametrize("route", ["HmsSpatialTransfer", "HmsSubbasinTransfer"])
@pytest.mark.parametrize("outcome", ["success", "timeout", "unreconciled"])
def test_grid_launch_uses_owned_isolated_runtime(tmp_path, monkeypatch, route, outcome):
    """Both grid launchers enforce isolation and retain the exact selected policy."""
    module = importlib.import_module("hms_commander." + route)
    namespace = getattr(module, route)
    for name, value in RAS_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("PYTHONPATH", "untrusted")
    monkeypatch.setenv("JAVA_TOOL_OPTIONS", "-Duntrusted=true")
    observed = []

    def run(command, **kwargs):
        observed.append(command)
        assert command[:4] == [
            sys.executable,
            "-I",
            "-m",
            "hms_commander.HmsGridWriterWorker",
        ]
        assert kwargs["timeout"] == 600
        assert kwargs["cwd"] == kwargs["output_dir"] == tmp_path
        for name, value in RAS_ENVIRONMENT.items():
            expected = (
                value
                if name.endswith("ALLOW_DOWNLOADS")
                else str(Path(value).absolute())
            )
            assert kwargs["env"][name] == expected
        assert "PYTHONPATH" not in kwargs["env"]
        assert "JAVA_TOOL_OPTIONS" not in kwargs["env"]
        request = json.loads(Path(command[command.index("--request") + 1]).read_text())
        Path(request["output_dss"]).write_bytes(b"synthetic completed artifact")
        result = {
            "status": "succeeded",
            "verification": {
                "status": "verified",
                "weighted_depth_area_by_support": {"one": [1.0]},
            },
        }
        Path(command[command.index("--result") + 1]).write_text(json.dumps(result))
        return SimpleNamespace(
            returncode=0,
            timed_out=outcome == "timeout",
            quiescent=outcome != "unreconciled",
            stderr="",
        )

    monkeypatch.setattr(module, "run_owned", run)
    start = datetime(2020, 1, 1)
    boundaries = [start, start + timedelta(minutes=5)]
    extras = (
        {}
        if route == "HmsSpatialTransfer"
        else {
            "support_ids": ["one"],
            "support_areas": np.ones((1, 1, 1)),
            "readback_absolute_value_tolerance": 0.0,
            "source_volumes": {"one": np.asarray([1.0])},
            "interval_ends": pd.DatetimeIndex(boundaries[1:]),
            "depth_unit_meters": 1.0,
            "volume_tolerance": {
                "absolute_cubic_meters": 0.0,
                "relative_fraction": 0.0,
            },
        }
    )

    def execute():
        return namespace._write_grid_subprocess(
            tmp_path / "grid.dss",
            "/A/B/PRECIPITATION///F/",
            np.ones((1, 1, 1)),
            boundaries,
            {
                "units": "MM",
                "data_type": "PER-CUM",
                "cell_size": 1.0,
                "origin": [0.0, 0.0],
                "crs": "EPSG:5070",
            },
            **extras,
        )

    if outcome == "success":
        assert execute()["status"] == "succeeded"
        assert (tmp_path / "grid.dss").is_file()
    else:
        error = TimeoutError if outcome == "timeout" else RuntimeError
        with pytest.raises(error, match="exceeded|reconciled"):
            execute()
        if route == "HmsSubbasinTransfer":
            assert not (tmp_path / "grid.dss").exists()
    assert len(observed) == 1


@pytest.mark.parametrize("route", ["fingerprint", "external_preflight"])
@pytest.mark.parametrize("policy", [None, "unsupported/2", POLICY])
def test_direct_grid_readers_admit_policy_before_native_io(
    tmp_path, monkeypatch, route, policy
):
    """Companion admission also covers retained caller-process read routes."""
    package = ModuleType("ras_commander")
    module = ModuleType("ras_commander.dss._hec_monolith")
    module.HecMonolithDownloader = SimpleNamespace(TRUST_POLICY=policy)
    calls = []

    def catalog(path):
        calls.append(path)
        raise RuntimeError("synthetic catalog boundary reached")

    package.RasDss = SimpleNamespace(get_catalog=catalog)
    monkeypatch.setitem(sys.modules, "ras_commander", package)
    monkeypatch.setitem(sys.modules, "ras_commander.dss._hec_monolith", module)
    source = tmp_path / "input.dss"
    source.write_bytes(b"synthetic input, not a DSS file")
    expected = (
        "synthetic catalog boundary" if policy == POLICY else "DSS runtime trust policy"
    )
    if route == "fingerprint":
        spatial = importlib.import_module("hms_commander.HmsSpatialTransfer")
        # This fixture tests admission, independently of CRS/grid qualification.
        projection = ModuleType("pyproj")
        projection.CRS = object
        monkeypatch.setitem(sys.modules, "pyproj", projection)
        monkeypatch.setattr(spatial, "_grid_definition", lambda *args, **kwargs: {})
        with pytest.raises(RuntimeError, match=expected):
            spatial.HmsSpatialTransfer.read_fingerprint_cube(
                source,
                "/A/B/PRECIP///F/",
                {},
                model_start=datetime(2020, 1, 1),
                model_end=datetime(2020, 1, 1, 1),
                interval_minutes=60,
            )
    else:
        from hms_commander._external_dss import preflight
        from hms_commander.dss.hms_dss import HmsDss

        monkeypatch.setattr(HmsDss, "get_catalog", catalog)
        (tmp_path / "project.grid").write_text(
            "Grid: Rain\n     Data Source Type: External DSS\n"
            "     Filename: input.dss\n     Pathname: /A/B/PRECIP///F/\nEnd:\n"
        )
        report = preflight(tmp_path, True)
        assert not report["passed"]
        assert expected in report["references"][0]["issues"][0]
    assert calls == ([source] if policy == POLICY else [])
