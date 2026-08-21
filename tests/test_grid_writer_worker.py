"""Tests for the isolated DSS grid-writer process boundary."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

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
