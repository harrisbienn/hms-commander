"""Deterministic hydrologic handoff products from an HMS output DSS."""

from __future__ import annotations

import hashlib
import json
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
            raise ValueError(
                "maximum_final_to_peak_ratio must be non-negative"
            )
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
                    "maximum_final_to_peak_ratio": (
                        maximum_final_to_peak_ratio
                    ),
                    "minimum_post_peak_hours": minimum_post_peak_hours,
                },
            }
            qualification_path = (
                stage / HmsResultsProducts.QUALIFICATION_FILENAME
            )
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
                mapping.get("pathname")
                or mapping.get("hms_dss_pathname")
                or ""
            ).strip()
            if not mapping_id:
                raise ValueError(
                    f"required_pathnames[{index}] has no mapping_id"
                )
            parts = parse_pathname(pathname)
            if not pathname.startswith("/") or not pathname.endswith("/"):
                raise ValueError(
                    f"required_pathnames[{index}] has invalid pathname: "
                    f"{pathname!r}"
                )
            if not parts["data_type"]:
                raise ValueError(
                    f"required_pathnames[{index}] has no DSS C-part: "
                    f"{pathname!r}"
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
                raise ValueError(
                    f"No aligned values and timestamps for {pathname}"
                )
            if not times.is_monotonic_increasing or times.has_duplicates:
                raise ValueError(
                    f"Time axis is not strictly increasing for {pathname}"
                )

            numeric = pd.to_numeric(values, errors="coerce").astype(float)
            sentinel = numeric <= sentinel_threshold
            missing = numeric.isna() | sentinel
            valid = numeric.mask(missing)
            interval_minutes, regular = HmsResultsProducts._interval_minutes(
                times
            )
            peak_value = float(valid.max()) if valid.notna().any() else None
            peak_time = (
                pd.Timestamp(valid.idxmax()) if valid.notna().any() else None
            )
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
            if column != "datetime"
            and pd.api.types.is_numeric_dtype(frame[column])
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
            if (
                "/PRECIP-EXCESS/" in pathname.upper()
                or "/EXCESS/" in pathname.upper()
            )
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
