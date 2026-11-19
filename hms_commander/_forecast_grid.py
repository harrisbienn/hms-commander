"""Portable forecast-grid admission and native readback for request version 2."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np

from ._dss_runtime import require_ras_dss_policy

REQUEST_SCHEMA = "hms-commander/grid-writer-request/2.0"
SOURCE_SCHEMA = "hms-commander/forecast-grid-source/1.0"
RESULT_SCHEMA = "hms-commander/grid-writer-result/2.0"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def _fields(value: Any, names: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != names:
        raise ValueError(f"{label} fields are invalid; expected {sorted(names)}")
    return value


def _hash(value: Any) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError("Expected a lowercase SHA256 identity")
    return value


def _path(value: Any, base: Path) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Expected a nonempty path")
    return (base / value).resolve()


def _identity(value: Any, base: Path, label: str) -> Path:
    _fields(value, {"path", "sha256"}, label)
    path = _path(value["path"], base)
    if not path.is_file() or _sha256(path) != _hash(value["sha256"]):
        raise ValueError(f"{label} identity does not match: {path}")
    return path


def _bytes(path: Path, expected: str) -> bytes:
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != expected:
        raise ValueError(f"Input changed while reading: {path}")
    return content


def _utc(value: Any) -> datetime:
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:00(?:Z|\+00:00)", value
    ):
        raise ValueError("Interval boundaries must be explicit UTC strings")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() != timedelta(0) or parsed.second or parsed.microsecond:
        raise ValueError("Interval boundaries must use UTC and whole minutes")
    return parsed.astimezone(timezone.utc)


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be numeric")
    if not np.isfinite(value):
        raise ValueError(f"{label} must be finite")
    return float(value)


def _write_new(path: Path, value: dict[str, Any]) -> None:
    """Publish complete JSON with atomic create-if-absent, never replacement."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, indent=2, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class _Inputs:
    request: dict[str, Any]
    source: dict[str, Any]
    request_sha256: str
    output: Path
    frames: np.ndarray
    mask: np.ndarray
    times: list[datetime]
    tolerance: float
    protected_paths: frozenset[Path]


