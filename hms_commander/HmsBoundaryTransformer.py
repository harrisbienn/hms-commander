"""Explicit hourly storage routing from a supplied data-only polynomial curve.

This module reproduces a specified numerical method; it neither fits a curve
nor grants engineering acceptance. Storage coordinates use the fitted curve's
flow-times-hours convention, not an implicitly converted physical volume.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping, Union

import numpy as np
import pandas as pd

from .Decorators import log_call
from .dss import DssCore

logger = logging.getLogger(__name__)
_METHOD = "explicit-storage-polynomial-v1"


def _unique_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate curve field: {key}")
        result[key] = value
    return result


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        digest = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _identity(path: Path) -> tuple[int, int, int]:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns, stat.st_ino


def _parts(pathname: str) -> tuple[str, ...]:
    if (
        not isinstance(pathname, str)
        or not pathname.startswith("/")
        or not pathname.endswith("/")
    ):
        raise ValueError("DSS selector must be a six-part pathname")
    parts = tuple(pathname.split("/")[1:-1])
    if len(parts) != 6 or any(any(c in p for c in "*?{}") for p in parts):
        raise ValueError(
            "DSS selector must be explicit; wildcards and templates are not supported"
        )
    if not parts[1] or not parts[2] or not parts[4] or not parts[5]:
        raise ValueError("DSS selector requires B, C, E and F parts")
    return parts


def _series_identity(pathname: str) -> tuple[str, ...]:
    parts = _parts(pathname)
    return tuple(p.upper() for i, p in enumerate(parts) if i != 3)


def _curve(
    model: Mapping[str, Any],
) -> tuple[np.ndarray, np.ndarray, float, float, float]:
    required = {
        "schema_version",
        "method",
        "breakpoints",
        "coefficients",
        "storage_min",
        "storage_max",
        "initial_storage",
        "initial_outflow",
        "time_step_hours",
        "storage_bounds_policy",
    }
    if not required.issubset(model) or set(model) - required - {"source_sha256"}:
        raise ValueError("Curve has missing or unknown fields")
    if (
        model["schema_version"] != 1
        or isinstance(model["schema_version"], bool)
        or model["method"] != _METHOD
    ):
        raise ValueError("Unsupported storage-curve schema or method")
    if (
        model["initial_outflow"] != "curve-at-minimum-storage"
        or model["time_step_hours"] != 1.0
        or isinstance(model["time_step_hours"], bool)
        or model["storage_bounds_policy"] != "clip-and-report"
    ):
        raise ValueError(
            "Unsupported initialization, time step or storage bounds policy"
        )
    x = np.asarray(model["breakpoints"], dtype=float)
    c = np.asarray(model["coefficients"], dtype=float)
    low, high, initial = (
        float(model[k]) for k in ("storage_min", "storage_max", "initial_storage")
    )
    if (
        x.ndim != 1
        or len(x) < 2
        or c.shape != (4, len(x) - 1)
        or not np.isfinite(x).all()
        or not np.isfinite(c).all()
        or not (np.diff(x) > 0).all()
        or not all(math.isfinite(v) for v in (low, high, initial))
    ):
        raise ValueError(
            "Curve requires finite, increasing breakpoints and four coefficient rows"
        )
    if low != x[0] or high != x[-1] or low >= high:
        raise ValueError("Storage bounds must equal the curve endpoint breakpoints")
    return x, c, low, high, initial


def _evaluate(value: float, x: np.ndarray, c: np.ndarray) -> float:
    index = min(max(int(np.searchsorted(x, value, side="right")) - 1, 0), len(x) - 2)
    offset = value - x[index]
    # Descending-power coefficients, evaluated without a SciPy runtime dependency.
    return float(
        ((c[0, index] * offset + c[1, index]) * offset + c[2, index]) * offset
        + c[3, index]
    )


class HmsBoundaryTransformer:
    """Static API for a versioned, externally fitted boundary-routing method."""

    @staticmethod
    @log_call
    def transform(frame: pd.DataFrame, model: Mapping[str, Any]) -> pd.DataFrame:
        """Route a regular CFS hydrograph using hourly means and explicit storage.

        Args:
            frame: DatetimeIndex and ``value`` column, with CFS/INST-VAL attrs.
                Timestamps must be naive, regular and span whole hours. The
                last hourly bin contains the terminal sample only by design.
            model: Plain JSON-compatible curve, initialization and bounds data.

        Returns:
            Hourly values with storage and unclipped storage columns. Attributes
            include raw clipping diagnostics and no engineering acceptance.

        Raises:
            ValueError: Invalid curve, series, metadata or unsupported clock.
        """
        x, c, low, high, initial = _curve(model)
        if (
            frame.attrs.get("units", "").upper() != "CFS"
            or frame.attrs.get("type", "").upper() != "INST-VAL"
        ):
            raise ValueError("Boundary routing requires CFS / INST-VAL input")
        times = frame.index
        if (
            not isinstance(times, pd.DatetimeIndex)
            or len(times) < 2
            or times.hasnans
            or times.tz is not None
            or times.has_duplicates
            or not times.is_monotonic_increasing
        ):
            raise ValueError(
                "Input requires at least two ordered, unique, naive timestamps"
            )
        if times[0] != times[0].floor("h") or times[-1] != times[-1].floor("h"):
            raise ValueError("Input start and end must align with hourly bin labels")
        intervals = times.to_series().diff().dropna().dt.total_seconds().to_numpy()
        seconds = float(intervals[0])
        if (
            not (intervals == seconds).all()
            or seconds <= 0
            or seconds % 60
            or 3600 % seconds
        ):
            raise ValueError(
                "Input interval must be regular whole minutes dividing one hour"
            )
        values = pd.to_numeric(frame["value"], errors="raise").to_numpy(dtype=float)
        if not np.isfinite(values).all() or (values < 0).any():
            raise ValueError(
                "Input contains missing, sentinel, nonfinite or negative flow"
            )
        hourly = (
            pd.Series(values, index=times)
            .resample("1h", closed="left", label="left")
            .mean()
        )
        qin = hourly.to_numpy()
        storage = np.zeros(len(qin))
        raw_storage = np.zeros(len(qin))
        qout = np.zeros(len(qin))
        storage[0] = raw_storage[0] = initial
        qout[0] = _evaluate(low, x, c)
        for i in range(1, len(qin)):
            raw_storage[i] = storage[i - 1] + qin[i - 1] - qout[i - 1]
            storage[i] = np.clip(raw_storage[i], low, high)
            qout[i] = _evaluate(storage[i], x, c)
        if (
            not np.isfinite(qout).all()
            or (qout < 0).any()
            or not np.isfinite(raw_storage).all()
        ):
            raise ValueError(
                "Curve routing produced nonfinite storage or nonfinite/negative flow"
            )
        adjustment = storage[1:] - raw_storage[1:]
        result = pd.DataFrame(
            {"value": qout, "storage": storage, "unclipped_storage": raw_storage},
            index=hourly.index,
        )
        result.attrs.update(
            units="CFS",
            type="INST-VAL",
            interval_minutes=60,
            method=_METHOD,
            engineering_accepted=False,
            clipping_count=int(np.count_nonzero(adjustment)),
            lower_clip_count=int(np.sum(raw_storage[1:] < low)),
            upper_clip_count=int(np.sum(raw_storage[1:] > high)),
            storage_adjustment_sum=float(adjustment.sum()),
            maximum_absolute_storage_adjustment=float(np.max(np.abs(adjustment))),
            initial_storage=initial,
            initial_outflow=float(qout[0]),
            terminal_bin_sample_count=1,
            input_interval_minutes=seconds / 60,
        )
        return result

    @staticmethod
    @log_call
    def materialize(
        source_dss: Union[str, Path],
        source_pathname: str,
        curve_path: Union[str, Path],
        output_dss: Union[str, Path],
        output_pathname: str,
        *,
        source_sha256: str,
        curve_sha256: str,
    ) -> dict[str, Any]:
        """Write one routed DSS series and return checksum-bound raw evidence.

        Args:
            source_dss: Run-owned HMS output or inspection copy, never a canonical model.
            source_pathname: Exact logical DSS selector; monthly/day D-parts may differ.
            curve_path: Data-only JSON; pickle and executable objects are unsupported.
            output_dss: New run-owned destination. Existing files are refused.
            output_pathname: Explicit output selector with a 1HOUR/1Hour E-part.
            source_sha256: Expected SHA-256 of the complete source DSS.
            curve_sha256: Expected SHA-256 of the complete curve JSON.

        Returns:
            Portable input/output hashes, selectors, time coverage and diagnostics.
            The caller records scenario identity and persists the returned evidence.

        Raises:
            ValueError: Invalid input, selector, checksum or output readback.
            FileExistsError: Output already exists; partial outputs also require a new path.
            RuntimeError: Source changed during extraction.
        """
        source, curve, output = (
            Path(p).resolve() for p in (source_dss, curve_path, output_dss)
        )
        if output.exists():
            raise FileExistsError(f"Refusing to overwrite boundary DSS: {output.name}")
        # Own the Java process so native advisory locks end before final hashing.
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="boundary-transform-", dir=output.parent
        ) as temporary:
            request = Path(temporary) / "request.json"
            response = Path(temporary) / "response.json"
            request.write_text(
                json.dumps(
                    {
                        "source_dss": str(source),
                        "source_pathname": source_pathname,
                        "curve_path": str(curve),
                        "output_dss": str(output),
                        "output_pathname": output_pathname,
                        "source_sha256": source_sha256,
                        "curve_sha256": curve_sha256,
                    }
                ),
                encoding="utf-8",
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-m",
                    "hms_commander.HmsBoundaryTransformer",
                    str(request),
                    str(response),
                ],
                capture_output=True,
                text=True,
                timeout=300,
            )
            if completed.returncode != 0:
                raise RuntimeError(
                    "Boundary transformation failed: " + completed.stderr[-2000:]
                )
            result = json.loads(response.read_text(encoding="utf-8"))
        if _sha256(source) != source_sha256 or _sha256(curve) != curve_sha256:
            raise RuntimeError(
                "Source DSS or fitted curve changed during transformation"
            )
        result["output_sha256"] = _sha256(output)
        return result

    @staticmethod
    def _materialize_in_process(
        source_dss: str,
        source_pathname: str,
        curve_path: str,
        output_dss: str,
        output_pathname: str,
        *,
        source_sha256: str,
        curve_sha256: str,
    ) -> dict[str, Any]:
        source, curve, output = (
            Path(p).resolve() for p in (source_dss, curve_path, output_dss)
        )
        if output.exists():
            raise FileExistsError(f"Refusing to overwrite boundary DSS: {output.name}")
        if _parts(output_pathname)[4].upper() != "1HOUR":
            raise ValueError("Output pathname must use an hourly E-part")
        identity = _series_identity(source_pathname)
        source_identity = _identity(source)
        actual_source = _sha256(source)
        curve_bytes = curve.read_bytes()
        actual_curve = hashlib.sha256(curve_bytes).hexdigest()
        if actual_source != source_sha256 or actual_curve != curve_sha256:
            raise ValueError("Source DSS or curve checksum mismatch")
        model = json.loads(curve_bytes, object_pairs_hook=_unique_fields)
        _curve(model)
        matches = [
            p for p in DssCore.get_catalog(source) if _series_identity(p) == identity
        ]
        if not matches:
            raise ValueError("Exact source series is absent from DSS catalog")
        frame = DssCore.read_timeseries(source, source_pathname)
        result = HmsBoundaryTransformer.transform(frame, model)
        if _identity(source) != source_identity:
            raise RuntimeError("Source DSS changed during boundary extraction")
        output.parent.mkdir(parents=True, exist_ok=True)
        # Reserve the output name before invoking the package writer.
        with output.open("xb"):
            pass
        DssCore.write_timeseries(
            output,
            output_pathname,
            result.index,
            result["value"],
            units="CFS",
            data_type="INST-VAL",
            interval_minutes=60,
        )
        output_identity = _identity(output)
        catalog = DssCore.get_catalog(output)
        if {_series_identity(p) for p in catalog} != {
            _series_identity(output_pathname)
        }:
            raise ValueError(
                "Boundary DSS does not contain exactly the requested logical series"
            )
        readback = DssCore.read_timeseries(output, output_pathname)
        if (
            not readback.index.equals(result.index)
            or not np.array_equal(
                readback["value"].to_numpy(), result["value"].to_numpy()
            )
            or readback.attrs.get("units", "").upper() != "CFS"
            or readback.attrs.get("type", "").upper() != "INST-VAL"
        ):
            raise ValueError("Boundary DSS readback differs from the routed series")
        if _identity(output) != output_identity or _identity(source) != source_identity:
            raise RuntimeError("DSS identity changed during readback")
        logger.info(
            "Routed boundary series: %d samples, %d clipped steps",
            len(result),
            result.attrs["clipping_count"],
        )
        return {
            "schema_version": 1,
            "method": _METHOD,
            "source_sha256": actual_source,
            "source_pathname": source_pathname,
            "curve_sha256": actual_curve,
            "output_pathname": output_pathname,
            "count": len(result),
            "start": result.index[0].isoformat(),
            "end": result.index[-1].isoformat(),
            "diagnostics": dict(result.attrs),
        }


if __name__ == "__main__":
    request_path, response_path = map(Path, sys.argv[1:])
    payload = json.loads(request_path.read_text(encoding="utf-8"))
    evidence = HmsBoundaryTransformer._materialize_in_process(**payload)
    response_path.write_text(json.dumps(evidence, allow_nan=False), encoding="utf-8")
