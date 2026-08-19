"""Versioned process boundary for one HEC-HMS scenario execution.

The worker intentionally depends only on hms-commander contracts.  A calling
orchestrator supplies a JSON request and receives an identity-bound JSON result;
it does not need to import this package into its own Python environment.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Union

from .Decorators import log_call
from .HmsPrj import HmsPrj
from .HmsResultsProducts import HmsResultsProducts
from .HmsScenario import HmsRunArtifact, HmsScenario
from .LoggingConfig import get_logger

logger = get_logger(__name__)

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class HmsScenarioWorkerError(RuntimeError):
    """Classified worker failure suitable for a machine-readable result."""

    def __init__(
        self,
        message: str,
        *,
        classification: str,
        exit_code: int,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.classification = classification
        self.exit_code = exit_code
        self.retryable = retryable


class HmsScenarioWorker:
    """Static namespace for the HMS scenario-worker request/result contract."""

    REQUEST_SCHEMA = "hms-commander/scenario-worker-request/1.0"
    RESULT_SCHEMA = "hms-commander/scenario-worker-result/1.0"

    @staticmethod
    @log_call
    def run(
        request_path: Union[str, Path],
        result_path: Union[str, Path],
    ) -> int:
        """Execute one JSON worker request and atomically write its result.

        Args:
            request_path: JSON request conforming to ``REQUEST_SCHEMA``.
            result_path: New JSON result destination. A verified successful
                result for the identical request is accepted without rerunning.

        Returns:
            Process-style exit code: zero for success or a verified repeat,
            nonzero for invalid input, conflicts, or execution failures.
        """
        request_file = Path(request_path).resolve()
        result_file = Path(result_path).resolve()
        started_at = _utc_now()
        started_clock = time.perf_counter()
        request: Optional[Dict[str, Any]] = None
        request_sha256: Optional[str] = None
        preparation: Dict[str, Any] = {"status": "not_started"}
        execution: Dict[str, Any] = {"status": "not_started"}
        output_dss: Optional[Dict[str, Any]] = None
        products: Optional[Dict[str, Any]] = None
        raw_qualification: Optional[Dict[str, Any]] = None
        timings: Dict[str, Any] = {}
        warnings: list[str] = []

        try:
            request = HmsScenarioWorker._load_request(request_file)
            request = HmsScenarioWorker._validate_request(request)
            request_sha256 = _json_sha256(request)

            if result_file.exists():
                HmsScenarioWorker._verify_completed_result(
                    result_file,
                    request_sha256=request_sha256,
                    specification_sha256=request["scenario"]["specification_sha256"],
                )
                logger.info(
                    "Verified existing HMS worker result for request %s",
                    request_sha256,
                )
                return 0

            workspace_path = Path(request["workspace"])
            product_directory = Path(request["products"]["directory"])
            if workspace_path.exists():
                raise HmsScenarioWorkerError(
                    f"HMS scenario workspace already exists: {workspace_path}",
                    classification="existing_workspace",
                    exit_code=3,
                )
            if product_directory.exists():
                raise HmsScenarioWorkerError(
                    "Hydrologic product destination already exists: "
                    f"{product_directory}",
                    classification="existing_product_destination",
                    exit_code=3,
                )

            HmsScenarioWorker._verify_input_identities(request)
            model_window = request["model_window"]
            source_model = request["source_model"]
            execution_options = request["execution"]

            preparation_started = time.perf_counter()
            preparation = {"status": "in_progress"}
            try:
                workspace = HmsScenario.prepare_workspace(
                    source_model["project"],
                    request["workspace"],
                    request["scenario"]["scenario_id"],
                    source_model["run"],
                    source_model["grid"],
                    request["forcing"]["dss"],
                    request["forcing"]["pathname"],
                    _parse_model_time(model_window["start"]),
                    _parse_model_time(model_window["end"]),
                    source_met=source_model.get("met"),
                    source_control=source_model.get("control"),
                    time_interval_minutes=model_window["interval_minutes"],
                    copy_precipitation=True,
                    include_generated_outputs=False,
                    overwrite=False,
                    hms_exe_path=execution_options.get("hms_executable"),
                )
                checks = HmsScenario.validate_workspace(workspace)
            finally:
                timings["preparation_seconds"] = _elapsed(preparation_started)
            preparation = {
                "status": "passed",
                "checks": checks,
                "workspace": workspace.to_dict(),
            }

            execution_started = time.perf_counter()
            execution = {"status": "in_progress"}
            try:
                artifact = HmsScenario.execute(
                    workspace,
                    hms_exe_path=execution_options.get("hms_executable"),
                    timeout=execution_options["timeout_seconds"],
                    max_memory=execution_options.get("max_memory"),
                )
            finally:
                timings["execution_seconds"] = _elapsed(execution_started)
            execution = artifact.to_dict()
            warnings = _hms_warning_lines(artifact.log_file)
            if artifact.status != "succeeded":
                raise HmsScenarioWorker._artifact_failure(artifact)

            output_dss = _file_identity(artifact.dss_file)
            product_started = time.perf_counter()
            try:
                try:
                    product_manifest = HmsResultsProducts.export(
                        artifact.dss_file,
                        request["products"]["required_pathnames"],
                        product_directory,
                        **request["products"]["qualification_policy"],
                    )
                except Exception as exc:
                    raise HmsScenarioWorkerError(
                        f"Hydrologic product export failed: {exc}",
                        classification="product_export_failed",
                        exit_code=4,
                        retryable=isinstance(exc, OSError),
                    ) from exc
            finally:
                timings["product_export_seconds"] = _elapsed(product_started)
            product_manifest_path = (
                product_directory / HmsResultsProducts.MANIFEST_FILENAME
            )
            qualification_path = (
                product_directory / HmsResultsProducts.QUALIFICATION_FILENAME
            )
            raw_qualification = json.loads(
                qualification_path.read_text(encoding="utf-8")
            )
            products = {
                "directory": str(product_directory),
                "manifest": _file_identity(product_manifest_path),
                "schema": product_manifest["schema"],
                "status": product_manifest["status"],
            }

            result = HmsScenarioWorker._result_payload(
                request=request,
                request_sha256=request_sha256,
                status="succeeded",
                started_at=started_at,
                started_clock=started_clock,
                preparation=preparation,
                execution=execution,
                output_dss=output_dss,
                products=products,
                raw_qualification=raw_qualification,
                timings=timings,
                warnings=warnings,
                error=None,
            )
            _write_json_new(result_file, result)
            logger.info(
                "Completed HMS scenario worker request %s",
                request_sha256,
            )
            return 0
        except Exception as exc:
            failure = HmsScenarioWorker._classify_exception(exc)
            result = HmsScenarioWorker._result_payload(
                request=request,
                request_sha256=request_sha256,
                status="failed",
                started_at=started_at,
                started_clock=started_clock,
                preparation=preparation,
                execution=execution,
                output_dss=output_dss,
                products=products,
                raw_qualification=raw_qualification,
                timings=timings,
                warnings=warnings,
                error={
                    "classification": failure.classification,
                    "exception_type": type(exc).__name__,
                    "message": str(exc),
                    "retryable": failure.retryable,
                },
            )
            try:
                _write_json_new(result_file, result)
            except FileExistsError:
                logger.error(
                    "Refusing to replace existing HMS worker result: %s",
                    result_file,
                )
            except OSError as write_error:
                logger.error(
                    "Could not write HMS worker failure result %s: %s",
                    result_file,
                    write_error,
                )
            logger.error(
                "HMS scenario worker failed (%s): %s",
                failure.classification,
                exc,
            )
            return failure.exit_code

    @staticmethod
    def _load_request(path: Path) -> Dict[str, Any]:
        if not path.is_file():
            raise HmsScenarioWorkerError(
                f"HMS worker request does not exist: {path}",
                classification="invalid_request",
                exit_code=2,
            )
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise HmsScenarioWorkerError(
                f"Could not read HMS worker request {path}: {exc}",
                classification="invalid_request",
                exit_code=2,
            ) from exc
        if not isinstance(payload, dict):
            raise HmsScenarioWorkerError(
                "HMS worker request root must be a JSON object",
                classification="invalid_request",
                exit_code=2,
            )
        return payload

    @staticmethod
    def _validate_request(payload: Mapping[str, Any]) -> Dict[str, Any]:
        required = {
            "schema",
            "scenario",
            "source_model",
            "forcing",
            "model_window",
            "workspace",
            "products",
            "execution",
        }
        missing = sorted(required - set(payload))
        unknown = sorted(set(payload) - required)
        if missing or unknown:
            details = []
            if missing:
                details.append("missing " + ", ".join(missing))
            if unknown:
                details.append("unknown " + ", ".join(unknown))
            raise HmsScenarioWorkerError(
                "Invalid HMS worker request fields: " + "; ".join(details),
                classification="invalid_request",
                exit_code=2,
            )
        if payload["schema"] != HmsScenarioWorker.REQUEST_SCHEMA:
            raise HmsScenarioWorkerError(
                f"Unsupported HMS worker request schema: {payload['schema']!r}",
                classification="invalid_request",
                exit_code=2,
            )

        scenario = _object(payload["scenario"], "scenario")
        _require_keys(
            scenario,
            required={"scenario_id", "specification_sha256"},
            optional=set(),
            label="scenario",
        )
        scenario_id = _nonempty_string(scenario["scenario_id"], "scenario.scenario_id")
        specification_sha256 = _sha256_string(
            scenario["specification_sha256"],
            "scenario.specification_sha256",
        )

        source_model = _object(payload["source_model"], "source_model")
        _require_keys(
            source_model,
            required={"project", "project_file_sha256", "run", "grid"},
            optional={"met", "control"},
            label="source_model",
        )
        normalized_source: Dict[str, Any] = {
            "project": str(
                Path(
                    _nonempty_string(source_model["project"], "source_model.project")
                ).resolve()
            ),
            "project_file_sha256": _sha256_string(
                source_model["project_file_sha256"],
                "source_model.project_file_sha256",
            ),
            "run": _nonempty_string(source_model["run"], "source_model.run"),
            "grid": _nonempty_string(source_model["grid"], "source_model.grid"),
        }
        for name in ("met", "control"):
            if source_model.get(name) is not None:
                normalized_source[name] = _nonempty_string(
                    source_model[name], f"source_model.{name}"
                )

        forcing = _object(payload["forcing"], "forcing")
        _require_keys(
            forcing,
            required={"dss", "sha256", "pathname"},
            optional=set(),
            label="forcing",
        )
        normalized_forcing = {
            "dss": str(Path(_nonempty_string(forcing["dss"], "forcing.dss")).resolve()),
            "sha256": _sha256_string(forcing["sha256"], "forcing.sha256"),
            "pathname": _nonempty_string(forcing["pathname"], "forcing.pathname"),
        }

        model_window = _object(payload["model_window"], "model_window")
        _require_keys(
            model_window,
            required={"start", "end", "time_zone", "interval_minutes"},
            optional=set(),
            label="model_window",
        )
        start = _parse_model_time(model_window["start"])
        end = _parse_model_time(model_window["end"])
        if end <= start:
            raise HmsScenarioWorkerError(
                "model_window.end must be later than model_window.start",
                classification="invalid_request",
                exit_code=2,
            )
        interval = _positive_int(
            model_window["interval_minutes"],
            "model_window.interval_minutes",
        )
        normalized_window = {
            "start": start.isoformat(timespec="seconds"),
            "end": end.isoformat(timespec="seconds"),
            "time_zone": _nonempty_string(
                model_window["time_zone"], "model_window.time_zone"
            ),
            "interval_minutes": interval,
        }

        products = _object(payload["products"], "products")
        _require_keys(
            products,
            required={"directory", "required_pathnames"},
            optional={"qualification_policy"},
            label="products",
        )
        mappings = products["required_pathnames"]
        if not isinstance(mappings, list) or not mappings:
            raise HmsScenarioWorkerError(
                "products.required_pathnames must be a non-empty array",
                classification="invalid_request",
                exit_code=2,
            )
        normalized_mappings = []
        for index, mapping_value in enumerate(mappings):
            mapping = _object(
                mapping_value,
                f"products.required_pathnames[{index}]",
            )
            if not {"mapping_id", "pathname"}.issubset(mapping):
                raise HmsScenarioWorkerError(
                    "Each products.required_pathnames entry requires mapping_id "
                    "and pathname",
                    classification="invalid_request",
                    exit_code=2,
                )
            normalized_mapping = dict(mapping)
            normalized_mapping["mapping_id"] = _nonempty_string(
                mapping["mapping_id"],
                f"products.required_pathnames[{index}].mapping_id",
            )
            normalized_mapping["pathname"] = _nonempty_string(
                mapping["pathname"],
                f"products.required_pathnames[{index}].pathname",
            )
            normalized_mappings.append(normalized_mapping)
        policy = _object(
            products.get("qualification_policy", {}),
            "products.qualification_policy",
        )
        allowed_policy = {
            "sentinel_threshold",
            "maximum_final_to_peak_ratio",
            "minimum_post_peak_hours",
        }
        if set(policy) - allowed_policy:
            raise HmsScenarioWorkerError(
                "products.qualification_policy contains unknown fields: "
                + ", ".join(sorted(set(policy) - allowed_policy)),
                classification="invalid_request",
                exit_code=2,
            )
        normalized_policy: Dict[str, float] = {}
        for name, value in policy.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise HmsScenarioWorkerError(
                    f"products.qualification_policy.{name} must be numeric",
                    classification="invalid_request",
                    exit_code=2,
                )
            normalized_policy[name] = float(value)
        if normalized_policy.get("sentinel_threshold", -1.0) >= 0:
            raise HmsScenarioWorkerError(
                "products.qualification_policy.sentinel_threshold must be negative",
                classification="invalid_request",
                exit_code=2,
            )
        for name in (
            "maximum_final_to_peak_ratio",
            "minimum_post_peak_hours",
        ):
            if normalized_policy.get(name, 0.0) < 0:
                raise HmsScenarioWorkerError(
                    f"products.qualification_policy.{name} must be non-negative",
                    classification="invalid_request",
                    exit_code=2,
                )

        execution = _object(payload["execution"], "execution")
        _require_keys(
            execution,
            required={"timeout_seconds"},
            optional={"hms_executable", "max_memory"},
            label="execution",
        )
        normalized_execution: Dict[str, Any] = {
            "timeout_seconds": _positive_int(
                execution["timeout_seconds"], "execution.timeout_seconds"
            )
        }
        if execution.get("hms_executable") is not None:
            normalized_execution["hms_executable"] = str(
                Path(
                    _nonempty_string(
                        execution["hms_executable"], "execution.hms_executable"
                    )
                ).resolve()
            )
        if execution.get("max_memory") is not None:
            normalized_execution["max_memory"] = _nonempty_string(
                execution["max_memory"], "execution.max_memory"
            )

        return {
            "schema": HmsScenarioWorker.REQUEST_SCHEMA,
            "scenario": {
                "scenario_id": scenario_id,
                "specification_sha256": specification_sha256,
            },
            "source_model": normalized_source,
            "forcing": normalized_forcing,
            "model_window": normalized_window,
            "workspace": str(
                Path(_nonempty_string(payload["workspace"], "workspace")).resolve()
            ),
            "products": {
                "directory": str(
                    Path(
                        _nonempty_string(products["directory"], "products.directory")
                    ).resolve()
                ),
                "required_pathnames": normalized_mappings,
                "qualification_policy": normalized_policy,
            },
            "execution": normalized_execution,
        }

    @staticmethod
    def _verify_input_identities(request: Mapping[str, Any]) -> None:
        forcing = Path(request["forcing"]["dss"])
        if not forcing.is_file():
            raise HmsScenarioWorkerError(
                f"Forcing DSS does not exist: {forcing}",
                classification="forcing_identity",
                exit_code=2,
            )
        forcing_sha256 = _sha256(forcing)
        if forcing_sha256 != request["forcing"]["sha256"]:
            raise HmsScenarioWorkerError(
                "Forcing DSS checksum does not match the worker request",
                classification="forcing_identity",
                exit_code=2,
            )

        project = Path(request["source_model"]["project"])
        project_folder = project.parent if project.is_file() else project
        project_file = (
            project
            if project.is_file() and project.suffix.lower() == ".hms"
            else HmsPrj.find_hms_project(project_folder)
        )
        if project_file is None:
            raise HmsScenarioWorkerError(
                f"Source model has no HMS project file: {project_folder}",
                classification="source_model_identity",
                exit_code=2,
            )
        if _sha256(project_file) != request["source_model"]["project_file_sha256"]:
            raise HmsScenarioWorkerError(
                "Source HMS project checksum does not match the worker request",
                classification="source_model_identity",
                exit_code=2,
            )
        executable = request["execution"].get("hms_executable")
        if executable is not None and not Path(executable).exists():
            raise HmsScenarioWorkerError(
                f"HEC-HMS executable reference does not exist: {executable}",
                classification="invalid_request",
                exit_code=2,
            )

    @staticmethod
    def _artifact_failure(artifact: HmsRunArtifact) -> HmsScenarioWorkerError:
        if artifact.abort_marker_found:
            classification = "aborted_run"
            message = "HEC-HMS recorded an aborted run marker"
        elif not artifact.completion_marker_found:
            classification = "missing_completion_marker"
            message = "HEC-HMS did not record the exact completion marker"
        elif not artifact.dss_exists or artifact.dss_size_bytes <= 0:
            classification = "empty_output"
            message = "HEC-HMS output DSS is missing or empty"
        else:
            classification = "execution_failed"
            message = "HEC-HMS process or log validation failed"
        return HmsScenarioWorkerError(
            message,
            classification=classification,
            exit_code=4,
            retryable=True,
        )

    @staticmethod
    def _verify_completed_result(
        result_path: Path,
        *,
        request_sha256: str,
        specification_sha256: str,
    ) -> Dict[str, Any]:
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise HmsScenarioWorkerError(
                f"Existing HMS worker result is unreadable: {result_path}",
                classification="existing_result_conflict",
                exit_code=3,
            ) from exc
        matches = (
            isinstance(result, dict)
            and result.get("schema") == HmsScenarioWorker.RESULT_SCHEMA
            and result.get("status") == "succeeded"
            and result.get("request", {}).get("sha256") == request_sha256
            and result.get("scenario", {}).get("specification_sha256")
            == specification_sha256
        )
        if not matches:
            raise HmsScenarioWorkerError(
                "Existing HMS worker result is not an identical completed request",
                classification="existing_result_conflict",
                exit_code=3,
            )
        for label, identity in (
            ("output DSS", result.get("output_dss")),
            ("product manifest", result.get("products", {}).get("manifest")),
        ):
            if not isinstance(identity, dict):
                raise HmsScenarioWorkerError(
                    f"Existing HMS worker result has no {label} identity",
                    classification="existing_result_conflict",
                    exit_code=3,
                )
            path = Path(str(identity.get("path", "")))
            if (
                not path.is_file()
                or path.stat().st_size != identity.get("size_bytes")
                or _sha256(path) != identity.get("sha256")
            ):
                raise HmsScenarioWorkerError(
                    f"Existing HMS worker {label} failed identity verification",
                    classification="existing_result_conflict",
                    exit_code=3,
                )
        return result

    @staticmethod
    def _classify_exception(exc: Exception) -> HmsScenarioWorkerError:
        if isinstance(exc, HmsScenarioWorkerError):
            return exc
        if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)):
            return HmsScenarioWorkerError(
                str(exc),
                classification="timeout",
                exit_code=4,
                retryable=True,
            )
        if isinstance(exc, FileExistsError):
            return HmsScenarioWorkerError(
                str(exc),
                classification="existing_destination",
                exit_code=3,
            )
        if isinstance(exc, (ValueError, FileNotFoundError, TypeError)):
            return HmsScenarioWorkerError(
                str(exc),
                classification="invalid_request",
                exit_code=2,
            )
        return HmsScenarioWorkerError(
            str(exc),
            classification="worker_error",
            exit_code=5,
            retryable=isinstance(exc, OSError),
        )

    @staticmethod
    def _result_payload(
        *,
        request: Optional[Mapping[str, Any]],
        request_sha256: Optional[str],
        status: str,
        started_at: str,
        started_clock: float,
        preparation: Mapping[str, Any],
        execution: Mapping[str, Any],
        output_dss: Optional[Mapping[str, Any]],
        products: Optional[Mapping[str, Any]],
        raw_qualification: Optional[Mapping[str, Any]],
        timings: Mapping[str, Any],
        warnings: Sequence[str],
        error: Optional[Mapping[str, Any]],
    ) -> Dict[str, Any]:
        scenario = request.get("scenario", {}) if request else {}
        finished_at = _utc_now()
        complete_timings = dict(timings)
        complete_timings["total_seconds"] = _elapsed(started_clock)
        return {
            "schema": HmsScenarioWorker.RESULT_SCHEMA,
            "status": status,
            "scenario": {
                "scenario_id": scenario.get("scenario_id"),
                "specification_sha256": scenario.get("specification_sha256"),
            },
            "request": {
                "schema": request.get("schema") if request else None,
                "sha256": request_sha256,
            },
            "preparation": dict(preparation),
            "execution": dict(execution),
            "output_dss": dict(output_dss) if output_dss else None,
            "products": dict(products) if products else None,
            "raw_qualification": (
                dict(raw_qualification) if raw_qualification else None
            ),
            "timings": {
                "started_at": started_at,
                "finished_at": finished_at,
                **complete_timings,
            },
            "warnings": list(warnings),
            "error": dict(error) if error else None,
        }


def _object(value: Any, label: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise HmsScenarioWorkerError(
            f"{label} must be a JSON object",
            classification="invalid_request",
            exit_code=2,
        )
    return dict(value)


def _require_keys(
    value: Mapping[str, Any],
    *,
    required: set[str],
    optional: set[str],
    label: str,
) -> None:
    missing = sorted(required - set(value))
    unknown = sorted(set(value) - required - optional)
    if missing or unknown:
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unknown:
            details.append("unknown " + ", ".join(unknown))
        raise HmsScenarioWorkerError(
            f"Invalid {label} fields: " + "; ".join(details),
            classification="invalid_request",
            exit_code=2,
        )


def _nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HmsScenarioWorkerError(
            f"{label} must be a non-empty string",
            classification="invalid_request",
            exit_code=2,
        )
    return value.strip()


def _sha256_string(value: Any, label: str) -> str:
    normalized = _nonempty_string(value, label)
    if _SHA256_PATTERN.fullmatch(normalized) is None:
        raise HmsScenarioWorkerError(
            f"{label} must be a lowercase SHA-256 hexadecimal digest",
            classification="invalid_request",
            exit_code=2,
        )
    return normalized


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise HmsScenarioWorkerError(
            f"{label} must be a positive integer",
            classification="invalid_request",
            exit_code=2,
        )
    return value


def _parse_model_time(value: Any) -> datetime:
    text = _nonempty_string(value, "model window timestamp")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise HmsScenarioWorkerError(
            f"Invalid model window timestamp: {text!r}",
            classification="invalid_request",
            exit_code=2,
        ) from exc
    if parsed.tzinfo is not None:
        raise HmsScenarioWorkerError(
            "Model window timestamps must be naive local/model times",
            classification="invalid_request",
            exit_code=2,
        )
    return parsed


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_sha256(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _file_identity(path: Union[str, Path]) -> Dict[str, Any]:
    file_path = Path(path).resolve()
    if not file_path.is_file():
        raise FileNotFoundError(f"Expected output file does not exist: {file_path}")
    return {
        "path": str(file_path),
        "size_bytes": file_path.stat().st_size,
        "sha256": _sha256(file_path),
    }


def _write_json_new(path: Path, payload: Mapping[str, Any]) -> Path:
    if path.exists():
        raise FileExistsError(f"HMS worker result already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        delete=False,
    ) as stream:
        json.dump(payload, stream, indent=2, sort_keys=True)
        stream.write("\n")
        temporary = Path(stream.name)
    try:
        if path.exists():
            raise FileExistsError(f"HMS worker result already exists: {path}")
        temporary.rename(path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def _hms_warning_lines(path: Union[str, Path]) -> list[str]:
    log_file = Path(path)
    if not log_file.is_file():
        return []
    return [
        line.strip()
        for line in log_file.read_text(
            encoding="utf-8",
            errors="replace",
        ).splitlines()
        if line.lstrip().startswith("WARNING")
    ]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _elapsed(started: float) -> float:
    return round(time.perf_counter() - started, 6)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the ``hms-scenario-worker`` command-line interface."""
    parser = argparse.ArgumentParser(
        description="Execute one versioned hms-commander scenario-worker request."
    )
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--result", required=True, type=Path)
    args = parser.parse_args(argv)
    exit_code = HmsScenarioWorker.run(args.request, args.result)
    if exit_code != 0:
        sys.stderr.write(f"HMS scenario worker failed; result: {args.result}\n")
    return exit_code


if __name__ == "__main__":  # pragma: no cover - exercised through console script
    raise SystemExit(main())
