"""Deterministic process boundary for DSS time-series window translation.

The worker copies complete regular records into a new DSS file and applies one
constant wall-clock offset to their timestamps. It never interpolates,
extrapolates, repeats, scales, or otherwise changes the source ordinates.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Union
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import numpy as np
import pandas as pd

from .Decorators import log_call
from .dss import DssCore

logger = logging.getLogger(__name__)

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class HmsTimeSeriesWorkerError(RuntimeError):
    """Classified time-series worker failure."""

    def __init__(
        self,
        message: str,
        *,
        classification: str,
        exit_code: int,
    ) -> None:
        super().__init__(message)
        self.classification = classification
        self.exit_code = exit_code


class HmsTimeSeriesWorker:
    """Static namespace for the versioned time-series worker contract."""

    REQUEST_SCHEMA = "hms-commander/timeseries-window-request/1.0"
    RESULT_SCHEMA = "hms-commander/timeseries-window-result/1.0"

    @staticmethod
    @log_call
    def run(
        request_path: Union[str, Path],
        result_path: Union[str, Path],
    ) -> int:
        """Translate complete source windows and atomically publish evidence.

        Args:
            request_path: JSON request conforming to ``REQUEST_SCHEMA``.
            result_path: New JSON result path. An existing, successful result
                is accepted only when its request, sources, and output still
                match exactly.

        Returns:
            Zero for success or verified reuse; otherwise a classified
            process-style exit code.
        """
        request_file = Path(request_path).resolve()
        result_file = Path(result_path).resolve()
        request: Optional[dict[str, Any]] = None
        request_sha256: Optional[str] = None
        temporary_output: Optional[Path] = None
        published = False

        try:
            request = HmsTimeSeriesWorker._validate_request(
                HmsTimeSeriesWorker._load_request(request_file)
            )
            request_sha256 = _json_sha256(request)
            output = Path(request["output_dss"])

            if result_file.exists():
                HmsTimeSeriesWorker._verify_completed_result(
                    result_file,
                    request=request,
                    request_sha256=request_sha256,
                )
                logger.info(
                    "Verified reusable DSS time-series result for request %s",
                    request_sha256,
                )
                return 0

            if output.exists():
                raise HmsTimeSeriesWorkerError(
                    f"Refusing unexpected existing output DSS: {output}",
                    classification="existing_output",
                    exit_code=3,
                )

            temporary_output = output.with_name(
                f".{output.stem}.{request_sha256[:12]}.partial{output.suffix}"
            )
            if temporary_output.exists():
                raise HmsTimeSeriesWorkerError(
                    f"Refusing unexpected partial DSS: {temporary_output}",
                    classification="existing_partial_output",
                    exit_code=3,
                )

            output.parent.mkdir(parents=True, exist_ok=True)
            HmsTimeSeriesWorker._verify_source_identities(request)
            records = HmsTimeSeriesWorker._run_dss_subprocess(
                request,
                temporary_output,
                request_sha256=request_sha256,
            )
            # The DSS/JVM process must exit before Windows reliably releases
            # source handles. Re-authenticate only after that boundary.
            HmsTimeSeriesWorker._verify_source_identities(
                request,
                drift=True,
            )
            if not temporary_output.is_file():
                raise RuntimeError(
                    f"DSS bridge did not create output: {temporary_output}"
                )
            temporary_output.replace(output)
            published = True

            result = HmsTimeSeriesWorker._success_result(
                request,
                request_sha256=request_sha256,
                output=output,
                records=records,
            )
            _write_json_new(result_file, result)
            return 0
        except HmsTimeSeriesWorkerError as exc:
            HmsTimeSeriesWorker._write_failure_if_new(
                result_file,
                request=request,
                request_sha256=request_sha256,
                error=exc,
            )
            return exc.exit_code
        except Exception as exc:
            error = HmsTimeSeriesWorkerError(
                str(exc),
                classification="translation_failed",
                exit_code=4,
            )
            HmsTimeSeriesWorker._write_failure_if_new(
                result_file,
                request=request,
                request_sha256=request_sha256,
                error=error,
                error_type=type(exc).__name__,
            )
            return error.exit_code
        finally:
            if temporary_output is not None and not published:
                temporary_output.unlink(missing_ok=True)

    @staticmethod
    def _load_request(path: Path) -> dict[str, Any]:
        if not path.is_file():
            raise HmsTimeSeriesWorkerError(
                f"Time-series worker request does not exist: {path}",
                classification="invalid_request",
                exit_code=2,
            )
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise HmsTimeSeriesWorkerError(
                f"Could not read time-series worker request {path}: {exc}",
                classification="invalid_request",
                exit_code=2,
            ) from exc
        if not isinstance(payload, dict):
            raise HmsTimeSeriesWorkerError(
                "Time-series worker request root must be an object",
                classification="invalid_request",
                exit_code=2,
            )
        return payload

    @staticmethod
    def _validate_request(payload: Mapping[str, Any]) -> dict[str, Any]:
        required = {
            "schema",
            "operation_id",
            "qualification",
            "source_window",
            "target_window",
            "output_dss",
            "sentinel_threshold",
            "series",
        }
        _require_keys(payload, required=required, label="request")
        if payload["schema"] != HmsTimeSeriesWorker.REQUEST_SCHEMA:
            raise _invalid(
                f"Unsupported time-series request schema: {payload['schema']!r}"
            )

        operation_id = _nonempty_string(payload["operation_id"], "operation_id")
        qualification = _object(payload["qualification"], "qualification")
        _require_keys(
            qualification,
            required={"status", "forecast_eligible", "basis_issue"},
            label="qualification",
        )
        if (
            qualification["status"] != "qualification_only"
            or qualification["forecast_eligible"] is not False
        ):
            raise _invalid(
                "qualification must remain qualification_only and "
                "forecast-ineligible"
            )
        basis_issue = _nonempty_string(
            qualification["basis_issue"], "qualification.basis_issue"
        )

        source_window = _object(payload["source_window"], "source_window")
        target_window = _object(payload["target_window"], "target_window")
        _require_keys(
            source_window,
            required={"start", "time_zone"},
            label="source_window",
        )
        _require_keys(
            target_window,
            required={"start", "end", "time_zone"},
            label="target_window",
        )
        source_start = _model_time(source_window["start"], "source_window.start")
        target_start = _model_time(target_window["start"], "target_window.start")
        target_end = _model_time(target_window["end"], "target_window.end")
        if target_end <= target_start:
            raise _invalid("target_window.end must be later than target_window.start")
        source_zone = _nonempty_string(
            source_window["time_zone"], "source_window.time_zone"
        )
        target_zone = _nonempty_string(
            target_window["time_zone"], "target_window.time_zone"
        )
        if source_zone != target_zone:
            raise _invalid("source and target time zones must match exactly")
        try:
            ZoneInfo(source_zone)
        except ZoneInfoNotFoundError as exc:
            raise _invalid(f"Unknown IANA time zone: {source_zone}") from exc

        threshold = payload["sentinel_threshold"]
        if (
            isinstance(threshold, bool)
            or not isinstance(threshold, (int, float))
            or not math.isfinite(float(threshold))
            or float(threshold) >= 0
        ):
            raise _invalid("sentinel_threshold must be a finite negative number")

        raw_series = payload["series"]
        if not isinstance(raw_series, list) or not raw_series:
            raise _invalid("series must be a non-empty array")
        normalized_series = []
        for index, raw_item in enumerate(raw_series):
            item = _object(raw_item, f"series[{index}]")
            _require_keys(
                item,
                required={
                    "series_id",
                    "source_dss",
                    "source_sha256",
                    "source_pathname",
                    "output_pathname",
                    "interval_minutes",
                    "units",
                    "data_type",
                },
                label=f"series[{index}]",
            )
            interval = _positive_int(
                item["interval_minutes"], f"series[{index}].interval_minutes"
            )
            duration_minutes = int((target_end - target_start).total_seconds() // 60)
            if duration_minutes % interval:
                raise _invalid(
                    f"series[{index}] interval does not divide the target window"
                )
            source_pathname = _pathname(
                item["source_pathname"], f"series[{index}].source_pathname"
            )
            output_pathname = _pathname(
                item["output_pathname"], f"series[{index}].output_pathname"
            )
            normalized_series.append(
                {
                    "series_id": _nonempty_string(
                        item["series_id"], f"series[{index}].series_id"
                    ),
                    "source_dss": str(
                        Path(
                            _nonempty_string(
                                item["source_dss"], f"series[{index}].source_dss"
                            )
                        ).resolve()
                    ),
                    "source_sha256": _sha256_string(
                        item["source_sha256"], f"series[{index}].source_sha256"
                    ),
                    "source_pathname": source_pathname,
                    "output_pathname": output_pathname,
                    "interval_minutes": interval,
                    "units": _nonempty_string(
                        item["units"], f"series[{index}].units"
                    ).upper(),
                    "data_type": _nonempty_string(
                        item["data_type"], f"series[{index}].data_type"
                    ).upper(),
                }
            )

        for field in ("series_id", "output_pathname"):
            values = [item[field].casefold() for item in normalized_series]
            if len(values) != len(set(values)):
                raise _invalid(f"series {field} values must be unique")

        return {
            "schema": HmsTimeSeriesWorker.REQUEST_SCHEMA,
            "operation_id": operation_id,
            "qualification": {
                "status": "qualification_only",
                "forecast_eligible": False,
                "basis_issue": basis_issue,
            },
            "source_window": {
                "start": source_start.isoformat(timespec="seconds"),
                "time_zone": source_zone,
            },
            "target_window": {
                "start": target_start.isoformat(timespec="seconds"),
                "end": target_end.isoformat(timespec="seconds"),
                "time_zone": target_zone,
            },
            "output_dss": str(
                Path(_nonempty_string(payload["output_dss"], "output_dss")).resolve()
            ),
            "sentinel_threshold": float(threshold),
            "series": normalized_series,
        }

    @staticmethod
    def _verify_source_identities(
        request: Mapping[str, Any],
        *,
        drift: bool = False,
    ) -> None:
        identities: dict[Path, str] = {}
        for series in request["series"]:
            source = Path(series["source_dss"])
            expected_sha256 = series["source_sha256"]
            prior = identities.setdefault(source, expected_sha256)
            if prior != expected_sha256:
                raise HmsTimeSeriesWorkerError(
                    f"Conflicting identities were supplied for source DSS: {source}",
                    classification="source_identity_mismatch",
                    exit_code=3,
                )
        for source, expected_sha256 in identities.items():
            if not source.is_file() or _sha256(source) != expected_sha256:
                classification = (
                    "source_identity_drift" if drift else "source_identity_mismatch"
                )
                action = "changed during DSS processing" if drift else "does not match"
                raise HmsTimeSeriesWorkerError(
                    f"Source DSS {action}: {source}",
                    classification=classification,
                    exit_code=3,
                )

    @staticmethod
    def _run_dss_subprocess(
        request: Mapping[str, Any],
        output: Path,
        *,
        request_sha256: str,
    ) -> list[dict[str, Any]]:
        """Run Java/DSS work in a child so native file handles are released."""
        child_request = output.with_name(
            f".{output.stem}.{request_sha256[:12]}.child-request.json"
        )
        child_evidence = output.with_name(
            f".{output.stem}.{request_sha256[:12]}.child-evidence.json"
        )
        if child_request.exists() or child_evidence.exists():
            raise HmsTimeSeriesWorkerError(
                "Refusing unexpected DSS child control artifacts beside output",
                classification="existing_partial_output",
                exit_code=3,
            )
        try:
            _write_json_new(child_request, request)
            command = [
                sys.executable,
                "-c",
                (
                    "from hms_commander.HmsTimeSeriesWorker import "
                    "_dss_child_main; raise SystemExit(_dss_child_main())"
                ),
                "--request",
                str(child_request),
                "--output",
                str(output),
                "--evidence",
                str(child_evidence),
            ]
            try:
                completed = subprocess.run(
                    command,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError("DSS child exceeded 300 seconds") from exc
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout).strip()
                if len(detail) > 2000:
                    detail = detail[-2000:]
                raise RuntimeError(
                    "DSS child process failed with exit code "
                    f"{completed.returncode}: {detail}"
                )
            if not child_evidence.is_file():
                raise RuntimeError("DSS child did not publish evidence")
            evidence = json.loads(child_evidence.read_text(encoding="utf-8"))
            if not isinstance(evidence, dict):
                raise RuntimeError("DSS child evidence root is invalid")
            records = evidence.get("series")
            if (
                evidence.get("status") != "succeeded"
                or not isinstance(records, list)
                or len(records) != len(request["series"])
            ):
                raise RuntimeError("DSS child evidence is incomplete")
            expected_ids = [item["series_id"] for item in request["series"]]
            if [item.get("series_id") for item in records] != expected_ids:
                raise RuntimeError("DSS child evidence series identity is invalid")
            return records
        finally:
            child_request.unlink(missing_ok=True)
            child_evidence.unlink(missing_ok=True)

    @staticmethod
    def _run_dss_in_process(
        request: Mapping[str, Any],
        output: Path,
    ) -> list[dict[str, Any]]:
        """Execute DSS operations inside the dedicated child process."""
        prepared = HmsTimeSeriesWorker._prepare_series(request)
        for item in prepared:
            DssCore.write_timeseries(
                output,
                item["output_pathname"],
                item["target_index"],
                item["values"],
                units=item["units"],
                data_type=item["data_type"],
                interval_minutes=item["interval_minutes"],
            )
        HmsTimeSeriesWorker._verify_written_series(output, prepared)
        if not output.is_file():
            raise RuntimeError(f"DSS bridge did not create output: {output}")
        return _prepared_records(prepared)

    @staticmethod
    def _prepare_series(request: Mapping[str, Any]) -> list[dict[str, Any]]:
        source_start = datetime.fromisoformat(request["source_window"]["start"])
        target_start = datetime.fromisoformat(request["target_window"]["start"])
        target_end = datetime.fromisoformat(request["target_window"]["end"])
        duration = target_end - target_start
        source_end = source_start + duration
        time_zone = request["source_window"]["time_zone"]
        threshold = request["sentinel_threshold"]

        prepared = []
        for series in request["series"]:
            interval = series["interval_minutes"]
            source_index = pd.date_range(
                source_start, source_end, freq=f"{interval}min"
            )
            target_index = pd.date_range(
                target_start, target_end, freq=f"{interval}min"
            )
            _validate_wall_clock_index(source_index, time_zone, "source window")
            _validate_wall_clock_index(target_index, time_zone, "target window")

            frame = DssCore.read_timeseries(
                series["source_dss"], series["source_pathname"]
            )
            selected = _select_exact_window(frame, source_index)
            values = _validated_values(
                selected,
                interval_minutes=interval,
                units=series["units"],
                data_type=series["data_type"],
                sentinel_threshold=threshold,
                label=series["series_id"],
            )
            prepared.append(
                {
                    **series,
                    "source_index": source_index,
                    "target_index": target_index,
                    "values": values,
                    "ordinate_sha256": _ordinate_sha256(values),
                    "source_timestamp_sha256": _timestamp_sha256(source_index),
                    "target_timestamp_sha256": _timestamp_sha256(target_index),
                }
            )

        return prepared

    @staticmethod
    def _verify_written_series(
        output: Path,
        prepared: Sequence[Mapping[str, Any]],
    ) -> None:
        for item in prepared:
            frame = DssCore.read_timeseries(output, item["output_pathname"])
            selected = _select_exact_window(frame, item["target_index"])
            values = _validated_values(
                selected,
                interval_minutes=item["interval_minutes"],
                units=item["units"],
                data_type=item["data_type"],
                sentinel_threshold=-float("inf"),
                label=item["series_id"],
            )
            if _ordinate_sha256(values) != item["ordinate_sha256"]:
                raise RuntimeError(
                    f"Output ordinates changed for series {item['series_id']}"
                )
            if _timestamp_sha256(selected.index) != item["target_timestamp_sha256"]:
                raise RuntimeError(
                    f"Output timestamps changed for series {item['series_id']}"
                )

    @staticmethod
    def _success_result(
        request: Mapping[str, Any],
        *,
        request_sha256: str,
        output: Path,
        records: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        source_start = datetime.fromisoformat(request["source_window"]["start"])
        target_start = datetime.fromisoformat(request["target_window"]["start"])
        duration = (
            datetime.fromisoformat(request["target_window"]["end"]) - target_start
        )
        source_end = source_start + duration
        source_by_id = {
            item["series_id"]: _file_identity(Path(item["source_dss"]))
            for item in request["series"]
        }
        published_records = [
            {**record, "source_dss": source_by_id[record["series_id"]]}
            for record in records
        ]
        return {
            "schema": HmsTimeSeriesWorker.RESULT_SCHEMA,
            "status": "succeeded",
            "operation_id": request["operation_id"],
            "request_sha256": request_sha256,
            "qualification": request["qualification"],
            "reuse_disposition": "created; verified identical reuse permitted",
            "source_window": {
                **request["source_window"],
                "end": source_end.isoformat(timespec="seconds"),
            },
            "target_window": request["target_window"],
            "translation_offset_seconds": int(
                (target_start - source_start).total_seconds()
            ),
            "output_dss": _file_identity(output),
            "series": published_records,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    @staticmethod
    def _verify_completed_result(
        result_path: Path,
        *,
        request: Mapping[str, Any],
        request_sha256: str,
    ) -> None:
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise HmsTimeSeriesWorkerError(
                f"Existing result is unreadable: {result_path}",
                classification="existing_result_conflict",
                exit_code=3,
            ) from exc
        if (
            not isinstance(result, dict)
            or result.get("schema") != HmsTimeSeriesWorker.RESULT_SCHEMA
            or result.get("status") != "succeeded"
            or result.get("request_sha256") != request_sha256
        ):
            raise HmsTimeSeriesWorkerError(
                f"Existing result does not match this request: {result_path}",
                classification="existing_result_conflict",
                exit_code=3,
            )
        output_identity = result.get("output_dss", {})
        output = Path(request["output_dss"])
        if (
            output_identity.get("path") != str(output)
            or not output.is_file()
            or _sha256(output) != output_identity.get("sha256")
        ):
            raise HmsTimeSeriesWorkerError(
                f"Existing output identity does not match: {output}",
                classification="existing_output_drift",
                exit_code=3,
            )
        for item in request["series"]:
            source = Path(item["source_dss"])
            if not source.is_file() or _sha256(source) != item["source_sha256"]:
                raise HmsTimeSeriesWorkerError(
                    f"Source DSS identity no longer matches: {source}",
                    classification="source_identity_drift",
                    exit_code=3,
                )

    @staticmethod
    def _write_failure_if_new(
        result_path: Path,
        *,
        request: Optional[Mapping[str, Any]],
        request_sha256: Optional[str],
        error: HmsTimeSeriesWorkerError,
        error_type: Optional[str] = None,
    ) -> None:
        if result_path.exists():
            return
        try:
            _write_json_new(
                result_path,
                {
                    "schema": HmsTimeSeriesWorker.RESULT_SCHEMA,
                    "status": "failed",
                    "operation_id": request.get("operation_id") if request else None,
                    "request_sha256": request_sha256,
                    "classification": error.classification,
                    "error": {
                        "type": error_type or type(error).__name__,
                        "message": str(error),
                    },
                    "created_at": datetime.now(timezone.utc).isoformat(
                        timespec="seconds"
                    ),
                },
            )
        except Exception:
            logger.exception("Could not write time-series worker failure result")


def _prepared_records(
    prepared: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    records = []
    for item in prepared:
        values = item["values"]
        records.append(
            {
                "series_id": item["series_id"],
                "source_pathname": item["source_pathname"],
                "output_pathname": item["output_pathname"],
                "interval_minutes": item["interval_minutes"],
                "units": item["units"],
                "data_type": item["data_type"],
                "record_count": len(values),
                "ordinate_sha256": item["ordinate_sha256"],
                "source_timestamp_sha256": item["source_timestamp_sha256"],
                "target_timestamp_sha256": item["target_timestamp_sha256"],
                "minimum": float(values.min()),
                "maximum": float(values.max()),
                "final": float(values[-1]),
                "final_is_maximum": bool(values[-1] == values.max()),
            }
        )
    return records


def _select_exact_window(
    frame: pd.DataFrame,
    expected_index: pd.DatetimeIndex,
) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame) or not isinstance(
        frame.index, pd.DatetimeIndex
    ):
        raise ValueError("DSS reader must return a DataFrame with DatetimeIndex")
    if frame.index.tz is not None:
        raise ValueError("DSS reader returned timezone-aware timestamps")
    if frame.index.has_duplicates:
        raise ValueError("DSS series contains duplicate timestamps")
    selected = frame.loc[
        (frame.index >= expected_index[0]) & (frame.index <= expected_index[-1])
    ].copy()
    if not selected.index.equals(expected_index):
        missing = expected_index.difference(selected.index)
        unexpected = selected.index.difference(expected_index)
        raise ValueError(
            "DSS series does not exactly cover the requested window: "
            f"expected={len(expected_index)}, actual={len(selected)}, "
            f"missing={len(missing)}, unexpected={len(unexpected)}"
        )
    return selected


def _validated_values(
    frame: pd.DataFrame,
    *,
    interval_minutes: int,
    units: str,
    data_type: str,
    sentinel_threshold: float,
    label: str,
) -> np.ndarray:
    if "value" not in frame.columns:
        raise ValueError(f"DSS series {label} has no value column")
    values = np.asarray(frame["value"], dtype=np.float64)
    if not np.isfinite(values).all():
        raise ValueError(f"DSS series {label} contains non-finite values")
    if np.any(values <= sentinel_threshold):
        raise ValueError(
            f"DSS series {label} contains values at or below the sentinel threshold"
        )
    actual_interval = frame.attrs.get("interval")
    if actual_interval is None or int(actual_interval) != interval_minutes:
        raise ValueError(
            f"DSS series {label} interval does not match {interval_minutes} minutes"
        )
    if str(frame.attrs.get("units", "")).strip().upper() != units:
        raise ValueError(f"DSS series {label} units do not match {units}")
    if str(frame.attrs.get("type", "")).strip().upper() != data_type:
        raise ValueError(f"DSS series {label} data type does not match {data_type}")
    return values


def _validate_wall_clock_index(
    index: pd.DatetimeIndex,
    time_zone: str,
    label: str,
) -> None:
    zone = ZoneInfo(time_zone)
    offsets = set()
    utc = timezone.utc
    for timestamp in index:
        value = timestamp.to_pydatetime()
        first = value.replace(tzinfo=zone, fold=0)
        second = value.replace(tzinfo=zone, fold=1)
        valid_first = (
            first.astimezone(utc).astimezone(zone).replace(tzinfo=None) == value
        )
        valid_second = (
            second.astimezone(utc).astimezone(zone).replace(tzinfo=None) == value
        )
        if not valid_first and not valid_second:
            raise ValueError(f"{label} contains nonexistent local time {value}")
        if valid_first and valid_second and first.utcoffset() != second.utcoffset():
            raise ValueError(f"{label} contains ambiguous local time {value}")
        offsets.add(first.utcoffset())
    if len(offsets) != 1:
        raise ValueError(f"{label} crosses a time-zone offset transition")


def _model_time(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise _invalid(f"{label} must be an ISO local timestamp string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise _invalid(f"{label} is not a valid ISO timestamp") from exc
    if parsed.tzinfo is not None:
        raise _invalid(f"{label} must be timezone-naive local model time")
    if parsed.second or parsed.microsecond:
        raise _invalid(f"{label} must be aligned to a whole minute")
    return parsed


def _pathname(value: Any, label: str) -> str:
    pathname = _nonempty_string(value, label)
    parts = (
        pathname[1:-1].split("/")
        if pathname.startswith("/") and pathname.endswith("/")
        else []
    )
    if len(parts) != 6:
        raise _invalid(f"{label} must contain six DSS parts")
    return pathname


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise _invalid(f"{label} must be a positive integer")
    return value


def _object(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise _invalid(f"{label} must be an object")
    return value


def _require_keys(
    value: Mapping[str, Any],
    *,
    required: set[str],
    label: str,
) -> None:
    missing = sorted(required - set(value))
    unknown = sorted(set(value) - required)
    if missing or unknown:
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unknown:
            details.append("unknown " + ", ".join(unknown))
        raise _invalid(f"Invalid {label} fields: " + "; ".join(details))


def _nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _invalid(f"{label} must be a non-empty string")
    return value.strip()


def _sha256_string(value: Any, label: str) -> str:
    normalized = _nonempty_string(value, label)
    if not _SHA256_PATTERN.fullmatch(normalized):
        raise _invalid(f"{label} must be a lowercase SHA-256 digest")
    return normalized


def _invalid(message: str) -> HmsTimeSeriesWorkerError:
    return HmsTimeSeriesWorkerError(
        message,
        classification="invalid_request",
        exit_code=2,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_sha256(value: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _ordinate_sha256(values: np.ndarray) -> str:
    canonical = np.asarray(values, dtype="<f8").tobytes(order="C")
    return hashlib.sha256(canonical).hexdigest()


def _timestamp_sha256(index: pd.DatetimeIndex) -> str:
    canonical = "\n".join(
        timestamp.isoformat(timespec="seconds") for timestamp in index
    ).encode("ascii")
    return hashlib.sha256(canonical).hexdigest()


def _file_identity(path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    return {
        "path": str(resolved),
        "sha256": _sha256(resolved),
        "bytes": resolved.stat().st_size,
    }


def _write_json_new(path: Path, payload: Mapping[str, Any]) -> None:
    if path.exists():
        raise FileExistsError(f"Refusing to overwrite worker result: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    if temporary.exists():
        raise FileExistsError(f"Refusing existing temporary result: {temporary}")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _dss_child_main(argv: Optional[list[str]] = None) -> int:
    """Execute the private Java/DSS child operation."""
    parser = argparse.ArgumentParser(description="HMS DSS child operation")
    parser.add_argument("--request", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--evidence", required=True)
    arguments = parser.parse_args(argv)
    request = HmsTimeSeriesWorker._validate_request(
        HmsTimeSeriesWorker._load_request(Path(arguments.request).resolve())
    )
    output = Path(arguments.output).resolve()
    evidence = Path(arguments.evidence).resolve()
    if output.exists():
        raise FileExistsError(f"Refusing existing DSS child output: {output}")
    records = HmsTimeSeriesWorker._run_dss_in_process(request, output)
    _write_json_new(
        evidence,
        {
            "schema": "hms-commander/timeseries-window-dss-child/1.0",
            "status": "succeeded",
            "series": records,
        },
    )
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    """Run the time-series window worker from the command line."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True)
    parser.add_argument("--result", required=True)
    arguments = parser.parse_args(argv)
    return HmsTimeSeriesWorker.run(arguments.request, arguments.result)


if __name__ == "__main__":  # pragma: no cover - exercised by subprocess
    raise SystemExit(main())
