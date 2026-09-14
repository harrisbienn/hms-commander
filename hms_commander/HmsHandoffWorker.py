"""Versioned worker boundary for authenticated hydrologic DSS handoffs."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Union

from .Decorators import log_call
from .HmsResultsProducts import HmsResultsProducts
from .LoggingConfig import get_logger

logger = get_logger(__name__)


class HmsHandoffWorkerError(RuntimeError):
    """Classified hydrologic handoff worker failure."""

    def __init__(self, message: str, *, classification: str, exit_code: int) -> None:
        super().__init__(message)
        self.classification = classification
        self.exit_code = exit_code


class HmsHandoffWorker:
    """Static namespace for the hydrologic handoff worker contract."""

    REQUEST_SCHEMA = "hms-commander/hydrologic-handoff-request/1.0"
    RESULT_SCHEMA = "hms-commander/hydrologic-handoff-result/1.0"

    @staticmethod
    @log_call
    def run(
        request_path: Union[str, Path],
        result_path: Union[str, Path],
    ) -> int:
        """Materialize a handoff package and atomically publish worker evidence.

        Args:
            request_path: JSON request conforming to ``REQUEST_SCHEMA``.
            result_path: New result path, or a matching completed result to
                verify for byte-identical reuse.

        Returns:
            Zero for success or verified reuse; otherwise a classified exit
            code with a failure result when it can be safely written.
        """
        request_file = Path(request_path).resolve()
        result_file = Path(result_path).resolve()
        request: Optional[dict[str, Any]] = None
        request_sha256: Optional[str] = None

        try:
            request = HmsHandoffWorker._validate_request(
                HmsHandoffWorker._load_request(request_file)
            )
            request_sha256 = _json_sha256(request)
            if result_file.exists():
                HmsHandoffWorker._verify_completed_result(
                    result_file,
                    request=request,
                    request_sha256=request_sha256,
                )
                logger.info(
                    "Verified reusable hydrologic handoff for request %s",
                    request_sha256,
                )
                return 0

            output = Path(request["output_directory"])
            if output.exists():
                raise HmsHandoffWorkerError(
                    f"Refusing unexpected existing handoff directory: {output}",
                    classification="existing_output",
                    exit_code=3,
                )
            product = HmsResultsProducts.materialize_handoff(
                request["mappings"],
                output,
                model_start=request["model_window"]["start"],
                model_end=request["model_window"]["end"],
                **request["qualification"],
            )
            result = {
                "schema": HmsHandoffWorker.RESULT_SCHEMA,
                "status": "succeeded",
                "operation_id": request["operation_id"],
                "request_sha256": request_sha256,
                "reuse_disposition": "created; verified identical reuse permitted",
                "model_window": request["model_window"],
                "directory": product["directory"],
                "assets": {
                    "dss": product["dss"],
                    "product_manifest": product["product_manifest"],
                    "provenance_manifest": product["provenance_manifest"],
                },
                "boundary_pathnames": product["boundary_pathnames"],
                "qualification": product["status"],
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            _write_json_new(result_file, result)
            return 0
        except HmsHandoffWorkerError as exc:
            HmsHandoffWorker._write_failure_if_new(
                result_file,
                request=request,
                request_sha256=request_sha256,
                error=exc,
            )
            return exc.exit_code
        except FileExistsError as exc:
            error = HmsHandoffWorkerError(
                str(exc),
                classification="existing_output",
                exit_code=3,
            )
            HmsHandoffWorker._write_failure_if_new(
                result_file,
                request=request,
                request_sha256=request_sha256,
                error=error,
                error_type=type(exc).__name__,
            )
            return error.exit_code
        except Exception as exc:
            error = HmsHandoffWorkerError(
                str(exc),
                classification="materialization_failed",
                exit_code=4,
            )
            HmsHandoffWorker._write_failure_if_new(
                result_file,
                request=request,
                request_sha256=request_sha256,
                error=error,
                error_type=type(exc).__name__,
            )
            return error.exit_code

    @staticmethod
    def _load_request(path: Path) -> dict[str, Any]:
        if not path.is_file():
            raise _invalid(f"Hydrologic handoff request does not exist: {path}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise _invalid(
                f"Could not read hydrologic handoff request: {path}"
            ) from exc
        if not isinstance(payload, dict):
            raise _invalid("Hydrologic handoff request root must be an object")
        return payload

    @staticmethod
    def _validate_request(payload: Mapping[str, Any]) -> dict[str, Any]:
        _require_keys(
            payload,
            required={
                "schema",
                "operation_id",
                "model_window",
                "output_directory",
                "qualification",
                "mappings",
            },
            label="request",
        )
        if payload["schema"] != HmsHandoffWorker.REQUEST_SCHEMA:
            raise _invalid(
                f"Unsupported hydrologic handoff schema: {payload['schema']!r}"
            )
        operation_id = _nonempty_string(payload["operation_id"], "operation_id")
        model_window = _object(payload["model_window"], "model_window")
        _require_keys(
            model_window,
            required={"start", "end"},
            label="model_window",
        )
        try:
            start = HmsResultsProducts._model_time(
                model_window["start"], "model_window.start"
            )
            end = HmsResultsProducts._model_time(
                model_window["end"], "model_window.end"
            )
        except ValueError as exc:
            raise _invalid(str(exc)) from exc
        if end <= start:
            raise _invalid("model_window.end must be later than model_window.start")

        qualification = _object(payload["qualification"], "qualification")
        _require_keys(
            qualification,
            required={
                "sentinel_threshold",
                "maximum_final_to_peak_ratio",
                "minimum_post_peak_hours",
            },
            label="qualification",
        )
        sentinel = _finite_number(
            qualification["sentinel_threshold"],
            "qualification.sentinel_threshold",
        )
        final_ratio = _finite_number(
            qualification["maximum_final_to_peak_ratio"],
            "qualification.maximum_final_to_peak_ratio",
        )
        post_peak = _finite_number(
            qualification["minimum_post_peak_hours"],
            "qualification.minimum_post_peak_hours",
        )
        if sentinel >= 0:
            raise _invalid("qualification.sentinel_threshold must be negative")
        if final_ratio < 0 or post_peak < 0:
            raise _invalid("qualification recession values must be non-negative")

        mappings = payload["mappings"]
        if not isinstance(mappings, list):
            raise _invalid("mappings must be a non-empty array")
        try:
            normalized = HmsResultsProducts._normalize_handoff_mappings(mappings)
        except (TypeError, ValueError) as exc:
            raise _invalid(str(exc)) from exc
        portable_mappings = [
            {**mapping, "source_dss": str(mapping["source_dss"])}
            for mapping in normalized
        ]
        return {
            "schema": HmsHandoffWorker.REQUEST_SCHEMA,
            "operation_id": operation_id,
            "model_window": {
                "start": start.isoformat(),
                "end": end.isoformat(),
            },
            "output_directory": str(
                Path(
                    _nonempty_string(payload["output_directory"], "output_directory")
                ).resolve()
            ),
            "qualification": {
                "sentinel_threshold": sentinel,
                "maximum_final_to_peak_ratio": final_ratio,
                "minimum_post_peak_hours": post_peak,
            },
            "mappings": portable_mappings,
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
            raise HmsHandoffWorkerError(
                f"Existing result is unreadable: {result_path}",
                classification="existing_result_conflict",
                exit_code=3,
            ) from exc
        expected_result_fields = {
            "schema",
            "status",
            "operation_id",
            "request_sha256",
            "reuse_disposition",
            "model_window",
            "directory",
            "assets",
            "boundary_pathnames",
            "qualification",
            "created_at",
        }
        if (
            not isinstance(result, dict)
            or set(result) != expected_result_fields
            or result.get("schema") != HmsHandoffWorker.RESULT_SCHEMA
            or result.get("status") != "succeeded"
            or result.get("request_sha256") != request_sha256
            or result.get("operation_id") != request["operation_id"]
            or result.get("model_window") != request["model_window"]
            or result.get("directory") != request["output_directory"]
        ):
            raise HmsHandoffWorkerError(
                f"Existing result does not match this request: {result_path}",
                classification="existing_result_conflict",
                exit_code=3,
            )
        expected_boundaries = {
            item["mapping_id"]: item["output_pathname"] for item in request["mappings"]
        }
        if result.get("boundary_pathnames") != expected_boundaries:
            raise HmsHandoffWorkerError(
                "Existing handoff boundary index changed",
                classification="existing_result_conflict",
                exit_code=3,
            )
        assets = result.get("assets")
        if not isinstance(assets, dict) or set(assets) != {
            "dss",
            "product_manifest",
            "provenance_manifest",
        }:
            raise HmsHandoffWorkerError(
                "Existing handoff result has no asset identities",
                classification="existing_result_conflict",
                exit_code=3,
            )
        output = Path(request["output_directory"])
        expected_assets = {
            "dss": output / HmsResultsProducts.HANDOFF_DSS_FILENAME,
            "product_manifest": (
                output / "products" / HmsResultsProducts.MANIFEST_FILENAME
            ),
            "provenance_manifest": (
                output / HmsResultsProducts.HANDOFF_PROVENANCE_FILENAME
            ),
        }
        for asset_id, expected_path in expected_assets.items():
            _verify_identity(assets.get(asset_id), asset_id, expected_path)
        dss_hash = assets["dss"]["sha256"]
        try:
            product = json.loads(
                Path(assets["product_manifest"]["path"]).read_text(encoding="utf-8")
            )
            provenance = json.loads(
                Path(assets["provenance_manifest"]["path"]).read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise HmsHandoffWorkerError(
                "Existing handoff manifests are unreadable",
                classification="existing_output_drift",
                exit_code=3,
            ) from exc
        if (
            product.get("schema") != HmsResultsProducts.SCHEMA
            or product.get("source", {}).get("sha256") != dss_hash
            or provenance.get("schema") != HmsResultsProducts.HANDOFF_SCHEMA
            or provenance.get("output", {}).get("sha256") != dss_hash
            or provenance.get("model_window") != request["model_window"]
            or product.get("status") != result.get("qualification")
            or product.get("time") != request["model_window"]
        ):
            raise HmsHandoffWorkerError(
                "Existing handoff manifests do not authenticate the output DSS",
                classification="existing_output_drift",
                exit_code=3,
            )
        expected_provenance = [
            HmsResultsProducts._portable_handoff_mapping(mapping)
            for mapping in request["mappings"]
        ]
        if provenance.get("mappings") != expected_provenance:
            raise HmsHandoffWorkerError(
                "Existing handoff provenance does not match this request",
                classification="existing_output_drift",
                exit_code=3,
            )
        for mapping in request["mappings"]:
            source = Path(mapping["source_dss"])
            if not source.is_file() or _sha256(source) != mapping["source_sha256"]:
                raise HmsHandoffWorkerError(
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
        error: HmsHandoffWorkerError,
        error_type: Optional[str] = None,
    ) -> None:
        if result_path.exists():
            return
        try:
            _write_json_new(
                result_path,
                {
                    "schema": HmsHandoffWorker.RESULT_SCHEMA,
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
            logger.exception("Could not write hydrologic handoff failure result")


def _verify_identity(value: Any, label: str, expected_path: Path) -> None:
    if not isinstance(value, dict) or set(value) != {
        "path",
        "size_bytes",
        "sha256",
    }:
        raise HmsHandoffWorkerError(
            f"Existing {label} identity is invalid",
            classification="existing_result_conflict",
            exit_code=3,
        )
    path = Path(str(value.get("path", ""))).resolve()
    if (
        path != expected_path.resolve()
        or not path.is_file()
        or path.stat().st_size != value.get("size_bytes")
        or _sha256(path) != value.get("sha256")
    ):
        raise HmsHandoffWorkerError(
            f"Existing {label} identity changed: {path}",
            classification="existing_output_drift",
            exit_code=3,
        )


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


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _invalid(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise _invalid(f"{label} must be finite")
    return result


def _invalid(message: str) -> HmsHandoffWorkerError:
    return HmsHandoffWorkerError(
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


def main(argv: Optional[list[str]] = None) -> int:
    """Run the hydrologic handoff worker from the command line."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True)
    parser.add_argument("--result", required=True)
    arguments = parser.parse_args(argv)
    return HmsHandoffWorker.run(arguments.request, arguments.result)


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
