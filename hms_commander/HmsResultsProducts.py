"""Deterministic hydrologic handoff products from an HMS output DSS."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Union

import numpy as np
import pandas as pd

from .Decorators import log_call
from .LoggingConfig import get_logger
from .dss import DssCore
from .dss.catalog import parse_pathname

logger = get_logger(__name__)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, payload: Dict[str, Any]) -> Path:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


class HmsResultsProducts:
    """Static namespace for qualified HMS-to-RAS result products."""

    SCHEMA = "hms-commander/hydrologic-product-manifest/1.0"
    MANIFEST_FILENAME = "hydrologic-products.json"
    TABLE_FILENAME = "hydrologic-hydrographs.csv"
    QUALIFICATION_FILENAME = "hydrologic-qualification.json"
    HANDOFF_SCHEMA = "hms-commander/hydrologic-handoff-provenance/1.0"
    HANDOFF_DSS_FILENAME = "hydrologic-handoff.dss"
    HANDOFF_PROVENANCE_FILENAME = "hydrologic-handoff-provenance.json"

    @staticmethod
    @log_call
    def export(
        dss_file: Union[str, Path],
        required_pathnames: Iterable[Mapping[str, Any]],
        output_directory: Union[str, Path],
        *,
        sentinel_threshold: float = -1.0e30,
        maximum_final_to_peak_ratio: float = 1.0,
        minimum_post_peak_hours: float = 0.0,
    ) -> Dict[str, Any]:
        """Export exact hydrographs and qualification metadata.

        Args:
            dss_file: Completed HMS output DSS.
            required_pathnames: Mapping records containing unique
                ``mapping_id`` and exact ``pathname`` values. Additional
                fields are retained as target metadata.
            output_directory: New directory for the product package.
            sentinel_threshold: Values at or below this threshold are counted
                as DSS missing/sentinel values.
            maximum_final_to_peak_ratio: Largest accepted final/peak ratio for
                the recession check.
            minimum_post_peak_hours: Minimum time required after the peak.

        Returns:
            JSON-serializable hydrologic product manifest.
        """
        source = Path(dss_file).resolve()
        output = Path(output_directory).resolve()
        if not source.is_file():
            raise FileNotFoundError(f"HMS output DSS does not exist: {source}")
        if source.stat().st_size <= 0:
            raise ValueError(f"HMS output DSS is empty: {source}")
        if output.exists():
            raise FileExistsError(
                f"Hydrologic product directory already exists: {output}"
            )
        if sentinel_threshold >= 0:
            raise ValueError("sentinel_threshold must be negative")
        if maximum_final_to_peak_ratio < 0:
            raise ValueError("maximum_final_to_peak_ratio must be non-negative")
        if minimum_post_peak_hours < 0:
            raise ValueError("minimum_post_peak_hours must be non-negative")

        mappings = HmsResultsProducts._normalize_mappings(required_pathnames)
        source_stat = source.stat()
        source_size = source_stat.st_size
        source_hash, catalog, table, summaries = (
            HmsResultsProducts._read_source_with_integrity(
                source,
                mappings,
                source_stat=source_stat,
                sentinel_threshold=sentinel_threshold,
                maximum_final_to_peak_ratio=maximum_final_to_peak_ratio,
                minimum_post_peak_hours=minimum_post_peak_hours,
            )
        )
        output.parent.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory(
            prefix=f".{output.name}-",
            dir=output.parent,
        ) as stage_name:
            stage = Path(stage_name)
            table_path = stage / HmsResultsProducts.TABLE_FILENAME
            table.to_csv(
                table_path,
                index=False,
                date_format="%Y-%m-%dT%H:%M:%S",
                float_format="%.17g",
                lineterminator="\n",
            )

            excess = HmsResultsProducts._qualify_precipitation_excess(catalog)
            qualification = {
                "schema": "hms-commander/hydrologic-qualification/1.0",
                "required_pathname_count": len(mappings),
                "unique_pathname_count": len(
                    {item["pathname"].casefold() for item in mappings}
                ),
                "all_required_pathnames_read": True,
                "all_required_pathnames_valid": all(
                    item["qualified"] for item in summaries
                ),
                "all_required_pathnames_enter_recession": all(
                    item["recession_accepted"] for item in summaries
                ),
                "pathnames": summaries,
                "precipitation_excess": excess,
                "policy": {
                    "sentinel_threshold": sentinel_threshold,
                    "maximum_final_to_peak_ratio": (maximum_final_to_peak_ratio),
                    "minimum_post_peak_hours": minimum_post_peak_hours,
                },
            }
            qualification_path = stage / HmsResultsProducts.QUALIFICATION_FILENAME
            _write_json(qualification_path, qualification)

            assets = {
                "hydrologic-hydrographs": HmsResultsProducts._file_asset(
                    table_path,
                    media_type="text/csv",
                    roles=["data", "hydrograph"],
                    extra={
                        "table": {
                            "row_count": len(table),
                            "columns": [
                                {"name": "time", "type": "timestamp"},
                                {"name": "mapping_id", "type": "string"},
                                {"name": "pathname", "type": "string"},
                                {"name": "value", "type": "float64"},
                                {"name": "units", "type": "string"},
                                {"name": "data_type", "type": "string"},
                            ],
                            "time_start": table["time"].min().isoformat(),
                            "time_end": table["time"].max().isoformat(),
                        }
                    },
                ),
                "hydrologic-qualification": HmsResultsProducts._file_asset(
                    qualification_path,
                    media_type="application/json",
                    roles=["metadata", "quality"],
                ),
            }
            manifest = {
                "schema": HmsResultsProducts.SCHEMA,
                "source": {
                    "href": source.name,
                    "size_bytes": source_size,
                    "sha256": source_hash,
                    "roles": ["source", "hms-output", "hydrologic-handoff"],
                    "integrity_verification": {
                        "pre_read": ["sha256", "size", "mtime_ns", "file_id"],
                        "post_read": ["size", "mtime_ns", "file_id"],
                        "status": "unchanged",
                    },
                },
                "status": {
                    "all_required_pathnames_read": True,
                    "all_required_pathnames_valid": qualification[
                        "all_required_pathnames_valid"
                    ],
                    "all_required_pathnames_enter_recession": qualification[
                        "all_required_pathnames_enter_recession"
                    ],
                    "hydrologic_handoff": "not_evaluated",
                },
                "time": {
                    "start": table["time"].min().isoformat(),
                    "end": table["time"].max().isoformat(),
                },
                "assets": dict(sorted(assets.items())),
            }
            _write_json(stage / HmsResultsProducts.MANIFEST_FILENAME, manifest)
            stage.replace(output)

        logger.info(
            "Exported %s required HMS hydrograph mappings to %s",
            len(mappings),
            output.name,
        )
        logger.debug("Hydrologic product directory: %s", output)
        return manifest

    @staticmethod
    @log_call
    def materialize_handoff(
        mappings: Iterable[Mapping[str, Any]],
        output_directory: Union[str, Path],
        *,
        model_start: str,
        model_end: str,
        sentinel_threshold: float = -1.0e30,
        maximum_final_to_peak_ratio: float = 1.0,
        minimum_post_peak_hours: float = 0.0,
    ) -> Dict[str, Any]:
        """Create one authenticated DSS from HMS and provider hydrographs.

        Each mapping identifies one checksum-pinned source DSS record and one
        output DSS pathname. Linear mappings are applied before the output is
        qualified through :meth:`export`. Multiple mapping IDs may share an
        output pathname only when their complete source and transformation
        definitions are identical.

        Args:
            mappings: Records containing ``mapping_id``, ``source_asset_id``,
                ``source_dss``, ``source_sha256``, ``source_pathname``,
                ``output_pathname``, ``source_units``, ``target_units``,
                ``value_type``, ``interval_minutes``, ``conversion``,
                ``multiplier``, and ``offset``.
            output_directory: New immutable handoff-package directory.
            model_start: Expected first model-local timestamp without an
                offset.
            model_end: Expected final model-local timestamp without an offset.
            sentinel_threshold: Values at or below this threshold are invalid.
            maximum_final_to_peak_ratio: Largest accepted final/peak ratio.
            minimum_post_peak_hours: Minimum time required after the peak.

        Returns:
            A dictionary containing authenticated output identities and the
            mapping-ID-to-pathname index needed by a RAS worker request.

        Raises:
            FileExistsError: If the output package already exists.
            FileNotFoundError: If a source DSS is missing.
            ValueError: If an identity, mapping, series, or transformation is
                invalid.
            RuntimeError: If a source changes while it is being read or the
                materialized hydrographs fail mechanical qualification.
        """
        normalized = HmsResultsProducts._normalize_handoff_mappings(mappings)
        output = Path(output_directory).resolve()
        if output.exists():
            raise FileExistsError(
                f"Hydrologic handoff directory already exists: {output}"
            )
        start = HmsResultsProducts._model_time(model_start, "model_start")
        end = HmsResultsProducts._model_time(model_end, "model_end")
        if end <= start:
            raise ValueError("model_end must be later than model_start")

        source_identities = HmsResultsProducts._verify_handoff_sources(normalized)
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=f".{output.name}-",
            dir=output.parent,
        ) as stage_name:
            stage = Path(stage_name)
            handoff_dss = stage / HmsResultsProducts.HANDOFF_DSS_FILENAME
            HmsResultsProducts._write_handoff_subprocess(
                normalized,
                handoff_dss,
                start=start,
                end=end,
            )
            HmsResultsProducts._verify_handoff_sources_unchanged(source_identities)
            if not handoff_dss.is_file() or handoff_dss.stat().st_size <= 0:
                raise RuntimeError("DSS child did not create a non-empty handoff")
            handoff_identity = HmsResultsProducts._identity(handoff_dss)
            required_pathnames = [
                {
                    "mapping_id": mapping["mapping_id"],
                    "pathname": mapping["output_pathname"],
                }
                for mapping in normalized
            ]
            products = stage / "products"
            HmsResultsProducts._export_handoff_subprocess(
                handoff_dss,
                required_pathnames,
                products,
                sentinel_threshold=sentinel_threshold,
                maximum_final_to_peak_ratio=maximum_final_to_peak_ratio,
                minimum_post_peak_hours=minimum_post_peak_hours,
            )
            qualified_identity = HmsResultsProducts._identity(handoff_dss)
            if qualified_identity != handoff_identity:
                raise RuntimeError(
                    "Materialized hydrologic handoff changed during qualification"
                )
            manifest_path = products / HmsResultsProducts.MANIFEST_FILENAME
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    "DSS qualification child did not create a valid product manifest"
                ) from exc
            if (
                not isinstance(manifest, dict)
                or manifest.get("schema") != HmsResultsProducts.SCHEMA
                or manifest.get("source", {}).get("sha256")
                != handoff_identity["sha256"]
            ):
                raise RuntimeError(
                    "DSS qualification child created an inconsistent product manifest"
                )
            if not manifest.get("status", {}).get("all_required_pathnames_valid"):
                raise RuntimeError(
                    "Materialized hydrologic handoff failed mechanical "
                    "pathname qualification"
                )

            provenance = {
                "schema": HmsResultsProducts.HANDOFF_SCHEMA,
                "model_window": {
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                },
                "output": {
                    "href": HmsResultsProducts.HANDOFF_DSS_FILENAME,
                    "size_bytes": handoff_identity["size_bytes"],
                    "sha256": handoff_identity["sha256"],
                },
                "mappings": [
                    HmsResultsProducts._portable_handoff_mapping(mapping)
                    for mapping in normalized
                ],
            }
            provenance_path = stage / HmsResultsProducts.HANDOFF_PROVENANCE_FILENAME
            _write_json(provenance_path, provenance)
            manifest["source"].update(
                {
                    "href": "../" + HmsResultsProducts.HANDOFF_DSS_FILENAME,
                    "roles": ["derived", "hydrologic-handoff"],
                }
            )
            manifest["assets"]["hydrologic-handoff-provenance"] = (
                HmsResultsProducts._file_asset(
                    provenance_path,
                    media_type="application/json",
                    roles=["metadata", "provenance"],
                    extra={
                        "href": "../" + HmsResultsProducts.HANDOFF_PROVENANCE_FILENAME
                    },
                )
            )
            _write_json(manifest_path, manifest)
            stage.replace(output)

        final_dss = output / HmsResultsProducts.HANDOFF_DSS_FILENAME
        final_manifest = output / "products" / HmsResultsProducts.MANIFEST_FILENAME
        final_provenance = output / HmsResultsProducts.HANDOFF_PROVENANCE_FILENAME
        logger.info(
            "Materialized %s boundary mappings into %s",
            len(normalized),
            output.name,
        )
        logger.debug("Hydrologic handoff directory: %s", output)
        return {
            "directory": str(output),
            "dss": {
                **handoff_identity,
                "path": str(final_dss.resolve()),
            },
            "product_manifest": HmsResultsProducts._identity(final_manifest),
            "provenance_manifest": HmsResultsProducts._identity(final_provenance),
            "boundary_pathnames": {
                mapping["mapping_id"]: mapping["output_pathname"]
                for mapping in normalized
            },
            "status": manifest["status"],
        }

    @staticmethod
    def _read_source_with_integrity(
        source: Path,
        mappings: list[Dict[str, Any]],
        *,
        source_stat: Any,
        sentinel_threshold: float,
        maximum_final_to_peak_ratio: float,
        minimum_post_peak_hours: float,
    ) -> tuple[str, list[str], pd.DataFrame, list[Dict[str, Any]]]:
        """Read DSS products while checking source file identity and metadata.

        The Java HEC-DSS native library retains a Windows advisory lock after
        reads, preventing a second bytewise checksum in the same process.  A
        full checksum is therefore captured before the read and stable size,
        modification time, and file identity are required afterward.
        """
        source_hash = _sha256(source)
        catalog = sorted(DssCore.get_catalog(source), key=str.casefold)
        table, summaries = HmsResultsProducts._read_required_hydrographs(
            source,
            mappings,
            sentinel_threshold=sentinel_threshold,
            maximum_final_to_peak_ratio=maximum_final_to_peak_ratio,
            minimum_post_peak_hours=minimum_post_peak_hours,
        )
        source_stat_after = source.stat()
        if (
            source_stat_after.st_size != source_stat.st_size
            or source_stat_after.st_mtime_ns != source_stat.st_mtime_ns
            or source_stat_after.st_ino != source_stat.st_ino
        ):
            raise RuntimeError(
                "Source HMS output DSS changed during product extraction"
            )
        return source_hash, catalog, table, summaries

    @staticmethod
    def _normalize_mappings(
        required_pathnames: Iterable[Mapping[str, Any]],
    ) -> list[Dict[str, Any]]:
        mappings = [dict(item) for item in required_pathnames]
        if not mappings:
            raise ValueError("required_pathnames must not be empty")

        normalized = []
        for index, mapping in enumerate(mappings):
            mapping_id = str(mapping.get("mapping_id", "")).strip()
            pathname = str(
                mapping.get("pathname") or mapping.get("hms_dss_pathname") or ""
            ).strip()
            if not mapping_id:
                raise ValueError(f"required_pathnames[{index}] has no mapping_id")
            parts = parse_pathname(pathname)
            if not pathname.startswith("/") or not pathname.endswith("/"):
                raise ValueError(
                    f"required_pathnames[{index}] has invalid pathname: "
                    f"{pathname!r}"
                )
            if not parts["data_type"]:
                raise ValueError(
                    f"required_pathnames[{index}] has no DSS C-part: " f"{pathname!r}"
                )
            normalized.append(
                {
                    **mapping,
                    "mapping_id": mapping_id,
                    "pathname": pathname,
                }
            )

        identifiers = [item["mapping_id"].casefold() for item in normalized]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("mapping_id values must be unique")
        return sorted(normalized, key=lambda item: item["mapping_id"].casefold())

    @staticmethod
    def _normalize_handoff_mappings(
        mappings: Iterable[Mapping[str, Any]],
    ) -> list[Dict[str, Any]]:
        required = {
            "mapping_id",
            "source_asset_id",
            "source_dss",
            "source_sha256",
            "source_pathname",
            "output_pathname",
            "source_units",
            "target_units",
            "value_type",
            "interval_minutes",
            "conversion",
            "multiplier",
            "offset",
        }
        raw_mappings = [dict(item) for item in mappings]
        if not raw_mappings:
            raise ValueError("handoff mappings must not be empty")
        normalized = []
        for index, mapping in enumerate(raw_mappings):
            missing = sorted(required - set(mapping))
            unknown = sorted(set(mapping) - required)
            if missing or unknown:
                details = []
                if missing:
                    details.append("missing " + ", ".join(missing))
                if unknown:
                    details.append("unknown " + ", ".join(unknown))
                raise ValueError(
                    f"handoff mappings[{index}] fields: " + "; ".join(details)
                )
            mapping_id = HmsResultsProducts._required_text(
                mapping["mapping_id"], f"handoff mappings[{index}].mapping_id"
            )
            source_asset_id = HmsResultsProducts._required_text(
                mapping["source_asset_id"],
                f"handoff mappings[{index}].source_asset_id",
            )
            source_dss = HmsResultsProducts._required_text(
                mapping["source_dss"],
                f"handoff mappings[{index}].source_dss",
            )
            source_hash = HmsResultsProducts._required_sha256(
                mapping["source_sha256"],
                f"handoff mappings[{index}].source_sha256",
            )
            source_pathname = HmsResultsProducts._required_pathname(
                mapping["source_pathname"],
                f"handoff mappings[{index}].source_pathname",
            )
            output_pathname = HmsResultsProducts._required_pathname(
                mapping["output_pathname"],
                f"handoff mappings[{index}].output_pathname",
            )
            source_units = HmsResultsProducts._required_text(
                mapping["source_units"],
                f"handoff mappings[{index}].source_units",
            )
            target_units = HmsResultsProducts._required_text(
                mapping["target_units"],
                f"handoff mappings[{index}].target_units",
            )
            value_type = HmsResultsProducts._required_text(
                mapping["value_type"],
                f"handoff mappings[{index}].value_type",
            )
            interval = mapping["interval_minutes"]
            if (
                isinstance(interval, bool)
                or not isinstance(interval, int)
                or interval <= 0
            ):
                raise ValueError(
                    f"handoff mappings[{index}].interval_minutes must be a "
                    "positive integer"
                )
            conversion = mapping["conversion"]
            if conversion not in {"identity", "linear"}:
                raise ValueError(
                    f"handoff mappings[{index}].conversion must be identity "
                    "or linear"
                )
            multiplier = HmsResultsProducts._finite_number(
                mapping["multiplier"],
                f"handoff mappings[{index}].multiplier",
            )
            offset = HmsResultsProducts._finite_number(
                mapping["offset"], f"handoff mappings[{index}].offset"
            )
            if multiplier == 0:
                raise ValueError(
                    f"handoff mappings[{index}].multiplier must not be zero"
                )
            if conversion == "identity" and (
                multiplier != 1.0
                or offset != 0.0
                or source_units.casefold() != target_units.casefold()
            ):
                raise ValueError(
                    f"handoff mappings[{index}] identity conversion must "
                    "preserve values and units"
                )
            normalized.append(
                {
                    "mapping_id": mapping_id,
                    "source_asset_id": source_asset_id,
                    "source_dss": Path(source_dss).resolve(),
                    "source_sha256": source_hash,
                    "source_pathname": source_pathname,
                    "output_pathname": output_pathname,
                    "source_units": source_units,
                    "target_units": target_units,
                    "value_type": value_type,
                    "interval_minutes": interval,
                    "conversion": conversion,
                    "multiplier": multiplier,
                    "offset": offset,
                }
            )
        identifiers = [item["mapping_id"].casefold() for item in normalized]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("handoff mapping_id values must be unique")
        normalized.sort(key=lambda item: item["mapping_id"].casefold())
        output_signatures: Dict[str, tuple[Any, ...]] = {}
        for mapping in normalized:
            output_key = mapping["output_pathname"].casefold()
            signature = HmsResultsProducts._materialization_signature(mapping)
            existing = output_signatures.setdefault(output_key, signature)
            if existing != signature:
                raise ValueError(
                    "output pathname collision has different source or "
                    f"transformation: {mapping['output_pathname']!r}"
                )
        return normalized

    @staticmethod
    def _verify_handoff_sources(
        mappings: list[Dict[str, Any]],
    ) -> Dict[Path, Dict[str, Any]]:
        identities: Dict[Path, Dict[str, Any]] = {}
        for mapping in mappings:
            source = mapping["source_dss"]
            if not source.is_file():
                raise FileNotFoundError(f"Handoff source DSS does not exist: {source}")
            if source.stat().st_size <= 0:
                raise ValueError(f"Handoff source DSS is empty: {source}")
            existing = identities.get(source)
            if existing is not None:
                if existing["sha256"] != mapping["source_sha256"]:
                    raise ValueError(
                        f"Handoff source DSS has conflicting checksums: {source}"
                    )
                continue
            actual_hash = _sha256(source)
            if actual_hash != mapping["source_sha256"]:
                raise ValueError(
                    f"Handoff source DSS checksum does not match: {source}"
                )
            stat = source.stat()
            identities[source] = {
                "sha256": actual_hash,
                "size_bytes": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "file_id": stat.st_ino,
            }
        return identities

    @staticmethod
    def _verify_handoff_sources_unchanged(
        identities: Mapping[Path, Mapping[str, Any]],
    ) -> None:
        for source, identity in identities.items():
            try:
                stat = source.stat()
            except OSError as exc:
                raise RuntimeError(
                    f"Handoff source DSS became unavailable while being read: {source}"
                ) from exc
            if (
                stat.st_size != identity["size_bytes"]
                or stat.st_mtime_ns != identity["mtime_ns"]
                or stat.st_ino != identity["file_id"]
            ):
                raise RuntimeError(
                    f"Handoff source DSS changed while being read: {source}"
                )

    @staticmethod
    def _write_handoff_subprocess(
        mappings: list[Dict[str, Any]],
        output: Path,
        *,
        start: pd.Timestamp,
        end: pd.Timestamp,
    ) -> None:
        """Write the DSS in a child so native handles close before hashing."""
        request_path = output.with_suffix(".child-request.json")
        payload = {
            "mappings": [
                {
                    **mapping,
                    "source_dss": str(mapping["source_dss"]),
                }
                for mapping in mappings
            ],
            "output": str(output),
            "model_start": start.isoformat(),
            "model_end": end.isoformat(),
        }
        try:
            _write_json(request_path, payload)
            command = [
                sys.executable,
                "-c",
                (
                    "from hms_commander.HmsResultsProducts import "
                    "_handoff_child_main; raise SystemExit(_handoff_child_main())"
                ),
                "--request",
                str(request_path),
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
                raise RuntimeError("DSS handoff child exceeded 300 seconds") from exc
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout).strip()
                if len(detail) > 2000:
                    detail = detail[-2000:]
                raise RuntimeError(
                    "DSS handoff child failed with exit code "
                    f"{completed.returncode}: {detail}"
                )
        finally:
            request_path.unlink(missing_ok=True)

    @staticmethod
    def _export_handoff_subprocess(
        source: Path,
        required_pathnames: list[Dict[str, Any]],
        output: Path,
        *,
        sentinel_threshold: float,
        maximum_final_to_peak_ratio: float,
        minimum_post_peak_hours: float,
    ) -> None:
        """Qualify the DSS in a child so native handles close before publish."""
        request_path = source.with_suffix(".export-request.json")
        payload = {
            "source": str(source),
            "required_pathnames": required_pathnames,
            "output": str(output),
            "qualification": {
                "sentinel_threshold": sentinel_threshold,
                "maximum_final_to_peak_ratio": maximum_final_to_peak_ratio,
                "minimum_post_peak_hours": minimum_post_peak_hours,
            },
        }
        try:
            _write_json(request_path, payload)
            command = [
                sys.executable,
                "-c",
                (
                    "from hms_commander.HmsResultsProducts import "
                    "_handoff_export_child_main; "
                    "raise SystemExit(_handoff_export_child_main())"
                ),
                "--request",
                str(request_path),
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
                raise RuntimeError(
                    "DSS qualification child exceeded 300 seconds"
                ) from exc
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout).strip()
                if len(detail) > 2000:
                    detail = detail[-2000:]
                raise RuntimeError(
                    "DSS qualification child failed with exit code "
                    f"{completed.returncode}: {detail}"
                )
        finally:
            request_path.unlink(missing_ok=True)

    @staticmethod
    def _write_handoff_in_process(
        mappings: list[Dict[str, Any]],
        output: Path,
        *,
        start: pd.Timestamp,
        end: pd.Timestamp,
    ) -> None:
        """Execute DSS materialization inside the dedicated child process."""
        written_outputs: Dict[str, tuple[Any, ...]] = {}
        for mapping in mappings:
            output_key = mapping["output_pathname"].casefold()
            signature = HmsResultsProducts._materialization_signature(mapping)
            if output_key in written_outputs:
                if written_outputs[output_key] != signature:
                    raise ValueError(
                        "output pathname collision has different source or "
                        f"transformation: {mapping['output_pathname']!r}"
                    )
                continue

            frame = HmsResultsProducts._read_handoff_source(
                mapping,
                start=start,
                end=end,
            )
            values = (
                HmsResultsProducts._value_series(frame).to_numpy(dtype=float)
                * mapping["multiplier"]
                + mapping["offset"]
            )
            if not np.isfinite(values).all():
                raise ValueError(
                    f"mapping {mapping['mapping_id']!r} produced non-finite values"
                )
            DssCore.write_timeseries(
                output,
                mapping["output_pathname"],
                pd.DatetimeIndex(frame.index),
                values,
                units=mapping["target_units"],
                data_type=mapping["value_type"],
                interval_minutes=mapping["interval_minutes"],
            )
            written_outputs[output_key] = signature

    @staticmethod
    def _read_handoff_source(
        mapping: Mapping[str, Any],
        *,
        start: pd.Timestamp,
        end: pd.Timestamp,
    ) -> pd.DataFrame:
        frame = DssCore.read_timeseries(
            mapping["source_dss"], mapping["source_pathname"]
        )
        times = pd.DatetimeIndex(frame.index)
        values = HmsResultsProducts._value_series(frame)
        if not len(times) or len(times) != len(values):
            raise ValueError(
                f"mapping {mapping['mapping_id']!r} has no aligned source values"
            )
        if (
            times.tz is not None
            or times.has_duplicates
            or not times.is_monotonic_increasing
        ):
            raise ValueError(
                f"mapping {mapping['mapping_id']!r} source time axis is invalid"
            )
        if times[0] != start or times[-1] != end:
            raise ValueError(
                f"mapping {mapping['mapping_id']!r} does not span the model window"
            )
        interval, regular = HmsResultsProducts._interval_minutes(times)
        if not regular or interval != mapping["interval_minutes"]:
            raise ValueError(
                f"mapping {mapping['mapping_id']!r} interval does not match"
            )
        source_pathname = str(frame.attrs.get("pathname", ""))
        if source_pathname.casefold() != mapping["source_pathname"].casefold():
            raise ValueError(
                f"mapping {mapping['mapping_id']!r} source pathname does not match"
            )
        source_units = str(frame.attrs.get("units", ""))
        if source_units.casefold() != mapping["source_units"].casefold():
            raise ValueError(
                f"mapping {mapping['mapping_id']!r} source units do not match"
            )
        value_type = str(frame.attrs.get("type", ""))
        if value_type.casefold() != mapping["value_type"].casefold():
            raise ValueError(
                f"mapping {mapping['mapping_id']!r} value type does not match"
            )
        numeric = pd.to_numeric(values, errors="coerce").to_numpy(dtype=float)
        if not np.isfinite(numeric).all():
            raise ValueError(
                f"mapping {mapping['mapping_id']!r} source contains non-finite values"
            )
        result = frame.copy()
        result["value"] = numeric
        return result

    @staticmethod
    def _materialization_signature(mapping: Mapping[str, Any]) -> tuple[Any, ...]:
        return (
            mapping["source_dss"],
            mapping["source_sha256"],
            mapping["source_pathname"].casefold(),
            mapping["source_units"].casefold(),
            mapping["target_units"].casefold(),
            mapping["value_type"].casefold(),
            mapping["interval_minutes"],
            mapping["conversion"],
            mapping["multiplier"],
            mapping["offset"],
        )

    @staticmethod
    def _portable_handoff_mapping(mapping: Mapping[str, Any]) -> Dict[str, Any]:
        return {
            "mapping_id": mapping["mapping_id"],
            "source": {
                "asset_id": mapping["source_asset_id"],
                "sha256": mapping["source_sha256"],
                "pathname": mapping["source_pathname"],
                "units": mapping["source_units"],
                "value_type": mapping["value_type"],
            },
            "output": {
                "pathname": mapping["output_pathname"],
                "units": mapping["target_units"],
                "value_type": mapping["value_type"],
                "interval_minutes": mapping["interval_minutes"],
            },
            "transformation": {
                "conversion": mapping["conversion"],
                "multiplier": mapping["multiplier"],
                "offset": mapping["offset"],
            },
        }

    @staticmethod
    def _model_time(value: Any, label: str) -> pd.Timestamp:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} must be an ISO timestamp string")
        try:
            parsed = pd.Timestamp(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label} must be an ISO timestamp") from exc
        if (
            parsed.tz is not None
            or parsed.second
            or parsed.microsecond
            or parsed.nanosecond
        ):
            raise ValueError(f"{label} must be a timezone-naive whole-minute timestamp")
        return parsed

    @staticmethod
    def _required_text(value: Any, label: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} must be a non-empty string")
        return value

    @staticmethod
    def _required_sha256(value: Any, label: str) -> str:
        digest = HmsResultsProducts._required_text(value, label)
        if len(digest) != 64 or any(
            character not in "0123456789abcdef" for character in digest
        ):
            raise ValueError(f"{label} must be a lowercase SHA-256 digest")
        return digest

    @staticmethod
    def _required_pathname(value: Any, label: str) -> str:
        pathname = HmsResultsProducts._required_text(value, label)
        parts = (
            pathname[1:-1].split("/")
            if pathname.startswith("/") and pathname.endswith("/")
            else []
        )
        if len(parts) != 6:
            raise ValueError(f"{label} must contain six DSS parts")
        return pathname

    @staticmethod
    def _finite_number(value: Any, label: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{label} must be numeric")
        result = float(value)
        if not np.isfinite(result):
            raise ValueError(f"{label} must be finite")
        return result

    @staticmethod
    def _identity(path: Path) -> Dict[str, Any]:
        resolved = path.resolve()
        return {
            "path": str(resolved),
            "size_bytes": resolved.stat().st_size,
            "sha256": _sha256(resolved),
        }

    @staticmethod
    def _read_required_hydrographs(
        source: Path,
        mappings: list[Dict[str, Any]],
        *,
        sentinel_threshold: float,
        maximum_final_to_peak_ratio: float,
        minimum_post_peak_hours: float,
    ) -> tuple[pd.DataFrame, list[Dict[str, Any]]]:
        frames = []
        summaries = []
        for mapping in mappings:
            pathname = mapping["pathname"]
            frame = DssCore.read_timeseries(source, pathname)
            values = HmsResultsProducts._value_series(frame)
            times = pd.DatetimeIndex(frame.index)
            if len(values) != len(times) or not len(values):
                raise ValueError(f"No aligned values and timestamps for {pathname}")
            if not times.is_monotonic_increasing or times.has_duplicates:
                raise ValueError(f"Time axis is not strictly increasing for {pathname}")

            numeric = pd.to_numeric(values, errors="coerce").astype(float)
            sentinel = numeric <= sentinel_threshold
            missing = numeric.isna() | sentinel
            valid = numeric.mask(missing)
            interval_minutes, regular = HmsResultsProducts._interval_minutes(times)
            peak_value = float(valid.max()) if valid.notna().any() else None
            peak_time = pd.Timestamp(valid.idxmax()) if valid.notna().any() else None
            final_value = (
                float(valid.dropna().iloc[-1]) if valid.notna().any() else None
            )
            final_to_peak_ratio = (
                final_value / peak_value
                if (
                    final_value is not None
                    and peak_value is not None
                    and peak_value > 0
                )
                else None
            )
            post_peak_hours = (
                (times[-1] - peak_time).total_seconds() / 3600.0
                if peak_time is not None
                else None
            )
            recession = bool(
                final_to_peak_ratio is not None
                and final_to_peak_ratio <= maximum_final_to_peak_ratio
                and post_peak_hours is not None
                and post_peak_hours >= minimum_post_peak_hours
            )
            negative_count = int((valid < 0).sum())
            missing_count = int(missing.sum())
            qualified = bool(
                missing_count == 0
                and negative_count == 0
                and regular
                and peak_value is not None
            )
            units = str(frame.attrs.get("units", ""))
            data_type = str(frame.attrs.get("type", ""))
            summaries.append(
                {
                    "mapping_id": mapping["mapping_id"],
                    "pathname": pathname,
                    "target": {
                        key: value
                        for key, value in mapping.items()
                        if key not in {"mapping_id", "pathname"}
                    },
                    "record_count": len(frame),
                    "start": times[0].isoformat(),
                    "end": times[-1].isoformat(),
                    "interval_minutes": interval_minutes,
                    "regular_interval": regular,
                    "units": units,
                    "data_type": data_type,
                    "missing_count": int(numeric.isna().sum()),
                    "sentinel_count": int(sentinel.sum()),
                    "missing_or_sentinel_count": missing_count,
                    "negative_count": negative_count,
                    "minimum_value": (
                        float(valid.min()) if valid.notna().any() else None
                    ),
                    "peak_value": peak_value,
                    "peak_time": (
                        peak_time.isoformat() if peak_time is not None else None
                    ),
                    "final_value": final_value,
                    "final_to_peak_ratio": final_to_peak_ratio,
                    "post_peak_hours": post_peak_hours,
                    "recession_accepted": recession,
                    "qualified": qualified,
                }
            )
            frames.append(
                pd.DataFrame(
                    {
                        "time": times,
                        "mapping_id": mapping["mapping_id"],
                        "pathname": pathname,
                        "value": numeric.to_numpy(),
                        "units": units,
                        "data_type": data_type,
                    }
                )
            )

        table = pd.concat(frames, ignore_index=True)
        table.sort_values(
            ["time", "mapping_id"],
            kind="stable",
            inplace=True,
        )
        table.reset_index(drop=True, inplace=True)
        return table, summaries

    @staticmethod
    def _value_series(frame: pd.DataFrame) -> pd.Series:
        if "value" in frame:
            return frame["value"]
        candidates = [
            column
            for column in frame.columns
            if column != "datetime" and pd.api.types.is_numeric_dtype(frame[column])
        ]
        if len(candidates) != 1:
            raise ValueError(
                "DSS time series must contain one unambiguous value column"
            )
        return frame[candidates[0]]

    @staticmethod
    def _interval_minutes(
        times: pd.DatetimeIndex,
    ) -> tuple[Optional[float], bool]:
        if len(times) < 2:
            return None, False
        intervals = (
            times.to_series(index=range(len(times)))
            .diff()
            .dropna()
            .dt.total_seconds()
            .to_numpy(dtype=float)
            / 60.0
        )
        regular = bool(np.allclose(intervals, intervals[0]))
        return (float(intervals[0]) if regular else None), regular

    @staticmethod
    def _qualify_precipitation_excess(
        catalog: list[str],
    ) -> Dict[str, Any]:
        paths = [
            pathname
            for pathname in catalog
            if ("/PRECIP-EXCESS/" in pathname.upper() or "/EXCESS/" in pathname.upper())
        ]
        parsed = [parse_pathname(pathname) for pathname in paths]
        return {
            "qualified": bool(paths),
            "pathname_count": len(paths),
            "pathnames": paths,
            "elements": sorted(
                {item["element_name"] for item in parsed},
                key=str.casefold,
            ),
            "intervals": sorted(
                {item["E"] for item in parsed if item["E"]},
                key=str.casefold,
            ),
            "runs": sorted(
                {item["run_name"] for item in parsed if item["run_name"]},
                key=str.casefold,
            ),
            "date_blocks": sorted(
                {item["D"] for item in parsed if item["D"]},
                key=str.casefold,
            ),
        }

    @staticmethod
    def _file_asset(
        path: Path,
        *,
        media_type: str,
        roles: list[str],
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        asset = {
            "href": path.name,
            "type": media_type,
            "roles": roles,
            "size_bytes": path.stat().st_size,
            "sha256": _sha256(path),
        }
        if extra:
            asset.update(extra)
        return asset


def _handoff_child_main(argv: Optional[list[str]] = None) -> int:
    """Run the isolated DSS writer used by ``materialize_handoff``."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--request", required=True)
    arguments = parser.parse_args(argv)
    payload = json.loads(Path(arguments.request).read_text(encoding="utf-8"))
    mappings = [
        {
            **mapping,
            "source_dss": Path(mapping["source_dss"]),
        }
        for mapping in payload["mappings"]
    ]
    HmsResultsProducts._write_handoff_in_process(
        mappings,
        Path(payload["output"]),
        start=HmsResultsProducts._model_time(payload["model_start"], "model_start"),
        end=HmsResultsProducts._model_time(payload["model_end"], "model_end"),
    )
    return 0


def _handoff_export_child_main(argv: Optional[list[str]] = None) -> int:
    """Run isolated DSS qualification used by ``materialize_handoff``."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--request", required=True)
    arguments = parser.parse_args(argv)
    payload = json.loads(Path(arguments.request).read_text(encoding="utf-8"))
    qualification = payload["qualification"]
    manifest = HmsResultsProducts.export(
        Path(payload["source"]),
        payload["required_pathnames"],
        Path(payload["output"]),
        sentinel_threshold=qualification["sentinel_threshold"],
        maximum_final_to_peak_ratio=qualification["maximum_final_to_peak_ratio"],
        minimum_post_peak_hours=qualification["minimum_post_peak_hours"],
    )
    if not manifest["status"]["all_required_pathnames_valid"]:
        raise RuntimeError(
            "Materialized hydrologic handoff failed mechanical pathname qualification"
        )
    return 0
