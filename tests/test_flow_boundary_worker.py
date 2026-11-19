"""Portable flow contracts using real arrays and an injected native I/O boundary."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from hms_commander import HmsFlowBoundaryWorker, HmsFlowBoundaryWorkerError
from hms_commander.dss import DssCore

worker = importlib.import_module("hms_commander.HmsFlowBoundaryWorker")
FIXTURES = Path(__file__).parent / "fixtures" / "flow_boundary"


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def invocation(tmp_path):
    source = tmp_path / "source.json"
    source.write_bytes((FIXTURES / "portable-flow-v1.0.json").read_bytes())
    portable = json.loads(source.read_text())
    request = {
        "schema": HmsFlowBoundaryWorker.REQUEST_SCHEMA,
        "operation_id": "fixture-reach-101",
        "source": {
            "path": "source.json",
            "sha256": _sha(source),
            "selector": portable["selector"],
        },
        "model_window": {
            "start": "2026-11-18T00:00:00Z",
            "end": "2026-11-18T02:00:00Z",
            "time_zone": "UTC",
        },
        "interval_minutes": 60,
        "units": "CMS",
        "output_dss": "boundary.dss",
        "output_pathname": "/FIXTURE/REACH-101/FLOW//1HOUR/CYCLE-20261118T0000/",
        "routing": {
            "method": "identity",
            "mapping_id": "fixture-mapping",
            "target_boundary_id": "upstream-1",
            "contribution_id": "exclusive-reach-101",
        },
        "alignment_policy": "exact",
        "missing_value_policy": "reject",
        "conversion": "identity",
        "unit_policy": "identity",
        "sentinel_threshold": -1e30,
    }
    request_path, result_path = tmp_path / "request.json", tmp_path / "result.json"
    _write(request_path, request)
    return SimpleNamespace(
        source=source,
        portable=portable,
        request=request,
        request_path=request_path,
        result_path=result_path,
        output=tmp_path / "boundary.dss",
        calls=[],
    )


@pytest.fixture
def native_boundary(monkeypatch, invocation):
    frames = {}

    def write(output, pathname, times, values, *, units, data_type, interval_minutes):
        frame = pd.DataFrame({"value": np.array(values)}, index=pd.DatetimeIndex(times))
        frame.attrs.update(units=units, type=data_type, interval=interval_minutes)
        frames[str(output)] = frame
        # Portable stand-in bytes are never described as a qualified DSS fixture.
        Path(output).write_bytes(
            b"PORTABLE-NATIVE-STAND-IN\n" + np.asarray(values).tobytes()
        )
        invocation.calls.append(str(output))

    def native(request, output):
        child_request = output.parent / "request.json"
        _write(child_request, request)
        normalized, prepared = worker._load(child_request)
        return HmsFlowBoundaryWorker._materialize_in_process(
            normalized, prepared, output
        )

    monkeypatch.setattr(DssCore, "write_timeseries", write)
    monkeypatch.setattr(
        DssCore, "read_timeseries", lambda path, pathname: frames[str(path)].copy()
    )
    monkeypatch.setattr(
        DssCore, "get_catalog", lambda path: [invocation.request["output_pathname"]]
    )
    monkeypatch.setattr(HmsFlowBoundaryWorker, "_run_native", native)
    return frames


def _run(invocation):
    return HmsFlowBoundaryWorker.run(invocation.request_path, invocation.result_path)


def test_complete_source_values_bind_evidence_and_identical_retry(
    invocation, native_boundary
):
    original = invocation.source.read_bytes()
    assert _run(invocation) == 0
    result = HmsFlowBoundaryWorker.verify(
        invocation.request_path, invocation.result_path
    )
    evidence_bytes = invocation.result_path.read_bytes()
    assert result["preparation_source_sha256"] == _sha(invocation.source)
    assert "source_data_sha256" not in result
    assert result["output_sha256"] == _sha(invocation.output)
    assert result["readback"]["sample_count"] == 3
    assert (
        result["readback"]["ordinate_sha256"]
        == hashlib.sha256(
            np.asarray([0, 17.5, 9.25], dtype="<f8").tobytes()
        ).hexdigest()
    )
    assert result["source_selector"] == invocation.portable["selector"]
    assert result["routing"] == invocation.request["routing"]
    assert result["forecast_eligible"] is False
    assert _run(invocation) == 0
    assert len(invocation.calls) == 1
    assert invocation.result_path.read_bytes() == evidence_bytes
    assert invocation.source.read_bytes() == original


@pytest.mark.parametrize(
    "mutation",
    [
        lambda source: source["values"].pop(0),
        lambda source: source["timestamps"].pop(),
        lambda source: source["timestamps"].reverse(),
        lambda source: source["timestamps"].__setitem__(1, source["timestamps"][0]),
        lambda source: source["timestamps"].__setitem__(0, "2026-11-18T01:00:00Z"),
        lambda source: source["timestamps"].__setitem__(0, "2026-11-18T00:00:00"),
        lambda source: source["timestamps"].__setitem__(1, "2026-11-18T01:00:01Z"),
        lambda source: source["values"].__setitem__(1, None),
        lambda source: source["values"].__setitem__(1, True),
        lambda source: source["values"].__setitem__(1, -1e30),
        lambda source: source["values"].__setitem__(1, float("nan")),
        lambda source: source.update(units="m3/s"),
        lambda source: source.update(data_type="PER-AVER"),
        lambda source: source.update(interval_minutes=30),
        lambda source: source.update(selector="replacement-reach-or-cycle"),
    ],
)
def test_invalid_authenticated_series_refuses_before_native(
    invocation, native_boundary, mutation
):
    mutation(invocation.portable)
    _write(invocation.source, invocation.portable)
    invocation.request["source"]["sha256"] = _sha(invocation.source)
    _write(invocation.request_path, invocation.request)
    assert _run(invocation) == 2
    assert invocation.calls == []
    assert not invocation.output.exists()
    assert json.loads(invocation.result_path.read_text())["status"] == "failed"


@pytest.mark.parametrize(
    "field,value",
    [
        ("conversion", "cms_to_cfs"),
        ("alignment_policy", "interpolate"),
        ("missing_value_policy", "zero"),
        ("units", "CFS"),
        ("interval_minutes", True),
        ("output_pathname", "/FIXTURE/REACH-101/FLOW//15MIN/CYCLE/"),
        ("output_pathname", "/FIXTURE/*/FLOW//1HOUR/CYCLE/"),
        ("output_pathname", "/FIXTURE/REACH-101/STAGE//1HOUR/CYCLE/"),
        ("values", [0, 17.5, 9.25]),
    ],
)
def test_unsupported_policy_or_shadow_array_refuses(
    invocation, native_boundary, field, value
):
    invocation.request[field] = value
    _write(invocation.request_path, invocation.request)
    assert _run(invocation) == 2
    assert not invocation.calls


def test_missing_source_identity_and_midflight_drift(
    invocation, native_boundary, monkeypatch
):
    invocation.source.write_bytes(invocation.source.read_bytes() + b" ")
    assert _run(invocation) == 3
    assert not invocation.calls
    invocation.request["source"]["sha256"] = _sha(invocation.source)
    _write(invocation.request_path, invocation.request)
    original_native = HmsFlowBoundaryWorker._run_native

    def drift(request, output):
        receipt = original_native(request, output)
        invocation.source.write_bytes(invocation.source.read_bytes() + b" ")
        return receipt

    monkeypatch.setattr(HmsFlowBoundaryWorker, "_run_native", drift)
    assert _run(invocation) == 3
    assert not invocation.output.exists()
    assert not invocation.result_path.exists()


@pytest.mark.parametrize(
    "defect", ["ordinates", "timestamps", "units", "interval", "catalog"]
)
def test_native_readback_failure_cannot_publish_success(
    invocation, native_boundary, monkeypatch, defect
):
    original_read = DssCore.read_timeseries

    def corrupt(path, pathname):
        frame = original_read(path, pathname)
        if defect == "ordinates":
            frame.iloc[1, 0] += 1
        elif defect == "timestamps":
            frame.index = frame.index + pd.Timedelta(minutes=1)
        elif defect in {"units", "interval"}:
            frame.attrs[defect] = "CFS" if defect == "units" else 15
        return frame

    monkeypatch.setattr(DssCore, "read_timeseries", corrupt)
    if defect == "catalog":
        monkeypatch.setattr(
            DssCore,
            "get_catalog",
            lambda path: [
                invocation.request["output_pathname"],
                "/OTHER/REACH/FLOW//1HOUR/CYCLE/",
            ],
        )
    assert _run(invocation) == 4
    assert not invocation.output.exists()
    assert json.loads(invocation.result_path.read_text())["status"] == "failed"


@pytest.mark.parametrize(
    "field",
    [
        "source_selector",
        "preparation_source_sha256",
        "output_sha256",
        "routing",
        "readback",
        "forecast_eligible",
    ],
)
def test_tampered_receipts_refuse_reuse(invocation, native_boundary, field):
    assert _run(invocation) == 0
    result = json.loads(invocation.result_path.read_text())
    result[field] = "tampered"
    _write(invocation.result_path, result)
    prior = invocation.result_path.read_bytes()
    assert _run(invocation) == 3
    assert invocation.result_path.read_bytes() == prior
    assert len(invocation.calls) == 1
    with pytest.raises(HmsFlowBoundaryWorkerError):
        HmsFlowBoundaryWorker.verify(invocation.request_path, invocation.result_path)


def test_output_mutation_and_changed_source_need_new_outputs(
    invocation, native_boundary
):
    assert _run(invocation) == 0
    invocation.output.write_bytes(b"changed")
    assert _run(invocation) == 3
    invocation.portable["values"][1] = 20.25
    _write(invocation.source, invocation.portable)
    invocation.request["source"]["sha256"] = _sha(invocation.source)
    _write(invocation.request_path, invocation.request)
    assert _run(invocation) == 3
    invocation.request["output_dss"] = "boundary-v2.dss"
    _write(invocation.request_path, invocation.request)
    invocation.result_path = invocation.result_path.with_name("result-v2.json")
    assert _run(invocation) == 0
    assert invocation.output.read_bytes() == b"changed"


def test_explicit_physical_clock_becomes_model_wall_clock(invocation, native_boundary):
    invocation.request["model_window"]["time_zone"] = "America/Chicago"
    _write(invocation.request_path, invocation.request)
    assert _run(invocation) == 0
    result = HmsFlowBoundaryWorker.verify(
        invocation.request_path, invocation.result_path
    )
    assert result["readback"]["model_start"] == "2026-11-17T18:00:00"
    assert result["model_window"]["start"] == "2026-11-18T00:00:00+00:00"


def test_explicit_metric_alias_preserves_values_and_preparation_identity(
    invocation, native_boundary
):
    invocation.portable["units"] = "m3/s"
    _write(invocation.source, invocation.portable)
    invocation.request["source"]["sha256"] = _sha(invocation.source)
    invocation.request["unit_policy"] = "m3/s-to-CMS-alias"
    _write(invocation.request_path, invocation.request)
    assert _run(invocation) == 0
    result = HmsFlowBoundaryWorker.verify(
        invocation.request_path, invocation.result_path
    )
    assert (result["source_units"], result["native_units"]) == ("m3/s", "CMS")
    assert result["preparation_source_sha256"] == _sha(invocation.source)
    assert "source_data_sha256" not in result
    assert list(native_boundary.values())[0]["value"].tolist() == [0, 17.5, 9.25]


def test_supervision_uncertainty_retains_diagnostic_files(invocation, monkeypatch):
    def uncertain(command, **kwargs):
        folder = kwargs["output_dir"]
        _write(folder / "process.json", {"quiescent": False})
        (folder / "stderr.log").write_text("fixture native process diagnostic")
        raise RuntimeError(f"retry is unsafe: {folder / 'process.json'}")

    monkeypatch.setattr(worker, "run_owned", uncertain)
    assert _run(invocation) == 4
    result = json.loads(invocation.result_path.read_text())
    diagnostics = Path(result["diagnostics_directory"])
    assert json.loads((diagnostics / "process.json").read_text())["quiescent"] is False
    assert (diagnostics / "stderr.log").is_file()
    assert not invocation.output.exists()


def test_numeric_false_does_not_replace_boolean_eligibility(
    invocation, native_boundary
):
    assert _run(invocation) == 0
    result = json.loads(invocation.result_path.read_text())
    result["forecast_eligible"] = 0
    _write(invocation.result_path, result)
    assert _run(invocation) == 3


def test_versioned_fixture_loads_without_native_execution():
    request, prepared = worker._load(FIXTURES / "request-v1.0.json")
    assert request["source"]["sha256"] == _sha(FIXTURES / "portable-flow-v1.0.json")
    assert prepared["values"].tolist() == [0, 17.5, 9.25]


def test_completed_stage_retirement_failure_preserves_success(
    invocation, native_boundary, monkeypatch
):
    original_rmtree = worker.shutil.rmtree

    def retained(path, *args, **kwargs):
        if Path(path).name.startswith(".flow-boundary-"):
            raise PermissionError("fixture scanner holds completed-stage handle")
        return original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(worker.shutil, "rmtree", retained)
    assert _run(invocation) == 0
    assert (
        HmsFlowBoundaryWorker.verify(invocation.request_path, invocation.result_path)[
            "status"
        ]
        == "succeeded"
    )
    assert _run(invocation) == 0


def test_dst_ambiguity_refuses_before_native(invocation, native_boundary):
    invocation.request["model_window"] = {
        "start": "2026-11-01T06:00:00Z",
        "end": "2026-11-01T08:00:00Z",
        "time_zone": "America/Chicago",
    }
    _write(invocation.request_path, invocation.request)
    assert _run(invocation) == 2
    assert not invocation.calls


def test_source_duplicate_keys_refuse_even_with_matching_hash(
    invocation, native_boundary
):
    invocation.source.write_text(
        invocation.source.read_text().replace(
            '"units": "CMS"', '"units": "CFS", "units": "CMS"'
        )
    )
    invocation.request["source"]["sha256"] = _sha(invocation.source)
    _write(invocation.request_path, invocation.request)
    assert _run(invocation) == 2
    assert not invocation.calls


def test_concurrent_writers_publish_one_success(
    invocation, native_boundary, monkeypatch
):
    barrier = threading.Barrier(2)
    original_native = HmsFlowBoundaryWorker._run_native

    def synchronized(request, output):
        receipt = original_native(request, output)
        barrier.wait(timeout=10)
        return receipt

    monkeypatch.setattr(HmsFlowBoundaryWorker, "_run_native", synchronized)
    with ThreadPoolExecutor(max_workers=2) as executor:
        codes = list(executor.map(lambda _: _run(invocation), range(2)))
    assert sorted(codes) == [0, 3]
    assert (
        HmsFlowBoundaryWorker.verify(invocation.request_path, invocation.result_path)[
            "status"
        ]
        == "succeeded"
    )
    # The losing attempt retains its unaccepted stage for diagnosis.
    assert len(list(invocation.output.parent.glob(".flow-boundary-*"))) == 1
    assert not list(invocation.output.parent.glob(".flow-evidence-*"))


def test_unaccepted_existing_output_and_alias_are_preserved(
    invocation, native_boundary
):
    invocation.output.write_bytes(b"prior partial output")
    assert _run(invocation) == 3
    assert invocation.output.read_bytes() == b"prior partial output"
    assert not invocation.result_path.exists()
    invocation.result_path = invocation.output
    assert _run(invocation) == 2
    assert invocation.output.read_bytes() == b"prior partial output"


def test_owned_child_failure_releases_before_publication(invocation, monkeypatch):
    commands = []

    def failed(command, **kwargs):
        commands.append((command, kwargs))
        return SimpleNamespace(timed_out=True, returncode=-1, stderr="timeout")

    monkeypatch.setattr(worker, "run_owned", failed)
    assert _run(invocation) == 4
    command, settings = commands[0]
    assert "-I" in command
    assert settings["timeout"] == 300
    assert "PYTHONPATH" not in settings["env"]
    assert not invocation.output.exists()


@pytest.mark.requires_java
@pytest.mark.parametrize(
    "source_units,native_units,unit_policy",
    [
        ("CMS", "CMS", "identity"),
        ("CFS", "CFS", "identity"),
        ("m3/s", "CMS", "m3/s-to-CMS-alias"),
    ],
)
def test_native_flow_boundary_qualification(
    invocation, source_units, native_units, unit_policy
):
    """Opt-in fresh DSS write/readback; retained pytest directory is evidence."""
    if os.environ.get("HMS_FLOW_NATIVE_QUALIFICATION") != "1":
        pytest.skip(
            "Native qualification requires explicit HMS_FLOW_NATIVE_QUALIFICATION=1"
        )
    invocation.portable["units"] = source_units
    _write(invocation.source, invocation.portable)
    invocation.request["source"]["sha256"] = _sha(invocation.source)
    invocation.request.update(units=native_units, unit_policy=unit_policy)
    _write(invocation.request_path, invocation.request)
    assert _run(invocation) == 0
    first = invocation.result_path.read_bytes()
    assert (
        HmsFlowBoundaryWorker.verify(invocation.request_path, invocation.result_path)[
            "readback"
        ]["sample_count"]
        == 3
    )
    assert _run(invocation) == 0
    assert invocation.result_path.read_bytes() == first