def _load(request_path: Path) -> _Inputs:
    """Authenticate preparation evidence without reimplementing source owners."""
    from pyproj import CRS

    request_bytes = request_path.read_bytes()
    request = json.loads(request_bytes)
    _fields(
        request,
        {
            "schema",
            "source",
            "expected_domain_sha256",
            "start",
            "end",
            "output_dss",
            "pathname",
            "absolute_value_tolerance_mm",
        },
        "Forecast grid request",
    )
    if request["schema"] != REQUEST_SCHEMA:
        raise ValueError("Unsupported forecast grid request schema")
    source_path = _identity(request["source"], request_path.parent, "Source descriptor")
    source = json.loads(_bytes(source_path, request["source"]["sha256"]))
    _fields(
        source,
        {
            "schema",
            "data",
            "required_mask",
            "domain",
            "times",
            "interval_seconds",
            "units",
            "data_type",
            "normalization_policy",
            "segments",
        },
        "Forecast grid source",
    )
    if source["schema"] != SOURCE_SCHEMA:
        raise ValueError("Unsupported forecast grid source schema")
    base = source_path.parent
    protected = {request_path, source_path}
    data_path = _identity(source["data"], base, "Frames")
    mask_path = _identity(source["required_mask"], base, "Required mask")
    policy_path = _identity(
        source["normalization_policy"], base, "Normalization policy"
    )
    protected.update((data_path, mask_path, policy_path))
    domain = _fields(
        source["domain"], {"grid", "required_cells", "owner_domain"}, "Domain"
    )
    if _digest(domain) != _hash(request["expected_domain_sha256"]):
        raise ValueError("Domain does not match the independently selected domain")
    protected.add(_identity(domain["owner_domain"], base, "Owner domain"))
    grid = _fields(
        domain["grid"], {"shape", "cell_size", "origin", "crs", "row_order"}, "Grid"
    )
    shape = grid["shape"]
    if (
        not isinstance(shape, list)
        or len(shape) != 2
        or any(type(size) is not int or size <= 0 for size in shape)
    ):
        raise ValueError("Grid shape must contain two positive integers")
    if _number(grid["cell_size"], "Cell size") <= 0:
        raise ValueError("Cell size must be positive")
    if not isinstance(grid["origin"], list) or len(grid["origin"]) != 2:
        raise ValueError("Grid origin must contain x and y coordinates")
    for coordinate in grid["origin"]:
        _number(coordinate, "Origin coordinate")
    if grid["row_order"] != "south-to-north":
        raise ValueError("Grid rows must run south-to-north")
    if not isinstance(grid["crs"], str) or not grid["crs"].strip():
        raise ValueError("Grid CRS must be a nonempty string")
    crs = CRS.from_user_input(grid["crs"])
    if (
        not crs.is_projected
        or len(crs.axis_info) < 2
        or any(axis.unit_conversion_factor != 1.0 for axis in crs.axis_info[:2])
    ):
        raise ValueError("Grid CRS must be projected with metre axes")
    cells = domain["required_cells"]
    if (
        not isinstance(cells, list)
        or not cells
        or any(
            type(cell) is not int or not 0 <= cell < shape[0] * shape[1]
            for cell in cells
        )
        or cells != sorted(set(cells))
    ):
        raise ValueError(
            "Required cells must be sorted unique in-domain integer indices"
        )
    mask = np.load(
        io.BytesIO(_bytes(mask_path, source["required_mask"]["sha256"])),
        allow_pickle=False,
    )
    if mask.dtype != np.dtype(bool) or mask.shape != tuple(shape):
        raise ValueError("Required mask must be a boolean array matching the grid")
    if np.flatnonzero(mask).tolist() != cells:
        raise ValueError("Required mask does not match the selected domain cells")
    frames = np.load(
        io.BytesIO(_bytes(data_path, source["data"]["sha256"])), allow_pickle=False
    )
    if frames.dtype.kind != "f" or frames.ndim != 3 or frames.shape[1:] != tuple(shape):
        raise ValueError(
            "Frames must be floating arrays with shape (time, rows, columns)"
        )
    if (
        not len(frames)
        or not np.isfinite(frames[:, mask]).all()
        or (frames[:, mask] < 0).any()
    ):
        raise ValueError("Every required cell must have finite nonnegative rainfall")
    if not np.isnan(frames[:, ~mask]).all():
        raise ValueError("Every outside-domain cell must be explicit NaN/NoData")
    if (frames[:, mask] > np.finfo(np.float32).max).any():
        raise ValueError("Rainfall exceeds the native float32 range")
    if source["units"] != "MM" or source["data_type"] != "PER-CUM":
        raise ValueError("Forecast grids require incremental MM / PER-CUM depths")
    step = source["interval_seconds"]
    if type(step) is not int or step <= 0 or step % 60:
        raise ValueError("Interval seconds must be a positive whole-minute integer")
    if not isinstance(source["times"], list) or len(source["times"]) != len(frames) + 1:
        raise ValueError("Interval boundaries must account for every frame")
    times = [_utc(value) for value in source["times"]]
    if any(
        end - start != timedelta(seconds=step) for start, end in zip(times, times[1:])
    ):
        raise ValueError("Interval boundaries must be contiguous and regular")
    if _utc(request["start"]) != times[0] or _utc(request["end"]) != times[-1]:
        raise ValueError(
            "Prepared arrays must cover the entire explicitly requested window"
        )
    segments = source["segments"]
    if not isinstance(segments, list) or not segments:
        raise ValueError("Source export segments must cover the complete window")
    next_frame = 0
    seen_forecast = False
    for segment in segments:
        _fields(
            segment, {"kind", "first_frame", "frame_count", "export"}, "Source segment"
        )
        if segment["kind"] not in {"observed", "forecast"}:
            raise ValueError("Source segment kind must be observed or forecast")
        if seen_forecast and segment["kind"] == "observed":
            raise ValueError("Observed source segments must precede forecast segments")
        seen_forecast = seen_forecast or segment["kind"] == "forecast"
        if (
            type(segment["first_frame"]) is not int
            or segment["first_frame"] != next_frame
            or type(segment["frame_count"]) is not int
            or segment["frame_count"] <= 0
        ):
            raise ValueError("Source segments must cover contiguous complete intervals")
        next_frame += segment["frame_count"]
        protected.add(_identity(segment["export"], base, "Source export"))
    if next_frame != len(frames) or not seen_forecast:
        raise ValueError("Source segments must cover all frames and include a forecast")
    pathname = request["pathname"]
    parts = pathname.split("/") if isinstance(pathname, str) else []
    if (
        len(parts) != 8
        or parts[0]
        or parts[-1]
        or parts[4]
        or parts[5]
        or any(not parts[index].strip() for index in (1, 2, 3, 6))
    ):
        raise ValueError("Pathname requires A/B/C/F and blank D/E parts")
    tolerance = _number(request["absolute_value_tolerance_mm"], "Readback tolerance")
    if tolerance < 0:
        raise ValueError("Readback tolerance must be nonnegative")
    output = _path(request["output_dss"], request_path.parent)
    if output in protected:
        raise ValueError("Output must not replace any authenticated input")
    return _Inputs(
        request,
        source,
        hashlib.sha256(request_bytes).hexdigest(),
        output,
        frames,
        mask,
        times,
        tolerance,
        frozenset(protected),
    )


