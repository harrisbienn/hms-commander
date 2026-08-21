"""Isolated process boundary for writing one HEC-DSS grid time series."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

SCHEMA = "hms-commander/grid-writer-request/1.0"
RESULT_SCHEMA = "hms-commander/grid-writer-result/1.0"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json_new(path: Path, payload: dict[str, Any]) -> None:
    if path.exists():
        raise FileExistsError(f"Refusing to overwrite grid-writer result: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def run(request_path: str | Path, result_path: str | Path) -> int:
    """Validate one request, write its grid series, and exit to release DSS."""
    request_file = Path(request_path).resolve()
    result_file = Path(result_path).resolve()
    try:
        request = json.loads(request_file.read_text(encoding="utf-8"))
        required = {"schema", "data", "output_dss", "pathname", "times", "grid_info"}
        if not isinstance(request, dict) or set(request) != required:
            raise ValueError("Grid-writer request fields are invalid")
        if request.get("schema") != SCHEMA:
            raise ValueError("Grid-writer request schema is unsupported")
        data_identity = request["data"]
        if not isinstance(data_identity, dict) or set(data_identity) != {
            "path",
            "sha256",
        }:
            raise ValueError("Grid-writer data identity is invalid")
        data_path = Path(str(data_identity["path"])).resolve()
        if not data_path.is_file() or _sha256(data_path) != data_identity["sha256"]:
            raise ValueError("Grid-writer data identity does not match")
        frames = np.load(data_path, allow_pickle=False)
        if frames.ndim != 3 or not np.isfinite(frames).all() or (frames < 0).any():
            raise ValueError("Grid-writer frames must be finite nonnegative 3-D data")
        raw_times = request["times"]
        if not isinstance(raw_times, list) or len(raw_times) != len(frames) + 1:
            raise ValueError("Grid-writer interval boundaries do not match frames")
        times = [datetime.fromisoformat(str(value)) for value in raw_times]
        if any(second <= first for first, second in zip(times, times[1:])):
            raise ValueError("Grid-writer interval boundaries are not increasing")
        output = Path(str(request["output_dss"])).resolve()
        if output.exists():
            raise FileExistsError(f"Refusing to overwrite grid DSS: {output}")
        output.parent.mkdir(parents=True, exist_ok=True)
        from ras_commander import RasDss

        written = RasDss.write_grid_timeseries(
            output,
            str(request["pathname"]),
            frames,
            times,
            dict(request["grid_info"]),
        )
        if len(written) != len(frames) or not output.is_file():
            raise RuntimeError("Grid writer did not produce every requested frame")
        _write_json_new(
            result_file,
            {
                "schema": RESULT_SCHEMA,
                "status": "succeeded",
                "record_count": len(written),
                "first_pathname": written[0],
                "last_pathname": written[-1],
            },
        )
        return 0
    except Exception as exc:
        try:
            _write_json_new(
                result_file,
                {
                    "schema": RESULT_SCHEMA,
                    "status": "failed",
                    "error": {
                        "type": type(exc).__name__,
                        "message": str(exc),
                    },
                },
            )
        except Exception:
            pass
        return 2


def main(argv: list[str] | None = None) -> int:
    """Run the grid writer from the command line."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True)
    parser.add_argument("--result", required=True)
    arguments = parser.parse_args(argv)
    return run(arguments.request, arguments.result)


if __name__ == "__main__":  # pragma: no cover - exercised by subprocess
    raise SystemExit(main())
