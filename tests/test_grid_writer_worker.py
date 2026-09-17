"""Tests for the isolated DSS grid-writer process boundary."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from hms_commander import HmsGridWriterWorker


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _request(tmp_path: Path) -> tuple[Path, Path, Path]:
    frames_path = tmp_path / "frames.npy"
    frames = np.arange(8, dtype=np.float32).reshape((2, 2, 2))
    np.save(frames_path, frames, allow_pickle=False)
    start = datetime(2019, 9, 18, 13)
    request = {
        "schema": "hms-commander/grid-writer-request/1.0",
        "data": {"path": str(frames_path), "sha256": _sha256(frames_path)},
        "output_dss": str(tmp_path / "output.dss"),
        "pathname": "/SHG/FIXTURE/PRECIPITATION///EXCESS/",
        "times": [
            (start + timedelta(minutes=5 * index)).isoformat(timespec="seconds")
            for index in range(3)
        ],
        "grid_info": {
            "cell_size": 500.0,
            "origin": [0.0, 0.0],
            "crs": "EPSG:5070",
            "units": "IN",
            "data_type": "PER-CUM",
            "interval_minutes": 5,
        },
    }
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    return request_path, tmp_path / "result.json", tmp_path / "output.dss"


def test_grid_writer_authenticates_frames_and_publishes_result(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from ras_commander import RasDss

    request_path, result_path, output_path = _request(tmp_path)

    def fake_write(path, pathname, frames, times, grid_info):
        assert frames.shape == (2, 2, 2)
        assert len(times) == 3
        assert grid_info["units"] == "IN"
        Path(path).write_bytes(b"fixture DSS")
        return [f"{pathname}0", f"{pathname}1"]

    monkeypatch.setattr(RasDss, "write_grid_timeseries", fake_write)

    assert HmsGridWriterWorker.run(request_path, result_path) == 0
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["status"] == "succeeded"
    assert result["record_count"] == 2
    assert output_path.read_bytes() == b"fixture DSS"


def test_grid_writer_rejects_tampered_frame_identity(tmp_path: Path) -> None:
    request_path, result_path, output_path = _request(tmp_path)
    request = json.loads(request_path.read_text(encoding="utf-8"))
    request["data"]["sha256"] = "0" * 64
    request_path.write_text(json.dumps(request), encoding="utf-8")

    assert HmsGridWriterWorker.run(request_path, result_path) == 2
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["status"] == "failed"
    assert "identity does not match" in result["error"]["message"]
    assert not output_path.exists()


def test_grid_writer_reopens_and_verifies_published_frames(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from ras_commander import RasDss

    request_path, result_path, output_path = _request(tmp_path)
    request = json.loads(request_path.read_text(encoding="utf-8"))
    effective_areas_path = tmp_path / "effective-areas.npy"
    effective_areas = np.asarray([[1.0, 0.5], [0.0, 2.0]], dtype=np.float64)
    np.save(effective_areas_path, effective_areas, allow_pickle=False)
    request["verification"] = {
        "effective_areas": {
            "path": str(effective_areas_path),
            "sha256": _sha256(effective_areas_path),
        },
        "absolute_value_tolerance": 0.01,
        "expected_units": "IN",
        "expected_data_type": "PER-CUM",
        "support_ids": ["all"],
        "expected_grid": {
            "shape": [2, 2],
            "cell_size": 500.0,
            "origin": [0.0, 0.0],
            "crs": "EPSG:5070",
        },
    }
    request_path.write_text(json.dumps(request), encoding="utf-8")
    written_frames: list[np.ndarray] = []
    written_paths = [
        "/SHG/FIXTURE/PRECIPITATION/18SEP2019:1300/18SEP2019:1305/EXCESS/",
        "/SHG/FIXTURE/PRECIPITATION/18SEP2019:1305/18SEP2019:1310/EXCESS/",
    ]

    def fake_write(path, pathname, frames, times, grid_info):
        written_frames.extend(np.asarray(frame) for frame in frames)
        Path(path).write_bytes(b"fixture DSS")
        return written_paths

    monkeypatch.setattr(RasDss, "write_grid_timeseries", fake_write)
    monkeypatch.setattr(
        RasDss,
        "get_catalog",
        staticmethod(lambda _: pd.DataFrame({"pathname": written_paths})),
    )

    def fake_read(_path, pathname):
        index = written_paths.index(pathname)
        start = datetime(2019, 9, 18, 13) + timedelta(minutes=5 * index)
        return {
            "data": written_frames[index],
            "shape": (2, 2),
            "units": "IN",
            "data_type": "PER-CUM",
            "cell_size": 500.0,
            "crs": "EPSG:5070",
            "metadata": {"origin": (0.0, 0.0)},
            "start_time": pd.Timestamp(start),
            "end_time": pd.Timestamp(start + timedelta(minutes=5)),
        }

    monkeypatch.setattr(RasDss, "read_grid", staticmethod(fake_read))

    assert HmsGridWriterWorker.run(request_path, result_path) == 0
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["verification"] == {
        "status": "verified",
        "absolute_value_tolerance": 0.01,
        "maximum_absolute_value_difference": 0.0,
        "weighted_depth_area_by_support": {"all": [6.5, 20.5]},
    }
    assert output_path.is_file()
