"""Isolated process boundary for writing and optionally verifying a DSS grid."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from itertools import pairwise
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


def _load_array_identity(value: Any, *, label: str) -> np.ndarray:
    if not isinstance(value, dict) or set(value) != {"path", "sha256"}:
        raise ValueError(f"{label} identity is invalid")
    path = Path(str(value["path"])).resolve()
    if not path.is_file() or _sha256(path) != value["sha256"]:
        raise ValueError(f"{label} identity does not match")
    return np.load(path, allow_pickle=False)


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
        optional = {"verification"}
        if (
            not isinstance(request, dict)
            or not required.issubset(request)
            or set(request) - required - optional
        ):
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
        if any(second <= first for first, second in pairwise(times)):
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
        verification_result = None
        if "verification" in request:
            verification = request["verification"]
            expected_fields = {
                "effective_areas",
                "absolute_value_tolerance",
                "expected_units",
                "expected_data_type",
                "expected_grid",
                "support_ids",
            }
            if (
                not isinstance(verification, dict)
                or set(verification) != expected_fields
            ):
                raise ValueError("Grid-writer verification fields are invalid")
            effective_areas = _load_array_identity(
                verification["effective_areas"],
                label="Grid-writer effective-area data",
            )
            if effective_areas.ndim == 2:
                effective_areas = effective_areas[np.newaxis, :, :]
            if (
                effective_areas.ndim != 3
                or effective_areas.shape[1:] != frames.shape[1:]
            ):
                raise ValueError("Grid-writer effective areas do not match frame shape")
            if not np.isfinite(effective_areas).all() or (effective_areas < 0).any():
                raise ValueError(
                    "Grid-writer effective areas must be finite and nonnegative"
                )
            tolerance = float(verification["absolute_value_tolerance"])
            if not np.isfinite(tolerance) or tolerance < 0:
                raise ValueError("Grid-writer verification tolerance is invalid")
            support_ids = verification["support_ids"]
            if (
                not isinstance(support_ids, list)
                or len(support_ids) != len(effective_areas)
                or not all(isinstance(value, str) and value for value in support_ids)
                or len(set(support_ids)) != len(support_ids)
            ):
                raise ValueError("Grid-writer verification support IDs are invalid")
            expected_grid = verification["expected_grid"]
            if not isinstance(expected_grid, dict) or set(expected_grid) != {
                "shape",
                "cell_size",
                "origin",
                "crs",
            }:
                raise ValueError("Grid-writer expected grid is invalid")

            from pyproj import CRS

            catalog = RasDss.get_catalog(output)
            catalog_paths = set(catalog["pathname"].astype(str))
            if any(pathname not in catalog_paths for pathname in written):
                raise RuntimeError("Grid-writer readback catalog is incomplete")
            weighted_depth_area = {support_id: [] for support_id in support_ids}
            maximum_difference = 0.0
            for index, pathname in enumerate(written):
                grid = RasDss.read_grid(output, pathname)
                values = np.asarray(grid["data"], dtype=np.float64)
                if values.shape != frames.shape[1:]:
                    raise RuntimeError("Grid-writer readback shape changed")
                if not np.isfinite(values).all() or (values < 0).any():
                    raise RuntimeError("Grid-writer readback values are invalid")
                difference = float(
                    np.max(np.abs(values - np.asarray(frames[index], dtype=np.float64)))
                )
                maximum_difference = max(maximum_difference, difference)
                if difference > tolerance:
                    raise RuntimeError(
                        "Grid-writer readback exceeded absolute value tolerance"
                    )
                if str(grid["units"]).upper() != str(
                    verification["expected_units"]
                ).upper() or str(grid["data_type"]).upper().replace("_", "-") != str(
                    verification["expected_data_type"]
                ).upper().replace(
                    "_", "-"
                ):
                    raise RuntimeError("Grid-writer readback metadata changed")
                if (
                    list(grid["shape"]) != list(expected_grid["shape"])
                    or not np.isclose(
                        float(grid["cell_size"]),
                        float(expected_grid["cell_size"]),
                        rtol=0.0,
                        atol=1.0e-9,
                    )
                    or not np.allclose(
                        np.asarray(grid["metadata"].get("origin"), dtype=float),
                        np.asarray(expected_grid["origin"], dtype=float),
                        rtol=0.0,
                        atol=1.0e-9,
                    )
                    or not CRS.from_user_input(str(grid["crs"])).equals(
                        CRS.from_user_input(str(expected_grid["crs"]))
                    )
                ):
                    raise RuntimeError("Grid-writer readback grid definition changed")
                if (
                    grid["start_time"].to_pydatetime() != times[index]
                    or grid["end_time"].to_pydatetime() != times[index + 1]
                ):
                    raise RuntimeError("Grid-writer readback interval changed")
                for support_id, support_area in zip(
                    support_ids,
                    effective_areas,
                    strict=True,
                ):
                    weighted_depth_area[support_id].append(
                        float(np.sum(values * support_area, dtype=np.float64))
                    )
            verification_result = {
                "status": "verified",
                "absolute_value_tolerance": tolerance,
                "maximum_absolute_value_difference": maximum_difference,
                "weighted_depth_area_by_support": weighted_depth_area,
            }
        result: dict[str, Any] = {
            "schema": RESULT_SCHEMA,
            "status": "succeeded",
            "record_count": len(written),
            "first_pathname": written[0],
            "last_pathname": written[-1],
        }
        if verification_result is not None:
            result["verification"] = verification_result
        _write_json_new(result_file, result)
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