def _grid_time(value: str) -> datetime:
    date, clock = value.split(":")
    if clock == "2400":
        return datetime.strptime(date, "%d%b%Y") + timedelta(days=1)
    return datetime.strptime(value, "%d%b%Y:%H%M")


def _native_write(inputs: _Inputs, output: Path) -> dict[str, Any]:
    """Use the admitted DSS owner; the caller isolates this in a child."""
    from pyproj import CRS

    require_ras_dss_policy()
    from ras_commander import RasDss

    grid = inputs.source["domain"]["grid"]
    native_times = [value.replace(tzinfo=None) for value in inputs.times]
    grid_info = {
        "cellsize": grid["cell_size"],
        "origin": grid["origin"],
        "crs": CRS.from_user_input(grid["crs"]).to_wkt(),
        "x_coord_cell_zero": grid["origin"][0],
        "y_coord_cell_zero": grid["origin"][1],
        "lower_left_cell_x": 0,
        "lower_left_cell_y": 0,
        "units": "MM",
        "data_type": "PER-CUM",
        "compression": "ZLIB",
    }
    written = RasDss.write_grid_timeseries(
        output,
        inputs.request["pathname"],
        inputs.frames,
        native_times,
        grid_info,
        dss_version=7,
    )
    if len(written) != len(inputs.frames) or len(set(written)) != len(written):
        raise ValueError("Grid writer did not return one distinct record per interval")
    if not output.is_file() or output.stat().st_size == 0:
        raise ValueError("Grid writer did not produce a nonempty DSS file")
    catalog = RasDss.get_catalog(output)
    paths = list(catalog["pathname"])
    if len(paths) != len(written) or set(paths) != set(written):
        raise ValueError("Readback catalog must contain exactly the requested records")
    records = []
    selector = inputs.request["pathname"].split("/")
    for index, pathname in enumerate(written):
        parts = pathname.split("/")
        if len(parts) != 8 or any(
            parts[key] != selector[key] for key in (0, 1, 2, 3, 6, 7)
        ):
            raise ValueError("Readback pathname family differs from the request")
        if (_grid_time(parts[4]), _grid_time(parts[5])) != tuple(
            native_times[index : index + 2]
        ):
            raise ValueError(
                "Readback pathname window differs from the requested interval"
            )
        read = RasDss.read_grid(output, pathname)
        # The pinned companion owns parsing Java header spellings and DSS 2400.
        RasDss._validate_grid_raw_timing(
            read, native_times[index], native_times[index + 1], pathname
        )
        values = np.asarray(read["data"], dtype=np.float64)
        if values.shape != inputs.mask.shape:
            raise ValueError("Readback grid shape differs from the source")
        if not np.isnan(values[~inputs.mask]).all():
            raise ValueError(
                "Readback outside-domain NoData mask differs from the source"
            )
        if (
            tuple(read["shape"]) != inputs.mask.shape
            or tuple(read["metadata"]["shape"]) != inputs.mask.shape
            or read["metadata"]["number_missing"] != int((~inputs.mask).sum())
        ):
            raise ValueError(
                "Readback shape or missing-cell header differs from the source"
            )
        if (
            not np.isfinite(values[inputs.mask]).all()
            or (values[inputs.mask] < 0).any()
        ):
            raise ValueError("Readback contains missing or negative required rainfall")
        error = float(
            np.max(np.abs(values[inputs.mask] - inputs.frames[index, inputs.mask]))
        )
        if error > inputs.tolerance:
            raise ValueError("Readback rainfall differs beyond the declared tolerance")
        if (
            str(read["units"]).upper() != "MM"
            or str(read["data_type"]).upper().replace("_", "-") != "PER-CUM"
        ):
            raise ValueError(
                "Readback units or data type differ from incremental MM / PER-CUM"
            )
        if (
            not np.isclose(
                float(read["cell_size"]), grid["cell_size"], rtol=0, atol=1e-9
            )
            or not np.allclose(
                read["metadata"]["origin"], grid["origin"], rtol=0, atol=1e-9
            )
            or not CRS.from_user_input(read["crs"]).equals(
                CRS.from_user_input(grid["crs"])
            )
        ):
            raise ValueError("Readback grid geometry or CRS differs from the source")
        if (read["start_time"], read["end_time"]) != tuple(
            native_times[index : index + 2]
        ):
            raise ValueError(
                "Readback metadata window differs from the requested interval"
            )
        records.append(
            {
                "pathname": pathname,
                "start": inputs.source["times"][index],
                "end": inputs.source["times"][index + 1],
                "required_cell_count": int(inputs.mask.sum()),
                "outside_nodata_cells": int((~inputs.mask).sum()),
                "max_absolute_error_mm": error,
            }
        )
    return {
        "schema": RESULT_SCHEMA,
        "status": "readback_verified",
        "request_sha256": inputs.request_sha256,
        "source_sha256": inputs.request["source"]["sha256"],
        "domain_sha256": inputs.request["expected_domain_sha256"],
        "records": records,
        "record_count": len(records),
        "units": "MM",
        "data_type": "PER-CUM",
        "clock": "UTC",
        "grid": grid,
        "interval_seconds": inputs.source["interval_seconds"],
        "absolute_value_tolerance_mm": inputs.tolerance,
        "forecast_eligible": False,
    }


