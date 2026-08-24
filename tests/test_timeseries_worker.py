"""Tests for deterministic DSS time-series window translation."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from hms_commander import HmsTimeSeriesWorker
from hms_commander.dss import DssCore

OUACHITA_PATH = "/MVK/OUACHITA/FLOW//1HOUR/CWMS-MODEL/"
BARTHOLOMEW_PATH = "/BW/BAYOU-BARTHOLOMEW/FLOW//15MIN/HEC-RAS-MODEL/"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _frame(index: pd.DatetimeIndex, values: np.ndarray, interval: int) -> pd.DataFrame:
    frame = pd.DataFrame({"value": values}, index=index)
    frame.insert(0, "datetime", frame.index)
    frame.attrs.update({"units": "CFS", "type": "INST-VAL", "interval": interval})
    return frame


def _request(
    tmp_path: Path,
    duration_hours: int = 96,
) -> tuple[Path, Path, Path, dict[tuple[str, str], pd.DataFrame]]:
    source_start = pd.Timestamp("2023-04-01T13:00:00")
    source_end = source_start + pd.Timedelta(hours=duration_hours)
    target_start = pd.Timestamp("2019-09-18T13:00:00")
    target_end = target_start + pd.Timedelta(hours=duration_hours)
    ouachita = tmp_path / "ouachita.dss"
    bartholomew = tmp_path / "bartholomew.dss"
    ouachita.write_bytes(b"immutable ouachita fixture")
    bartholomew.write_bytes(b"immutable bartholomew fixture")

    source_frames = {
        (str(ouachita.resolve()), OUACHITA_PATH): _frame(
            pd.date_range(source_start, source_end, freq="60min"),
            np.linspace(52_000.0, 53_000.0, duration_hours + 1),
            60,
        ),
        (str(bartholomew.resolve()), BARTHOLOMEW_PATH): _frame(
            pd.date_range(source_start, source_end, freq="15min"),
            np.linspace(2_900.0, 3_100.0, duration_hours * 4 + 1),
            15,
        ),
    }
    output = tmp_path / "translated.dss"
    request = {
        "schema": "hms-commander/timeseries-window-request/1.0",
        "operation_id": f"deloutre-static-gage-window-{duration_hours}h",
        "qualification": {
            "status": "qualification_only",
            "forecast_eligible": False,
            "basis_issue": "https://github.com/harrisbienn/floodforecast/issues/62",
        },
        "source_window": {
            "start": source_start.isoformat(),
            "time_zone": "America/Chicago",
        },
        "target_window": {
            "start": target_start.isoformat(),
            "end": target_end.isoformat(),
            "time_zone": "America/Chicago",
        },
        "output_dss": str(output),
        "sentinel_threshold": -1.0e20,
        "series": [
            {
                "series_id": "MVK_Ouachita",
                "source_dss": str(ouachita),
                "source_sha256": _sha256(ouachita),
                "source_pathname": OUACHITA_PATH,
                "output_pathname": OUACHITA_PATH,
                "interval_minutes": 60,
                "units": "CFS",
                "data_type": "INST-VAL",
            },
            {
                "series_id": "BW_BayouBartholomew",
                "source_dss": str(bartholomew),
                "source_sha256": _sha256(bartholomew),
                "source_pathname": BARTHOLOMEW_PATH,
                "output_pathname": BARTHOLOMEW_PATH,
                "interval_minutes": 15,
                "units": "CFS",
                "data_type": "INST-VAL",
            },
        ],
    }
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    return request_path, tmp_path / "result.json", output, source_frames


def _install_fake_dss(
    monkeypatch,
    source_frames: dict[tuple[str, str], pd.DataFrame],
) -> list[dict[str, object]]:
    written: dict[tuple[str, str], pd.DataFrame] = {}
    calls: list[dict[str, object]] = []

    def fake_read(dss_file, pathname, *args, **kwargs):
        key = (str(Path(dss_file).resolve()), pathname)
        frame = source_frames[key] if key in source_frames else written[key]
        return frame.copy()

    def fake_write(
        dss_file,
        pathname,
        times,
        values,
        *,
        units,
        data_type,
        interval_minutes,
        create_if_missing=True,
    ):
        output = Path(dss_file).resolve()
        values_array = np.asarray(values, dtype=np.float64)
        frame = _frame(pd.DatetimeIndex(times), values_array, interval_minutes)
        frame.attrs.update({"units": units, "type": data_type})
        written[(str(output), pathname)] = frame
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("ab") as stream:
            stream.write(pathname.encode("utf-8") + values_array.tobytes())
        calls.append(
            {
                "pathname": pathname,
                "times": pd.DatetimeIndex(times),
                "values": values_array.copy(),
            }
        )

    monkeypatch.setattr(DssCore, "read_timeseries", fake_read)
    monkeypatch.setattr(DssCore, "write_timeseries", fake_write)

    def fake_subprocess(request, output, *, request_sha256):
        return HmsTimeSeriesWorker._run_dss_in_process(request, output)

    monkeypatch.setattr(
        HmsTimeSeriesWorker,
        "_run_dss_subprocess",
        fake_subprocess,
    )
    return calls


@pytest.mark.parametrize(
    ("duration_hours", "source_end", "record_counts"),
    [
        (96, "2023-04-05T13:00:00", [97, 385]),
        (120, "2023-04-06T13:00:00", [121, 481]),
        (144, "2023-04-07T13:00:00", [145, 577]),
    ],
)
def test_worker_translates_complete_paired_series_and_verifies_reuse(
    tmp_path: Path,
    monkeypatch,
    duration_hours: int,
    source_end: str,
    record_counts: list[int],
) -> None:
    request_path, result_path, output, source_frames = _request(
        tmp_path, duration_hours
    )
    source_hashes = {
        path: _sha256(path)
        for path in (tmp_path / "ouachita.dss", tmp_path / "bartholomew.dss")
    }
    calls = _install_fake_dss(monkeypatch, source_frames)

    assert HmsTimeSeriesWorker.run(request_path, result_path) == 0
    assert HmsTimeSeriesWorker.run(request_path, result_path) == 0

    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["status"] == "succeeded"
    assert result["qualification"] == {
        "status": "qualification_only",
        "forecast_eligible": False,
        "basis_issue": "https://github.com/harrisbienn/floodforecast/issues/62",
    }
    assert result["source_window"]["end"] == source_end
    assert result["translation_offset_seconds"] == -111542400
    assert [record["record_count"] for record in result["series"]] == record_counts
    assert all(record["final_is_maximum"] for record in result["series"])
    assert len(calls) == 2
    assert calls[0]["times"][0] == pd.Timestamp("2019-09-18T13:00:00")
    assert calls[1]["times"][-1] == pd.Timestamp("2019-09-18T13:00:00") + pd.Timedelta(
        hours=duration_hours
    )
    np.testing.assert_array_equal(
        calls[0]["values"],
        source_frames[(str((tmp_path / "ouachita.dss").resolve()), OUACHITA_PATH)][
            "value"
        ],
    )
    np.testing.assert_array_equal(
        calls[1]["values"],
        source_frames[
            (str((tmp_path / "bartholomew.dss").resolve()), BARTHOLOMEW_PATH)
        ]["value"],
    )
    assert output.is_file()
    assert all(_sha256(path) == digest for path, digest in source_hashes.items())


def test_worker_rejects_incomplete_source_window(
    tmp_path: Path,
    monkeypatch,
) -> None:
    request_path, result_path, output, source_frames = _request(tmp_path)
    key = (str((tmp_path / "ouachita.dss").resolve()), OUACHITA_PATH)
    source_frames[key] = source_frames[key].drop(source_frames[key].index[10])
    _install_fake_dss(monkeypatch, source_frames)

    assert HmsTimeSeriesWorker.run(request_path, result_path) == 4

    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["classification"] == "translation_failed"
    assert "does not exactly cover" in result["error"]["message"]
    assert not output.exists()


def test_worker_rejects_sentinel_values(
    tmp_path: Path,
    monkeypatch,
) -> None:
    request_path, result_path, output, source_frames = _request(tmp_path)
    key = (str((tmp_path / "bartholomew.dss").resolve()), BARTHOLOMEW_PATH)
    source_frames[key].iloc[12, source_frames[key].columns.get_loc("value")] = -1.0e30
    _install_fake_dss(monkeypatch, source_frames)

    assert HmsTimeSeriesWorker.run(request_path, result_path) == 4

    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert "sentinel threshold" in result["error"]["message"]
    assert not output.exists()


@pytest.mark.parametrize(
    ("attribute", "actual", "message"),
    [
        ("interval", 30, "interval does not match"),
        ("units", "CMS", "units do not match"),
        ("type", "PER-AVER", "data type does not match"),
    ],
)
def test_worker_rejects_source_metadata_drift(
    tmp_path: Path,
    monkeypatch,
    attribute: str,
    actual: object,
    message: str,
) -> None:
    request_path, result_path, output, source_frames = _request(tmp_path)
    key = (str((tmp_path / "ouachita.dss").resolve()), OUACHITA_PATH)
    source_frames[key].attrs[attribute] = actual
    _install_fake_dss(monkeypatch, source_frames)

    assert HmsTimeSeriesWorker.run(request_path, result_path) == 4

    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert message in result["error"]["message"]
    assert not output.exists()


def test_worker_rejects_source_mutation_during_dss_processing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    request_path, result_path, output, source_frames = _request(tmp_path)
    _install_fake_dss(monkeypatch, source_frames)
    source = tmp_path / "ouachita.dss"

    def mutate_source(request, temporary_output, *, request_sha256):
        records = HmsTimeSeriesWorker._run_dss_in_process(request, temporary_output)
        with source.open("ab") as stream:
            stream.write(b"changed during child processing")
        return records

    monkeypatch.setattr(
        HmsTimeSeriesWorker,
        "_run_dss_subprocess",
        mutate_source,
    )

    assert HmsTimeSeriesWorker.run(request_path, result_path) == 3

    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["classification"] == "source_identity_drift"
    assert not output.exists()


def test_worker_rejects_dst_transition_window(
    tmp_path: Path,
    monkeypatch,
) -> None:
    request_path, result_path, output, source_frames = _request(tmp_path)
    request = json.loads(request_path.read_text(encoding="utf-8"))
    request["target_window"].update(
        {"start": "2025-11-01T00:00:00", "end": "2025-11-03T00:00:00"}
    )
    request_path.write_text(json.dumps(request), encoding="utf-8")
    _install_fake_dss(monkeypatch, source_frames)

    assert HmsTimeSeriesWorker.run(request_path, result_path) == 4

    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert "ambiguous local time" in result["error"]["message"]
    assert not output.exists()


def test_worker_refuses_changed_request_against_existing_result(
    tmp_path: Path,
    monkeypatch,
) -> None:
    request_path, result_path, output, source_frames = _request(tmp_path)
    _install_fake_dss(monkeypatch, source_frames)
    assert HmsTimeSeriesWorker.run(request_path, result_path) == 0
    original_result = result_path.read_bytes()

    request = json.loads(request_path.read_text(encoding="utf-8"))
    request["operation_id"] = "changed-operation"
    request_path.write_text(json.dumps(request), encoding="utf-8")

    assert HmsTimeSeriesWorker.run(request_path, result_path) == 3
    assert result_path.read_bytes() == original_result
    assert output.is_file()


def test_worker_detects_output_tampering_on_reuse(
    tmp_path: Path,
    monkeypatch,
) -> None:
    request_path, result_path, output, source_frames = _request(tmp_path)
    _install_fake_dss(monkeypatch, source_frames)
    assert HmsTimeSeriesWorker.run(request_path, result_path) == 0

    with output.open("ab") as stream:
        stream.write(b"tampered")

    assert HmsTimeSeriesWorker.run(request_path, result_path) == 3
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["status"] == "succeeded"
    assert result["output_dss"]["sha256"] != _sha256(output)


def test_worker_refuses_existing_output_without_result(
    tmp_path: Path,
    monkeypatch,
) -> None:
    request_path, result_path, output, source_frames = _request(tmp_path)
    _install_fake_dss(monkeypatch, source_frames)
    output.write_bytes(b"unowned output")

    assert HmsTimeSeriesWorker.run(request_path, result_path) == 3

    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["classification"] == "existing_output"
    assert output.read_bytes() == b"unowned output"


def test_packaged_timeseries_schemas_match_public_contract_constants() -> None:
    contracts = Path(__file__).resolve().parents[1] / "hms_commander" / "contracts"
    request_schema = json.loads(
        (contracts / "timeseries-window-request-v1.0.schema.json").read_text(
            encoding="utf-8"
        )
    )
    result_schema = json.loads(
        (contracts / "timeseries-window-result-v1.0.schema.json").read_text(
            encoding="utf-8"
        )
    )

    assert request_schema["$id"] == HmsTimeSeriesWorker.REQUEST_SCHEMA
    assert result_schema["$id"] == HmsTimeSeriesWorker.RESULT_SCHEMA


@pytest.mark.parametrize(
    ("times", "values", "message"),
    [
        (
            pd.date_range("2023-04-01T13:00:00", periods=2, freq="15min"),
            [[1.0], [2.0]],
            "one-dimensional",
        ),
        (
            pd.DatetimeIndex([pd.Timestamp("2023-04-01T13:00:00"), pd.NaT]),
            [1.0, 2.0],
            "must not contain NaT",
        ),
    ],
)
def test_dss_core_rejects_invalid_arrays_before_starting_jvm(
    tmp_path: Path,
    monkeypatch,
    times,
    values,
    message: str,
) -> None:
    def unexpected_jvm_start(*args, **kwargs):
        raise AssertionError("JVM must not start for invalid arrays")

    monkeypatch.setattr(DssCore, "_configure_jvm", unexpected_jvm_start)

    with pytest.raises(ValueError, match=message):
        DssCore.write_timeseries(
            tmp_path / "invalid.dss",
            BARTHOLOMEW_PATH,
            times,
            values,
            units="CFS",
            data_type="INST-VAL",
            interval_minutes=15,
        )


@pytest.mark.requires_java
@pytest.mark.skipif(
    os.environ.get("HMS_COMMANDER_RUN_DSS_INTEGRATION") != "1",
    reason="set HMS_COMMANDER_RUN_DSS_INTEGRATION=1 for DSS round-trip coverage",
)
def test_dss_core_writes_and_reads_regular_timeseries(tmp_path: Path) -> None:
    output = tmp_path / "roundtrip.dss"
    index = pd.date_range("2023-04-01T13:00:00", periods=9, freq="15min")
    values = np.linspace(2_900.0, 3_100.0, len(index))

    DssCore.write_timeseries(
        output,
        BARTHOLOMEW_PATH,
        index,
        values,
        units="CFS",
        data_type="INST-VAL",
        interval_minutes=15,
    )
    actual = DssCore.read_timeseries(output, BARTHOLOMEW_PATH)

    assert actual.index.equals(index)
    np.testing.assert_array_equal(actual["value"].to_numpy(), values)
    assert actual.attrs["units"] == "CFS"
    assert actual.attrs["type"] == "INST-VAL"
    assert actual.attrs["interval"] == 15
