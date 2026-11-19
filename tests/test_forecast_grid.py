"""Forecast-grid admission with real arrays and only native DSS I/O replaced."""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from hms_commander import HmsForecastGrid, HmsGridWriterWorker
from hms_commander._forecast_grid import _digest, _sha256

api_module = importlib.import_module("hms_commander.HmsForecastGrid")


def _save(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _repin(folder, *, domain=False):
    source_path = folder / "source.json"
    source = json.loads(source_path.read_bytes())
    for identity in [
        source["data"],
        source["required_mask"],
        source["normalization_policy"],
        source["domain"]["owner_domain"],
        *[row["export"] for row in source["segments"]],
    ]:
        identity["sha256"] = _sha256(folder / identity["path"])
    _save(source_path, source)
    request_path = folder / "request.json"
    request = json.loads(request_path.read_bytes())
    request["source"]["sha256"] = _sha256(source_path)
    if domain:
        request["expected_domain_sha256"] = _digest(source["domain"])
    _save(request_path, request)


@pytest.fixture
def bundle(tmp_path):
    fixture = json.loads(
        (Path(__file__).parent / "fixtures/forecast_grid/v2.json").read_bytes()
    )
    np.save(
        tmp_path / "frames.npy",
        np.array(fixture["frames"], dtype=np.float32),
        allow_pickle=False,
    )
    np.save(
        tmp_path / "mask.npy", np.array(fixture["mask"], dtype=bool), allow_pickle=False
    )
    for name in ("owner-domain", "policy", "observed", "forecast"):
        _save(tmp_path / f"{name}.json", {"fixture_evidence": name})
    _save(tmp_path / "source.json", fixture["source"])
    _save(tmp_path / "request.json", fixture["request"])
    _repin(tmp_path, domain=True)
    return tmp_path


@pytest.fixture
def native_io(monkeypatch):
    # Import the real accepted companion and keep its trust-capability check active.
    from ras_commander import RasDss

    state = SimpleNamespace(calls=0, records={}, alteration=None)

    def write(path, pathname, frames, times, grid_info, **kwargs):
        state.calls += 1
        assert kwargs == {"dss_version": 7}
        assert grid_info["compression"] == "ZLIB"
        assert times[0].tzinfo is None  # Explicit UTC conversion at the native edge.
        Path(path).write_bytes(b"offline-native-boundary-fixture")
        if state.alteration == "sidecar":
            Path(path).with_suffix(".log").write_text("native diagnostics")
        if state.alteration == "partial":
            raise RuntimeError("simulated native interruption")
        paths = []
        for index, frame in enumerate(frames):
            parts = pathname.split("/")
            parts[4] = times[index].strftime("%d%b%Y:%H%M").upper()
            parts[5] = times[index + 1].strftime("%d%b%Y:%H%M").upper()
            record = "/".join(parts)
            paths.append(record)
            state.records[record] = {
                "data": frame.astype(np.float32),
                "units": "MM",
                "data_type": "PER-CUM",
                "shape": frame.shape,
                "cell_size": grid_info["cellsize"],
                "crs": grid_info["crs"],
                "start_time": times[index],
                "end_time": times[index + 1],
                "metadata": {
                    "origin": grid_info["origin"],
                    "shape": frame.shape,
                    "number_missing": int(np.isnan(frame).sum()),
                    "timing": {"start": parts[4], "end": parts[5]},
                },
            }
        return paths

    def catalog(path):
        paths = list(state.records)
        if state.alteration == "extra_record":
            paths.append("/EXTRA/EXTRA/EXTRA///EXTRA/")
        if state.alteration == "missing_record":
            paths.pop()
        return pd.DataFrame({"pathname": paths})

    def read(path, pathname):
        record = dict(state.records[pathname])
        record["metadata"] = dict(record["metadata"])
        record["data"] = record["data"].copy()
        field = state.alteration
        if field == "missing_required":
            record["data"][0, 0] = np.nan
        elif field == "outside_zero":
            record["data"][1, 0] = 0
        elif field == "changed_value":
            record["data"][0, 0] += 1
        elif field == "units":
            record["units"] = "IN"
        elif field == "data_type":
            record["data_type"] = "INST-VAL"
        elif field == "shape":
            record["data"] = record["data"][:, :1]
        elif field == "crs":
            record["crs"] = "EPSG:3857"
        elif field == "origin":
            record["metadata"]["origin"] = [0, 0]
        elif field == "header_window":
            record["metadata"]["timing"] = {
                "start": "01NOV2026:2300",
                "end": "01NOV2026:2300",
            }
        elif field == "header_missing":
            record["metadata"]["number_missing"] = 0
        elif field == "cell_size":
            record["cell_size"] = 2000
        elif field == "window":
            record["end_time"] = record["start_time"]
        return record

    monkeypatch.setattr(RasDss, "write_grid_timeseries", write)
    monkeypatch.setattr(RasDss, "get_catalog", catalog)
    monkeypatch.setattr(RasDss, "read_grid", read)
    return state


@pytest.fixture
def owned_child(monkeypatch, native_io):
    state = SimpleNamespace(calls=0, timed_out=False, quiescent=True, after=None)

    def run(args, **kwargs):
        state.calls += 1
        request = Path(args[args.index("--request") + 1])
        result = Path(args[args.index("--result") + 1])
        assert "-I" in args
        assert (
            repr(str(Path(api_module.__file__).resolve().parents[1]))
            in args[args.index("-c") + 1]
        )
        assert kwargs["timeout"] > 0
        # A portable test of the existing child entry point, without launching Java.
        returncode = HmsGridWriterWorker.run(request, result)
        assert not (request.parent / "rainfall.dss").exists()
        if state.after:
            state.after()
        return SimpleNamespace(
            returncode=returncode, timed_out=state.timed_out, quiescent=state.quiescent
        )

    monkeypatch.setattr(api_module, "run_owned", run)
    return state


def test_complete_grid_preserves_true_zero_and_nodata_and_retries(
    bundle, owned_child, native_io
):
    before = {path.name: path.read_bytes() for path in bundle.iterdir()}
    request, result = bundle / "request.json", bundle / "result.json"
    accepted = HmsForecastGrid.materialize(request, result)
    assert accepted["record_count"] == 2
    assert accepted["status"] == "succeeded" and accepted["process_quiescent"] is True
    assert accepted["records"][0]["required_cell_count"] == 3
    assert accepted["records"][0]["outside_nodata_cells"] == 1
    assert accepted["records"][0]["max_absolute_error_mm"] == 0
    assert not accepted["forecast_eligible"]
    assert native_io.records[accepted["records"][0]["pathname"]]["data"][0, 0] == 0
    assert HmsForecastGrid.verify(request, result) == accepted
    assert HmsForecastGrid.materialize(request, result) == accepted
    assert owned_child.calls == native_io.calls == 1
    assert all(
        (bundle / name).read_bytes() == content for name, content in before.items()
    )


@pytest.mark.parametrize(
    "alteration",
    [
        "missing_required",
        "outside_zero",
        "changed_value",
        "units",
        "data_type",
        "shape",
        "crs",
        "origin",
        "cell_size",
        "window",
        "header_window",
        "header_missing",
        "extra_record",
        "missing_record",
        "partial",
    ],
)
def test_native_readback_failures_never_publish(
    bundle, owned_child, native_io, alteration
):
    native_io.alteration = alteration
    with pytest.raises(RuntimeError, match="worker failed"):
        HmsForecastGrid.materialize(bundle / "request.json", bundle / "result.json")
    assert not (bundle / "rainfall.dss").exists()
    assert not (bundle / "result.json").exists()
    with pytest.raises(FileExistsError, match="partial"):
        HmsForecastGrid.materialize(bundle / "request.json", bundle / "result.json")
    assert native_io.calls == 1


@pytest.mark.parametrize(
    "alteration",
    [
        "required_nan",
        "outside_zero",
        "negative",
        "mask_narrowing",
        "domain_narrowing",
        "wrong_crs",
        "wrong_row_order",
        "units",
        "step",
        "gap",
        "naive_clock",
        "offset_clock",
        "short_window",
        "incomplete_prefix",
        "source_bytes",
        "frame_bytes",
        "policy_bytes",
    ],
)
def test_bad_inputs_fail_before_native_io(bundle, native_io, alteration):
    source = json.loads((bundle / "source.json").read_bytes())
    request = json.loads((bundle / "request.json").read_bytes())
    repin = True
    if alteration in {"required_nan", "outside_zero", "negative"}:
        frames = np.load(bundle / "frames.npy")
        if alteration == "required_nan":
            frames[0, 0, 0] = np.nan
        elif alteration == "outside_zero":
            frames[0, 1, 0] = 0
        else:
            frames[0, 0, 0] = -1
        np.save(bundle / "frames.npy", frames, allow_pickle=False)
    elif alteration == "mask_narrowing":
        mask = np.load(bundle / "mask.npy")
        mask[0, 0] = False
        np.save(bundle / "mask.npy", mask, allow_pickle=False)
    elif alteration == "domain_narrowing":
        source["domain"]["required_cells"] = [1, 3]
    elif alteration == "wrong_crs":
        source["domain"]["grid"]["crs"] = "EPSG:4326"
    elif alteration == "wrong_row_order":
        source["domain"]["grid"]["row_order"] = "north-to-south"
    elif alteration == "units":
        source["units"] = "IN"
    elif alteration == "step":
        source["interval_seconds"] = True
    elif alteration == "gap":
        source["times"][1] = "2026-11-02T00:01:00+00:00"
    elif alteration == "naive_clock":
        source["times"][0] = "2026-11-01T23:00:00"
    elif alteration == "offset_clock":
        source["times"][0] = "2026-11-01T18:00:00-05:00"
    elif alteration == "short_window":
        request["start"] = source["times"][1]
    elif alteration == "incomplete_prefix":
        source["segments"] = source["segments"][1:]
    else:
        target = {
            "source_bytes": "forecast.json",
            "frame_bytes": "frames.npy",
            "policy_bytes": "policy.json",
        }[alteration]
        with (bundle / target).open("ab") as stream:
            stream.write(b"changed")
        repin = False
    _save(bundle / "source.json", source)
    _save(bundle / "request.json", request)
    if repin:
        # Deliberately rehash request and mask while retaining the selected domain.
        _repin(bundle)
    with pytest.raises(ValueError):
        HmsForecastGrid.materialize(bundle / "request.json", bundle / "result.json")
    assert native_io.calls == 0
    assert not (bundle / "rainfall.dss").exists()


@pytest.mark.parametrize(
    "field",
    [
        "units",
        "clock",
        "grid",
        "record_count",
        "domain_sha256",
        "process_quiescent",
        "coverage",
        "tolerance",
        "output",
    ],
)
def test_verify_refuses_altered_result_semantics(bundle, owned_child, field):
    request, result = bundle / "request.json", bundle / "result.json"
    value = HmsForecastGrid.materialize(request, result)
    if field == "coverage":
        value["records"][0]["required_cell_count"] = 2
    elif field == "tolerance":
        value["records"][0]["max_absolute_error_mm"] = 1
    elif field == "output":
        (bundle / "rainfall.dss").write_bytes(b"different")
    elif field == "process_quiescent":
        value[field] = False
    else:
        value[field] = "changed"
    _save(result, value)
    with pytest.raises(ValueError):
        HmsForecastGrid.verify(request, result)
    with pytest.raises(ValueError):
        HmsForecastGrid.materialize(request, result)
    assert owned_child.calls == 1


@pytest.mark.parametrize("failure", ["timed_out", "quiescent"])
def test_parent_refuses_unreconciled_child(bundle, owned_child, failure):
    setattr(owned_child, failure, failure == "timed_out")
    with pytest.raises((RuntimeError, TimeoutError)):
        HmsForecastGrid.materialize(bundle / "request.json", bundle / "result.json")
    assert not (bundle / "rainfall.dss").exists()
    assert not (bundle / "result.json").exists()


def test_source_mutation_during_worker_is_refused(bundle, owned_child):
    owned_child.after = lambda: (bundle / "forecast.json").write_bytes(b"changed")
    with pytest.raises(ValueError, match="identity"):
        HmsForecastGrid.materialize(bundle / "request.json", bundle / "result.json")
    assert not (bundle / "rainfall.dss").exists()


def test_concurrent_identical_request_is_refused_without_second_writer(
    bundle, owned_child
):
    def concurrent():
        with pytest.raises(FileExistsError):
            HmsForecastGrid.materialize(bundle / "request.json", bundle / "result.json")

    owned_child.after = concurrent
    HmsForecastGrid.materialize(bundle / "request.json", bundle / "result.json")
    assert owned_child.calls == 1


def test_competing_output_publication_cannot_replace_existing_bytes(
    bundle, owned_child
):
    owned_child.after = lambda: (bundle / "rainfall.dss").write_bytes(
        b"competing-owner"
    )
    with pytest.raises(FileExistsError):
        HmsForecastGrid.materialize(bundle / "request.json", bundle / "result.json")
    assert (bundle / "rainfall.dss").read_bytes() == b"competing-owner"
    assert not (bundle / "result.json").exists()


def test_changed_request_cannot_reuse_accepted_output(bundle, owned_child):
    HmsForecastGrid.materialize(bundle / "request.json", bundle / "result.json")
    request = json.loads((bundle / "request.json").read_bytes())
    request["absolute_value_tolerance_mm"] = 1
    _save(bundle / "request.json", request)
    with pytest.raises(ValueError, match="request_sha256"):
        HmsForecastGrid.materialize(bundle / "request.json", bundle / "result.json")
    assert owned_child.calls == 1


def test_old_companion_policy_is_refused_before_native_write(
    bundle, native_io, monkeypatch
):
    from ras_commander.dss._hec_monolith import HecMonolithDownloader

    monkeypatch.setattr(HecMonolithDownloader, "TRUST_POLICY", "old", raising=False)
    assert HmsGridWriterWorker.run(bundle / "request.json", bundle / "worker.json") == 2
    assert native_io.calls == 0
    assert "trust policy" in (bundle / "worker.json").read_text()


@pytest.mark.parametrize("clock", ["2026-11-01T23:00Z", "2026-11-01 23:00:00+00:00"])
def test_runtime_rejects_clock_spellings_outside_schema(bundle, native_io, clock):
    source = json.loads((bundle / "source.json").read_bytes())
    source["times"][0] = clock
    _save(bundle / "source.json", source)
    _repin(bundle)
    with pytest.raises(ValueError, match="explicit UTC"):
        HmsForecastGrid.materialize(bundle / "request.json", bundle / "result.json")
    assert native_io.calls == 0


@pytest.mark.parametrize("crs", [5070, {"init": "epsg:5070"}, "EPSG:4326"])
def test_selected_domain_still_requires_a_projected_crs_string(bundle, native_io, crs):
    source = json.loads((bundle / "source.json").read_bytes())
    source["domain"]["grid"]["crs"] = crs
    _save(bundle / "source.json", source)
    _repin(bundle, domain=True)
    with pytest.raises(ValueError, match="Grid CRS"):
        HmsForecastGrid.materialize(bundle / "request.json", bundle / "result.json")
    assert native_io.calls == 0


@pytest.mark.parametrize("field", ["record_count", "tolerance", "grid"])
def test_verify_refuses_boolean_numeric_substitution(bundle, owned_child, field):
    request, result = bundle / "request.json", bundle / "result.json"
    value = HmsForecastGrid.materialize(request, result)
    if field == "record_count":
        value["record_count"] = True
    elif field == "tolerance":
        value["absolute_value_tolerance_mm"] = False
    else:
        value["grid"]["origin"][0] = False
    _save(result, value)
    with pytest.raises(ValueError):
        HmsForecastGrid.verify(request, result)


def test_native_float32_precision_is_checked_against_declared_tolerance(
    bundle, owned_child
):
    frames = np.load(bundle / "frames.npy").astype(np.float64)
    frames[0, 0, 0] = 0.1
    np.save(bundle / "frames.npy", frames, allow_pickle=False)
    request = json.loads((bundle / "request.json").read_bytes())
    request["absolute_value_tolerance_mm"] = 0
    _save(bundle / "request.json", request)
    _repin(bundle)
    with pytest.raises(RuntimeError, match="worker failed"):
        HmsForecastGrid.materialize(bundle / "request.json", bundle / "result.json")
    assert "tolerance" in (bundle / "result.json.worker.json").read_text()


def test_native_sidecar_retention_does_not_reject_accepted_product(
    bundle, owned_child, native_io, caplog
):
    native_io.alteration = "sidecar"
    accepted = HmsForecastGrid.materialize(
        bundle / "request.json", bundle / "result.json"
    )
    assert accepted["status"] == "succeeded"
    assert "staging evidence remains" in caplog.text
    assert len(list(bundle.glob(".forecast-grid-*/grid.log"))) == 1


def test_reservation_cleanup_failure_is_reported_after_acceptance(
    bundle, owned_child, monkeypatch, caplog
):
    original = Path.unlink

    def unlink(path, *args, **kwargs):
        if path.name == "result.json.lock":
            raise PermissionError("simulated retained reservation")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    accepted = HmsForecastGrid.materialize(
        bundle / "request.json", bundle / "result.json"
    )
    assert (
        HmsForecastGrid.verify(bundle / "request.json", bundle / "result.json")
        == accepted
    )
    assert "reservation remains" in caplog.text


def test_unknown_process_ownership_preserves_reservation_before_receipt(
    bundle, monkeypatch
):
    calls = []

    def unresolved(*args, **kwargs):
        calls.append(args)
        raise RuntimeError("cleanup did not establish ownership; retry is unsafe")

    monkeypatch.setattr(api_module, "run_owned", unresolved)
    with pytest.raises(RuntimeError, match="retry is unsafe"):
        HmsForecastGrid.materialize(bundle / "request.json", bundle / "result.json")
    assert (bundle / "result.json.lock").is_file()
    assert not (bundle / "result.json.worker.json").exists()
    with pytest.raises(FileExistsError):
        HmsForecastGrid.materialize(bundle / "request.json", bundle / "result.json")
    assert len(calls) == 1


@pytest.mark.requires_java
def test_native_forecast_grid_qualification(bundle):
    """Opt-in native writing/readback and post-exit file movement; no HEC solver.

    Run only in an installed, explicitly trusted native environment. This test
    has no missing-runtime skip once opted in: a requested qualification must fail
    if its runtime is unavailable. Portable CI excludes the requires_java marker.
    """
    if os.environ.get("HMS_GRID_NATIVE_QUALIFICATION") != "1":
        pytest.skip(
            "Set HMS_GRID_NATIVE_QUALIFICATION=1 for explicit native qualification"
        )
    accepted = HmsForecastGrid.materialize(
        bundle / "request.json", bundle / "result.json"
    )
    assert accepted["records"][0]["outside_nodata_cells"] == 1
    assert accepted["process_quiescent"] is True
    output = bundle / "rainfall.dss"
    moved = output.with_suffix(".released")
    output.rename(moved)
    moved.rename(output)
    assert (
        HmsForecastGrid.verify(bundle / "request.json", bundle / "result.json")
        == accepted
    )
    assert (
        HmsForecastGrid.materialize(bundle / "request.json", bundle / "result.json")
        == accepted
    )