def _run(request_path: Path, result_path: Path) -> int:
    """Write a staged DSS and receipt; only a reconciled parent can accept it."""
    try:
        inputs = _load(request_path)
        if result_path in inputs.protected_paths or result_path == inputs.output:
            raise ValueError("Worker result must not alias an input or output")
        if result_path.exists() or inputs.output.exists():
            raise FileExistsError("Refusing existing worker result or DSS output")
        inputs.output.parent.mkdir(parents=True, exist_ok=True)
        # A unique staging directory avoids concurrent native writers sharing a DSS.
        # Keep failures for diagnosis; a failed attempt never publishes its DSS.
        stage = Path(
            tempfile.mkdtemp(prefix=".forecast-grid-", dir=inputs.output.parent)
        )
        staged_output = stage / "grid.dss"
        receipt = _native_write(inputs, staged_output)
        receipt["staged_output"] = str(staged_output)
        # Reauthenticate all inputs after native I/O to catch changes mid-write.
        if _load(request_path).request_sha256 != inputs.request_sha256:
            raise ValueError("Request changed during native writing")
        _write_new(result_path, receipt)
        return 0
    except Exception as exc:
        # This is the process failure boundary, not a substitute for validation.
        if not result_path.exists() and result_path != request_path:
            try:
                _write_new(
                    result_path,
                    {
                        "schema": RESULT_SCHEMA,
                        "status": "failed",
                        "error": {"type": type(exc).__name__, "message": str(exc)},
                    },
                )
            except OSError:
                pass
        return 2
