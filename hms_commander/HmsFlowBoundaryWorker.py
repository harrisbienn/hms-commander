"""Materialize one authenticated portable flow series without changing its values.

This contract is independent of hydrologic-handoff 1.0. The caller owns reach
selection and mapping policy; the worker records an explicit identity routing
decision and never sums, scales, interpolates, or substitutes source records.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Union
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from .Decorators import log_call
from .HmsBoundaryTransformer import _parts, _series_identity
from .HmsTimeSeriesWorker import (
    HmsTimeSeriesWorker,
    _json_sha256,
    _ordinate_sha256,
    _sha256,
    _timestamp_sha256,
    _validate_wall_clock_index,
)
from ._process import _isolated_worker_command, dss_environment, run_owned
from .dss import DssCore

logger = logging.getLogger(__name__)
_MAX_SAMPLES = 100_000
_MAX_SOURCE_BYTES = 16 * 1024 * 1024


class HmsFlowBoundaryWorkerError(RuntimeError):
    """Worker failure with a stable classification and process exit code."""

    def __init__(self, message: str, *, classification: str, exit_code: int) -> None:
        super().__init__(message)
        self.classification = classification
        self.exit_code = exit_code


class HmsFlowBoundaryWorker:
    """Static API for the portable flow-boundary materialization contract."""

    SOURCE_SCHEMA = "hms-commander/portable-flow-series/1.0"
    REQUEST_SCHEMA = "hms-commander/flow-boundary-request/1.0"
    RESULT_SCHEMA = "hms-commander/flow-boundary-result/1.0"

    @staticmethod
    @log_call
    def run(request_path: Union[str, Path], result_path: Union[str, Path]) -> int:
        """Write a new DSS and immutable evidence, or verify identical reuse.

        Args:
            request_path (str | Path): Local JSON request. Relative asset paths
                resolve against this file's directory.
            result_path (str | Path): New evidence JSON path. Existing successful
                evidence is reused only after source, output, and binding checks.

        Returns:
            int: Zero for success or reuse; 2 for invalid input, 3 for identity
            or publication conflicts, and 4 for native materialization failure.
            New failures write a result when its destination remains available.
        """
        request_file, result_file = (
            Path(request_path).resolve(),
            Path(result_path).resolve(),
        )
        request: Optional[dict[str, Any]] = None
        failure_destination_safe = True
        stage: Optional[Path] = None
        try:
            try:
                request, prepared = _load(request_file)
            except (
                ValueError,
                KeyError,
                TypeError,
                UnicodeError,
                OverflowError,
            ) as exc:
                raise HmsFlowBoundaryWorkerError(
                    str(exc), classification="invalid_input", exit_code=2
                ) from exc
            output = Path(request["output_dss"])
            if result_file in {request_file, Path(request["source"]["path"]), output}:
                failure_destination_safe = False
                raise HmsFlowBoundaryWorkerError(
                    "Result must differ from request, source, and output paths",
                    classification="invalid_input",
                    exit_code=2,
                )
            if result_file.exists():
                _verify(result_file, request, prepared)
                return 0
            if output.exists():
                raise _conflict(f"Refusing existing output DSS: {output}")
            output.parent.mkdir(parents=True, exist_ok=True)
            # A private sibling workspace prevents concurrent native writers from
            # sharing partial files. Only fully read-back bytes are published.
            stage = Path(
                tempfile.mkdtemp(prefix=".flow-boundary-", dir=output.parent)
            ).resolve()
            staged_output = stage / "boundary.dss"
            receipt = HmsFlowBoundaryWorker._run_native(request, staged_output)
            expected = _binding(request, prepared)
            if _json_sha256(receipt) != _json_sha256(expected):
                raise RuntimeError(
                    "DSS child evidence does not match the authenticated series"
                )
            _authenticate_source(request)
            if not staged_output.is_file() or not staged_output.stat().st_size:
                raise RuntimeError("DSS child did not create a nonempty output")
            output_sha256 = _sha256(staged_output)
            # link() is exclusive on Windows and POSIX; replace() could
            # silently overwrite a competing publisher on POSIX.
            os.link(staged_output, output)
            result = {
                "schema": HmsFlowBoundaryWorker.RESULT_SCHEMA,
                "status": "succeeded",
                **expected,
                "output_sha256": output_sha256,
                "output_bytes": output.stat().st_size,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "forecast_eligible": False,
            }
            _publish_json(result_file, result)
            # Retain native logs/ownership records whenever processing fails.
            # Only this newly-created, verified sibling directory is retired.
            if (
                stage.parent == output.parent.resolve()
                and stage.name.startswith(".flow-boundary-")
                and not stage.is_symlink()
            ):
                try:
                    shutil.rmtree(stage)
                except OSError as exc:
                    # Output and evidence are already accepted. A scanner or
                    # filesystem lock can delay retirement without invalidating
                    # the completed native operation.
                    logger.warning("Retained completed attempt %s: %s", stage, exc)
            logger.info("Materialized flow boundary %s", request["operation_id"])
            return 0
        except HmsFlowBoundaryWorkerError as exc:
            error = exc
        except FileExistsError as exc:
            error = _conflict(str(exc))
        except Exception as exc:
            # Deliberate process boundary: native failures produce no success
            # evidence and retain any already-published output for diagnosis.
            error = HmsFlowBoundaryWorkerError(
                str(exc), classification="materialization_failed", exit_code=4
            )
        logger.error("Flow boundary failed: %s", error)
        # A competing publisher may own this result name. Conflict losers must
        # never race the successful writer with a terminal failure receipt.
        if (
            failure_destination_safe
            and error.exit_code != 3
            and not result_file.exists()
        ):
            try:
                _publish_json(
                    result_file,
                    {
                        "schema": HmsFlowBoundaryWorker.RESULT_SCHEMA,
                        "status": "failed",
                        "request_sha256": _json_sha256(request) if request else None,
                        "classification": error.classification,
                        "message": str(error),
                        "forecast_eligible": False,
                        "diagnostics_directory": (
                            str(stage) if stage is not None else None
                        ),
                    },
                )
            except OSError as exc:
                logger.error("Could not publish failure evidence: %s", exc)
        return error.exit_code

    @staticmethod
    @log_call
    def verify(
        request_path: Union[str, Path], result_path: Union[str, Path]
    ) -> dict[str, Any]:
        """Authenticate a completed materialization for a supplied request.

        Args:
            request_path (str | Path): Original request, with unchanged source.
            result_path (str | Path): Successful evidence to authenticate.

        Returns:
            dict[str, Any]: Verified result binding source bytes, physical/model
            clocks, selector, routing, samples, units, and output DSS checksum.

        Raises:
            ValueError: Request or source violates the versioned contract.
            HmsFlowBoundaryWorkerError: Evidence, source, or output differs.
            OSError: A required file cannot be read.

        Notes:
            This checks the recorded native readback and immutable output hash.
            It does not rerun Java or grant model/forecast qualification.
        """
        request_file, result_file = (
            Path(request_path).resolve(),
            Path(result_path).resolve(),
        )
        request, prepared = _load(request_file)
        return _verify(result_file, request, prepared)

    @staticmethod
    def _run_native(request: Mapping[str, Any], output: Path) -> dict[str, Any]:
        child_request, child_result = (
            output.parent / "request.json",
            output.parent / "readback.json",
        )
        _publish_json(child_request, request)
        # Bind the child to this installed/source package, not a roaming-user
        # install or cwd. run_owned bounds and terminates the entire process tree.
        completed = run_owned(
            _isolated_worker_command(
                "flow-boundary",
                str(child_request),
                str(output),
                str(child_result),
            ),
            timeout=300,
            cwd=output.parent,
            output_dir=output.parent,
            env=dss_environment(),
        )
        if completed.timed_out:
            raise RuntimeError("Flow boundary DSS child exceeded 300 seconds")
        if completed.returncode:
            raise RuntimeError(
                f"Flow boundary DSS child failed: {completed.stderr[-2000:]}"
            )
        return _read_json(child_result, 64 * 1024)

    @staticmethod
    def _materialize_in_process(
        request: Mapping[str, Any], prepared: Mapping[str, Any], output: Path
    ) -> dict[str, Any]:
        if output.exists():
            raise FileExistsError(f"Refusing existing DSS child output: {output}")
        DssCore.write_timeseries(
            output,
            request["output_pathname"],
            prepared["target_index"],
            prepared["values"],
            units=request["units"],
            data_type="INST-VAL",
            interval_minutes=request["interval_minutes"],
        )
        catalog = DssCore.get_catalog(output)
        if {_series_identity(pathname) for pathname in catalog} != {
            _series_identity(request["output_pathname"])
        }:
            raise RuntimeError(
                "DSS catalog differs from the requested logical flow series"
            )
        HmsTimeSeriesWorker._verify_written_series(output, [prepared])
        return _binding(request, prepared)


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON field: {key}")
        result[key] = value
    return result


def _read_json(path: Path, maximum_bytes: int) -> dict[str, Any]:
    with path.open("rb") as stream:
        raw = stream.read(maximum_bytes + 1)
    if len(raw) > maximum_bytes:
        raise ValueError(f"JSON exceeds {maximum_bytes} bytes: {path.name}")
    return _decode(raw)


def _decode(raw: bytes) -> dict[str, Any]:
    def invalid_constant(value: str) -> None:
        raise ValueError(f"Non-finite JSON number: {value}")

    result = json.loads(
        raw.decode("utf-8"), object_pairs_hook=_unique, parse_constant=invalid_constant
    )
    if not isinstance(result, dict):
        raise ValueError("JSON root must be an object")
    return result


def _fields(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(f"{label} must contain exactly: {', '.join(sorted(keys))}")
    return value


def _text(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(ord(c) < 32 for c in value)
    ):
        raise ValueError(
            f"{label} must be a nonempty string without surrounding whitespace or controls"
        )
    return value


def _utc(value: Any) -> datetime:
    text = _text(value, "timestamp")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:00(?:Z|[+-]\d{2}:\d{2})", text):
        raise ValueError("Physical timestamps must use minute-aligned RFC 3339 syntax")
    moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if (
        moment.tzinfo is None
        or moment.utcoffset() is None
        or moment.second
        or moment.microsecond
    ):
        raise ValueError("Physical timestamps must be offset-aware and minute-aligned")
    return moment.astimezone(timezone.utc)


def _authenticate_source(request: Mapping[str, Any]) -> bytes:
    source = Path(request["source"]["path"])
    try:
        with source.open("rb") as stream:
            raw = stream.read(_MAX_SOURCE_BYTES + 1)
    except OSError as exc:
        raise _conflict(f"Portable flow source cannot be read: {source}") from exc
    if len(raw) > _MAX_SOURCE_BYTES:
        raise ValueError("Portable flow source exceeds 16 MiB")
    if hashlib.sha256(raw).hexdigest() != request["source"]["sha256"]:
        raise _conflict(
            "Portable flow source bytes do not match preparation_source_sha256"
        )
    return raw


def _load(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    request = _read_json(path, 64 * 1024)
    _fields(
        request,
        {
            "schema",
            "operation_id",
            "source",
            "model_window",
            "interval_minutes",
            "units",
            "output_dss",
            "output_pathname",
            "routing",
            "alignment_policy",
            "missing_value_policy",
            "conversion",
            "unit_policy",
            "sentinel_threshold",
        },
        "request",
    )
    if request["schema"] != HmsFlowBoundaryWorker.REQUEST_SCHEMA:
        raise ValueError("Unsupported flow-boundary request schema")
    _text(request["operation_id"], "operation_id")
    if (
        request["alignment_policy"],
        request["missing_value_policy"],
        request["conversion"],
    ) != ("exact", "reject", "identity"):
        raise ValueError(
            "Only exact alignment, rejected missing values, and identity conversion are supported"
        )
    source = _fields(request["source"], {"path", "sha256", "selector"}, "source")
    if not re.fullmatch(r"[0-9a-f]{64}", _text(source["sha256"], "source.sha256")):
        raise ValueError("source.sha256 must be a lowercase SHA-256")
    _text(source["selector"], "source.selector")
    source["path"] = str((path.parent / _text(source["path"], "source.path")).resolve())
    request["output_dss"] = str(
        (path.parent / _text(request["output_dss"], "output_dss")).resolve()
    )
    if Path(request["output_dss"]) in {path, Path(source["path"])}:
        raise ValueError("Output must differ from request and source paths")
    if Path(request["output_dss"]).suffix.lower() != ".dss":
        raise ValueError("output_dss must have a .dss extension")
    routing = _fields(
        request["routing"],
        {"method", "mapping_id", "target_boundary_id", "contribution_id"},
        "routing",
    )
    for key, value in routing.items():
        _text(value, f"routing.{key}")
    if routing["method"] != "identity":
        raise ValueError(
            "Only caller-declared identity routing is supported; route upstream separately"
        )
    if request["units"] not in {"CMS", "CFS"}:
        raise ValueError("Native units must be CMS or CFS")
    if request["unit_policy"] not in {"identity", "m3/s-to-CMS-alias"}:
        raise ValueError(
            "unit_policy must explicitly select identity or m3/s-to-CMS-alias"
        )
    interval = request["interval_minutes"]
    if isinstance(interval, bool) or not isinstance(interval, int) or interval <= 0:
        raise ValueError("interval_minutes must be a positive integer")
    threshold = request["sentinel_threshold"]
    if (
        isinstance(threshold, bool)
        or not isinstance(threshold, (int, float))
        or not math.isfinite(threshold)
        or threshold >= 0
    ):
        raise ValueError("sentinel_threshold must be a finite negative number")
    parts = _parts(_text(request["output_pathname"], "output_pathname"))
    match = re.fullmatch(r"([1-9][0-9]*)(MIN|MINUTE|HOUR|DAY)", parts[4], re.IGNORECASE)
    if (
        parts[2].upper() != "FLOW"
        or parts[3]
        or not match
        or int(match[1])
        * {"MIN": 1, "MINUTE": 1, "HOUR": 60, "DAY": 1440}[match[2].upper()]
        != interval
    ):
        raise ValueError(
            "Output pathname must use FLOW, an empty D-part, and the declared interval E-part"
        )
    window = _fields(
        request["model_window"], {"start", "end", "time_zone"}, "model_window"
    )
    start, end = _utc(window["start"]), _utc(window["end"])
    duration_minutes = (end - start).total_seconds() / 60
    if (
        duration_minutes <= 0
        or duration_minutes % interval
        or duration_minutes / interval + 1 > _MAX_SAMPLES
    ):
        raise ValueError(
            "Model window must contain 2..100000 inclusive regular samples"
        )
    time_zone = _text(window["time_zone"], "model_window.time_zone")
    zone = ZoneInfo(time_zone)
    window.update(start=start.isoformat(), end=end.isoformat())
    expected_index = pd.date_range(start, end, freq=f"{interval}min")
    target_index = expected_index.tz_convert(zone).tz_localize(None)
    _validate_wall_clock_index(target_index, time_zone, "model window")
    portable = _decode(_authenticate_source(request))
    _fields(
        portable,
        {
            "schema",
            "selector",
            "units",
            "data_type",
            "interval_minutes",
            "timestamps",
            "values",
        },
        "portable source",
    )
    if (
        portable["schema"] != HmsFlowBoundaryWorker.SOURCE_SCHEMA
        or portable["selector"] != source["selector"]
    ):
        raise ValueError("Portable source schema or exact selector does not match")
    source_units = (
        "m3/s" if request["unit_policy"] == "m3/s-to-CMS-alias" else request["units"]
    )
    if (
        portable["units"] != source_units
        or (request["unit_policy"] == "m3/s-to-CMS-alias" and request["units"] != "CMS")
        or portable["data_type"] != "INST-VAL"
        or type(portable["interval_minutes"]) is not int
        or portable["interval_minutes"] != interval
    ):
        raise ValueError(
            "Portable source units, instantaneous type, or interval differs"
        )
    timestamps, values = portable["timestamps"], portable["values"]
    if (
        not isinstance(timestamps, list)
        or not isinstance(values, list)
        or len(timestamps) != len(expected_index)
        or len(values) != len(expected_index)
    ):
        raise ValueError(
            "Portable series must provide every inclusive model-window sample exactly once"
        )
    source_index = pd.DatetimeIndex([_utc(value) for value in timestamps])
    if not source_index.equals(expected_index):
        raise ValueError(
            "Portable timestamps must match the complete ordered physical sample lattice"
        )
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= threshold
        for value in values
    ):
        raise ValueError(
            "Portable flow values must be finite numbers above the sentinel threshold"
        )
    ordinates = np.asarray(values, dtype=np.float64)
    prepared = {
        "source_units": source_units,
        "series_id": request["operation_id"],
        "output_pathname": request["output_pathname"],
        "interval_minutes": interval,
        "units": request["units"],
        "data_type": "INST-VAL",
        "target_index": target_index,
        "values": ordinates,
        "ordinate_sha256": _ordinate_sha256(ordinates),
        "target_timestamp_sha256": _timestamp_sha256(target_index),
        "physical_timestamp_sha256": _timestamp_sha256(source_index),
    }
    return request, prepared


def _binding(request: Mapping[str, Any], prepared: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "request_sha256": _json_sha256(request),
        "operation_id": request["operation_id"],
        "preparation_source_sha256": request["source"]["sha256"],
        "source_selector": request["source"]["selector"],
        "model_window": request["model_window"],
        "routing": request["routing"],
        "output_dss": request["output_dss"],
        "output_pathname": request["output_pathname"],
        "source_units": prepared["source_units"],
        "native_units": request["units"],
        "unit_policy": request["unit_policy"],
        "data_type": "INST-VAL",
        "interval_minutes": request["interval_minutes"],
        "alignment_policy": "exact",
        "missing_value_policy": "reject",
        "conversion": "identity",
        "readback": {
            "status": "verified",
            "endpoint_semantics": "inclusive_instantaneous",
            "sample_count": len(prepared["values"]),
            "ordinate_sha256": prepared["ordinate_sha256"],
            "physical_timestamp_sha256": prepared["physical_timestamp_sha256"],
            "model_timestamp_sha256": prepared["target_timestamp_sha256"],
            "model_start": prepared["target_index"][0].isoformat(),
            "model_end": prepared["target_index"][-1].isoformat(),
        },
    }


def _verify(
    result_path: Path, request: Mapping[str, Any], prepared: Mapping[str, Any]
) -> dict[str, Any]:
    result = _read_json(result_path, 64 * 1024)
    expected = {
        "schema": HmsFlowBoundaryWorker.RESULT_SCHEMA,
        "status": "succeeded",
        **_binding(request, prepared),
        "forecast_eligible": False,
    }
    if set(result) != set(expected) | {
        "created_at",
        "output_sha256",
        "output_bytes",
    } or _json_sha256({key: result[key] for key in expected}) != _json_sha256(expected):
        raise _conflict(
            "Existing result does not match the complete source/request/readback binding"
        )
    created = datetime.fromisoformat(_text(result["created_at"], "created_at"))
    if created.tzinfo is None:
        raise _conflict("Existing result has no creation time zone")
    output = Path(request["output_dss"])
    if (
        type(result["output_bytes"]) is not int
        or not output.is_file()
        or not output.stat().st_size
        or output.stat().st_size != result["output_bytes"]
        or _sha256(output) != result["output_sha256"]
    ):
        raise _conflict(
            "Existing output does not match its authenticated materialization evidence"
        )
    return result


def _conflict(message: str) -> HmsFlowBoundaryWorkerError:
    return HmsFlowBoundaryWorkerError(
        message, classification="identity_conflict", exit_code=3
    )


def _publish_json(path: Path, payload: Mapping[str, Any]) -> None:
    encoded = (
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")
    if len(encoded) > 64 * 1024:
        raise ValueError("Worker control/evidence JSON exceeds 64 KiB")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".flow-evidence-", dir=path.parent
    ) as temporary:
        staged = Path(temporary) / "result.json"
        staged.write_bytes(encoded)
        os.link(staged, path)


def _child_main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Private flow-boundary DSS child")
    parser.add_argument("request")
    parser.add_argument("output")
    parser.add_argument("result")
    arguments = parser.parse_args(argv)
    request, prepared = _load(Path(arguments.request).resolve())
    receipt = HmsFlowBoundaryWorker._materialize_in_process(
        request, prepared, Path(arguments.output).resolve()
    )
    _publish_json(Path(arguments.result).resolve(), receipt)
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    """Run the versioned flow-boundary command-line interface."""
    parser = argparse.ArgumentParser(
        description="Materialize an authenticated portable flow boundary"
    )
    parser.add_argument("--request", required=True)
    parser.add_argument("--result", required=True)
    arguments = parser.parse_args(argv)
    return HmsFlowBoundaryWorker.run(arguments.request, arguments.result)


if __name__ == "__main__":
    raise SystemExit(main())
