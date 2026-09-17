"""Compile deterministic HMS-subbasin to RAS-grid transfer maps."""

from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .Decorators import log_call

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_AREA_UNIT_FACTORS = {
    "SQUARE_METERS": 1.0,
    "SQUARE_KILOMETERS": 1_000_000.0,
    "ACRES": 4_046.8564224,
    "SQUARE_MILES": 2_589_988.110336,
}
_AREA_UNIT_ALIASES = {
    "M2": "SQUARE_METERS",
    "SQ_M": "SQUARE_METERS",
    "SQUARE_METER": "SQUARE_METERS",
    "SQUARE_METERS": "SQUARE_METERS",
    "KM2": "SQUARE_KILOMETERS",
    "SQ_KM": "SQUARE_KILOMETERS",
    "SQUARE_KILOMETER": "SQUARE_KILOMETERS",
    "SQUARE_KILOMETERS": "SQUARE_KILOMETERS",
    "ACRE": "ACRES",
    "ACRES": "ACRES",
    "MI2": "SQUARE_MILES",
    "SQ_MI": "SQUARE_MILES",
    "SQUARE_MILE": "SQUARE_MILES",
    "SQUARE_MILES": "SQUARE_MILES",
}
_DEPTH_UNIT_METERS = {"IN": 0.0254, "MM": 0.001}


def _canonical_sha256(payload: Mapping[str, Any]) -> str:
    content = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _array_sha256(values: np.ndarray) -> str:
    contiguous = np.ascontiguousarray(values)
    digest = hashlib.sha256()
    digest.update(str(contiguous.dtype).encode("ascii"))
    digest.update(json.dumps(list(contiguous.shape)).encode("ascii"))
    digest.update(contiguous.tobytes(order="C"))
    return digest.hexdigest()


def _required_mapping(
    value: Any,
    required: set[str],
    *,
    label: str,
) -> None:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    missing = sorted(required - set(value))
    unexpected = sorted(set(value) - required)
    if missing or unexpected:
        details = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if unexpected:
            details.append(f"unexpected {', '.join(unexpected)}")
        raise ValueError(f"{label} has invalid fields: {'; '.join(details)}")


def _non_empty(value: Any, *, label: str) -> str:
    normalized = str(value).strip()
    if not normalized:
        raise ValueError(f"{label} must not be empty")
    return normalized


def _file_identity(value: Any, *, label: str) -> dict[str, Any]:
    _required_mapping(value, {"name", "size_bytes", "sha256"}, label=label)
    name = _non_empty(value["name"], label=f"{label}.name")
    if Path(name).name != name:
        raise ValueError(f"{label}.name must be a portable filename")
    try:
        size_bytes = int(value["size_bytes"])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}.size_bytes must be an integer") from exc
    sha256 = str(value["sha256"])
    if size_bytes <= 0:
        raise ValueError(f"{label}.size_bytes must be positive")
    if not _SHA256_PATTERN.fullmatch(sha256):
        raise ValueError(f"{label}.sha256 must be lowercase SHA-256")
    return {"name": name, "size_bytes": size_bytes, "sha256": sha256}


def _normalized_hms_model(value: Any) -> dict[str, Any]:
    _required_mapping(
        value,
        {"project_id", "basin_model_id", "basin_file", "geometry_source"},
        label="hms_model",
    )
    return {
        "project_id": _non_empty(value["project_id"], label="hms_model.project_id"),
        "basin_model_id": _non_empty(
            value["basin_model_id"],
            label="hms_model.basin_model_id",
        ),
        "basin_file": _file_identity(
            value["basin_file"],
            label="hms_model.basin_file",
        ),
        "geometry_source": _file_identity(
            value["geometry_source"],
            label="hms_model.geometry_source",
        ),
    }


def _normalized_area(value: Any, units: Any, *, label: str) -> dict[str, Any]:
    try:
        declared_value = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} area must be numeric") from exc
    if not math.isfinite(declared_value) or declared_value <= 0:
        raise ValueError(f"{label} area must be finite and positive")
    unit_key = re.sub(r"[^A-Z0-9]+", "_", str(units).strip().upper()).strip("_")
    canonical_units = _AREA_UNIT_ALIASES.get(unit_key)
    if canonical_units is None:
        supported = ", ".join(sorted(_AREA_UNIT_FACTORS))
        raise ValueError(f"{label} area units are unsupported; use one of {supported}")
    square_meters = declared_value * _AREA_UNIT_FACTORS[canonical_units]
    return {
        "declared_value": declared_value,
        "declared_units": canonical_units,
        "square_meters": square_meters,
    }


def _validated_target_grid(value: Any) -> dict[str, Any]:
    _required_mapping(
        value,
        {
            "definition_id",
            "crs",
            "shape",
            "cell_size_meters",
            "cell_area_square_meters",
            "origin",
            "row_order",
            "definition_sha256",
        },
        label="target_grid",
    )
    try:
        from pyproj import CRS

        shape = list(value["shape"])
        origin = list(value["origin"])
        rows, columns = (int(item) for item in shape)
        origin_x, origin_y = (float(item) for item in origin)
        cell_size = float(value["cell_size_meters"])
        cell_area = float(value["cell_area_square_meters"])
        crs = CRS.from_user_input(value["crs"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("target_grid is incomplete or invalid") from exc
    if shape != [rows, columns] or rows <= 0 or columns <= 0:
        raise ValueError("target_grid shape must contain two positive integers")
    if len(origin) != 2 or not all(
        math.isfinite(item) for item in (origin_x, origin_y)
    ):
        raise ValueError("target_grid origin must contain two finite coordinates")
    if not math.isfinite(cell_size) or cell_size <= 0:
        raise ValueError("target_grid cell size must be finite and positive")
    if not math.isclose(
        cell_area,
        cell_size * cell_size,
        rel_tol=1.0e-12,
        abs_tol=1.0e-9,
    ):
        raise ValueError("target_grid cell area must equal cell size squared")
    if value["row_order"] != "south_to_north":
        raise ValueError("target_grid must use south_to_north row order")
    if not crs.is_projected:
        raise ValueError("target_grid CRS must be projected")
    axis_factors = {
        float(axis.unit_conversion_factor)
        for axis in crs.axis_info
        if axis.unit_conversion_factor is not None
    }
    if axis_factors and any(
        not math.isclose(factor, 1.0, rel_tol=0.0, abs_tol=1.0e-12)
        for factor in axis_factors
    ):
        raise ValueError("target_grid CRS axes must use meters")
    unsigned = dict(value)
    recorded_hash = unsigned.pop("definition_sha256")
    if recorded_hash != _canonical_sha256(unsigned):
        raise ValueError("target_grid definition_sha256 is missing or invalid")
    return dict(value)


def _validated_application_area(value: Any) -> dict[str, Any]:
    try:
        normalized = json.loads(json.dumps(value, sort_keys=True, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError("RAS application area must be JSON serializable") from exc
    if not isinstance(normalized, dict):
        raise ValueError("RAS application area must be an object")
    if normalized.get("schema") != ("ras-commander/precipitation-application-area/1.0"):
        raise ValueError("RAS application-area schema is unsupported")
    if normalized.get("method") != "ras-mesh-effective-area":
        raise ValueError("RAS application-area method is unsupported")
    recorded_hash = normalized.pop("application_area_sha256", None)
    if not isinstance(recorded_hash, str) or recorded_hash != _canonical_sha256(
        normalized
    ):
        raise ValueError("RAS application-area hash is missing or invalid")
    normalized["application_area_sha256"] = recorded_hash

    _required_mapping(
        normalized.get("model"),
        {"project_id", "plan_id", "geometry_id", "two_d_flow_area"},
        label="RAS application-area model",
    )
    for key, model_value in normalized["model"].items():
        _non_empty(model_value, label=f"RAS application-area model.{key}")

    grid = _validated_target_grid(normalized.get("target_grid"))
    rows, columns = grid["shape"]
    cell_area = float(grid["cell_area_square_meters"])
    cell_size = float(grid["cell_size_meters"])
    origin_x, origin_y = (float(item) for item in grid["origin"])

    cells = normalized.get("cells")
    if not isinstance(cells, list) or len(cells) != rows * columns:
        raise ValueError("RAS application-area cells do not cover the target grid")
    for expected_id, cell in enumerate(cells):
        if not isinstance(cell, dict):
            raise ValueError("RAS application-area cells must be objects")
        expected_row, expected_column = divmod(expected_id, columns)
        if (
            cell.get("cell_id") != expected_id
            or cell.get("row_index") != expected_row
            or cell.get("column_index") != expected_column
        ):
            raise ValueError("RAS application-area cells are not in row-major order")
        expected_center = [
            origin_x + (expected_column + 0.5) * cell_size,
            origin_y + (expected_row + 0.5) * cell_size,
        ]
        if cell.get("center") != expected_center:
            raise ValueError("RAS application-area cell centers do not match the grid")
        try:
            effective_area = float(cell["effective_area_square_meters"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("RAS application-area cell area is invalid") from exc
        if (
            not math.isfinite(effective_area)
            or effective_area < 0
            or effective_area > cell_area
        ):
            raise ValueError("RAS application-area cell area is outside valid bounds")
        membership = cell.get("membership")
        if membership == "outside" and effective_area != 0:
            raise ValueError("outside RAS cells must have zero effective area")
        if membership == "inside" and effective_area != cell_area:
            raise ValueError("inside RAS cells must have their complete area")
        if membership == "partial" and not 0 < effective_area < cell_area:
            raise ValueError("partial RAS cells must have a partial effective area")
        if membership not in {"inside", "partial", "outside"}:
            raise ValueError("RAS application-area membership is unsupported")
    normalized["target_grid"] = grid
    return normalized


def _normalized_volume_tolerance(value: Any) -> dict[str, float]:
    _required_mapping(
        value,
        {"absolute_cubic_meters", "relative_fraction"},
        label="volume_tolerance",
    )
    try:
        absolute = float(value["absolute_cubic_meters"])
        relative = float(value["relative_fraction"])
    except (TypeError, ValueError) as exc:
        raise ValueError("volume_tolerance values must be numeric") from exc
    if (
        not math.isfinite(absolute)
        or absolute < 0
        or not math.isfinite(relative)
        or relative < 0
    ):
        raise ValueError("volume_tolerance values must be finite and nonnegative")
    return {
        "absolute_cubic_meters": absolute,
        "relative_fraction": relative,
    }


def _residual_record(
    source: float,
    target: float,
    tolerance: Mapping[str, float],
    *,
    label: str,
) -> dict[str, Any]:
    if not math.isfinite(source) or not math.isfinite(target):
        raise ValueError(f"Volume evidence is non-finite for {label}")
    if source < 0 or target < 0:
        raise ValueError(f"Volume evidence is negative for {label}")
    residual = target - source
    absolute_residual = abs(residual)
    limit = max(
        tolerance["absolute_cubic_meters"],
        abs(source) * tolerance["relative_fraction"],
    )
    if absolute_residual > limit:
        raise ValueError(
            f"Volume residual exceeds tolerance for {label}: "
            f"{absolute_residual} > {limit} cubic meters"
        )
    relative = (
        None
        if source == 0 and absolute_residual
        else (0.0 if source == 0 else absolute_residual / abs(source))
    )
    return {
        "source_cubic_meters": float(source),
        "target_cubic_meters": float(target),
        "residual_cubic_meters": float(residual),
        "absolute_residual_cubic_meters": float(absolute_residual),
        "relative_residual_fraction": relative,
        "allowed_residual_cubic_meters": float(limit),
    }


def _volume_evidence(
    source_by_subbasin: Mapping[str, np.ndarray],
    target_by_subbasin: Mapping[str, np.ndarray],
    interval_ends: pd.DatetimeIndex,
    tolerance: Mapping[str, float],
) -> dict[str, Any]:
    if set(source_by_subbasin) != set(target_by_subbasin):
        raise ValueError("Volume evidence subbasin identities do not match")
    names = sorted(source_by_subbasin)
    step_count = len(interval_ends)
    for name in names:
        source = np.asarray(source_by_subbasin[name], dtype=np.float64)
        target = np.asarray(target_by_subbasin[name], dtype=np.float64)
        if source.shape != (step_count,) or target.shape != (step_count,):
            raise ValueError("Volume evidence series lengths do not match the window")
        if (
            not np.isfinite(source).all()
            or not np.isfinite(target).all()
            or (source < 0).any()
            or (target < 0).any()
        ):
            raise ValueError("Volume evidence must be finite and nonnegative")

    per_subbasin = []
    for name in names:
        source = np.asarray(source_by_subbasin[name], dtype=np.float64)
        target = np.asarray(target_by_subbasin[name], dtype=np.float64)
        step_residuals = [
            _residual_record(
                float(source[index]),
                float(target[index]),
                tolerance,
                label=f"subbasin {name!r} step {index}",
            )
            for index in range(step_count)
        ]
        aggregate = _residual_record(
            float(np.sum(source, dtype=np.float64)),
            float(np.sum(target, dtype=np.float64)),
            tolerance,
            label=f"subbasin {name!r} aggregate",
        )
        aggregate["maximum_step_absolute_residual_cubic_meters"] = max(
            item["absolute_residual_cubic_meters"] for item in step_residuals
        )
        per_subbasin.append({"subbasin": name, **aggregate})

    source_total = np.sum(
        np.stack([source_by_subbasin[name] for name in names]),
        axis=0,
        dtype=np.float64,
    )
    target_total = np.sum(
        np.stack([target_by_subbasin[name] for name in names]),
        axis=0,
        dtype=np.float64,
    )
    per_step = []
    for index, interval_end in enumerate(interval_ends):
        per_step.append(
            {
                "interval_end": interval_end.isoformat(),
                **_residual_record(
                    float(source_total[index]),
                    float(target_total[index]),
                    tolerance,
                    label=f"aggregate step {index}",
                ),
            }
        )
    aggregate = _residual_record(
        float(np.sum(source_total, dtype=np.float64)),
        float(np.sum(target_total, dtype=np.float64)),
        tolerance,
        label="aggregate run",
    )
    aggregate["maximum_step_absolute_residual_cubic_meters"] = max(
        item["absolute_residual_cubic_meters"] for item in per_step
    )
    return {
        "per_step": per_step,
        "per_subbasin": per_subbasin,
        "aggregate": aggregate,
    }


class HmsSubbasinTransfer:
    """Compile the volume-conserving HMS-subbasin transfer-map contract."""

    SCHEMA = "hms-commander/subbasin-volume-transfer-map/1.0"
    METHOD = "hms-subbasin-volume-conserving-v1"
    ALGORITHM = "target-center-subbasin-coverage-effective-area-scaling-v1"
    AUDIT_SCHEMA = "hms-commander/subbasin-volume-transfer-audit/1.0"
    PRODUCT_SCHEMA = "hms-commander/subbasin-volume-excess-product/1.0"
    AREA_PRECISION_DECIMAL_PLACES = 9

    @staticmethod
    @log_call
    def compile_transfer_map(
        subbasins: Any,
        application_area: Mapping[str, Any],
        selected_subbasins: Iterable[str],
        hms_model: Mapping[str, Any],
        *,
        name_column: str = "subbasin",
        source_area_column: str = "source_area",
        source_area_units_column: str = "source_area_units",
    ) -> dict[str, Any]:
        """Compile a deterministic, volume-conserving spatial transfer map.

        Target cells are attributed using their center points, matching the
        received engineering prototype. The denominator uses the exact RAS
        effective receiving area rather than the full fishnet cell area.

        Args:
            subbasins: GeoDataFrame containing unique polygon subbasins,
                explicit source areas, and explicit area units.
            application_area: Authenticated RAS Commander application-area
                artifact.
            selected_subbasins: Exact HMS subbasins eligible for transfer.
            hms_model: Project/basin identifiers and portable identities for
                the HMS basin and geometry source files.
            name_column: Column containing HMS/DSS subbasin names.
            source_area_column: Column containing positive source areas.
            source_area_units_column: Column containing explicit area units.

        Returns:
            Validated JSON-serializable transfer-map artifact.

        Raises:
            ValueError: If identities, geometry, units, assignments, or
                receiving-area denominators are incomplete or ambiguous.
        """
        try:
            import shapely
            from pyproj import CRS
            from shapely.geometry import Point
            from shapely.ops import unary_union
        except ImportError as exc:  # pragma: no cover - optional GIS dependencies
            raise ImportError(
                "HmsSubbasinTransfer requires hms-commander[gis]"
            ) from exc

        normalized_application = _validated_application_area(application_area)
        normalized_model = _normalized_hms_model(hms_model)
        if isinstance(selected_subbasins, (str, bytes)):
            raise ValueError("selected_subbasins must be an iterable of names")
        selected_names = [
            _non_empty(name, label="selected subbasin") for name in selected_subbasins
        ]
        if not selected_names:
            raise ValueError("selected_subbasins must not be empty")
        if len(set(selected_names)) != len(selected_names):
            raise ValueError("selected_subbasins must not contain duplicates")
        selected_names = sorted(selected_names)

        required_columns = {
            name_column,
            source_area_column,
            source_area_units_column,
            "geometry",
        }
        if subbasins is None or not required_columns.issubset(
            set(getattr(subbasins, "columns", []))
        ):
            raise ValueError(
                "subbasins must contain name, source area, area units, and geometry"
            )
        if subbasins.crs is None:
            raise ValueError("subbasins must declare a CRS")
        source_crs = CRS.from_user_input(subbasins.crs)
        target_grid = normalized_application["target_grid"]
        target_crs = CRS.from_user_input(target_grid["crs"])

        working = subbasins.loc[
            subbasins[name_column].astype(str).isin(selected_names)
        ].copy()
        working[name_column] = working[name_column].astype(str)
        if working[name_column].duplicated().any():
            raise ValueError("selected HMS subbasin names must be unique")
        found_names = set(working[name_column])
        missing_names = sorted(set(selected_names) - found_names)
        if missing_names:
            raise ValueError(
                "selected HMS subbasins are missing: " + ", ".join(missing_names)
            )
        if working.geometry.isna().any() or working.geometry.is_empty.any():
            raise ValueError("selected HMS subbasin geometry must not be empty")
        if not working.geometry.is_valid.all():
            raise ValueError("selected HMS subbasin geometry must be valid")
        if not all(
            geometry.geom_type in {"Polygon", "MultiPolygon"}
            for geometry in working.geometry
        ):
            raise ValueError("selected HMS subbasin geometry must contain polygons")

        working = working.sort_values(name_column, kind="stable")
        source_geometry_records = [
            {
                "subbasin": str(row[name_column]),
                "geometry_wkb_hex": row.geometry.wkb_hex.lower(),
            }
            for _, row in working.iterrows()
        ]
        source_crs_name = source_crs.to_string()
        source_geometry_sha256 = _canonical_sha256(
            {
                "crs": source_crs_name,
                "subbasins": source_geometry_records,
            }
        )
        if not source_crs.equals(target_crs):
            working = working.to_crs(target_crs)
        if working.geometry.isna().any() or working.geometry.is_empty.any():
            raise ValueError("HMS subbasin reprojection produced empty geometry")
        if not working.geometry.is_valid.all():
            raise ValueError("HMS subbasin reprojection produced invalid geometry")

        area_sum = float(working.geometry.area.sum())
        geometry_union = unary_union(working.geometry.tolist())
        overlap_area = area_sum - float(geometry_union.area)
        overlap_tolerance = max(1.0e-6, area_sum * 1.0e-12)
        if overlap_area > overlap_tolerance:
            raise ValueError(
                "selected HMS subbasin polygons overlap by a positive area"
            )

        subbasin_by_name: dict[str, dict[str, Any]] = {}
        geometry_by_name = {}
        for _, row in working.iterrows():
            name = str(row[name_column])
            area = _normalized_area(
                row[source_area_column],
                row[source_area_units_column],
                label=f"subbasin {name!r}",
            )
            subbasin_by_name[name] = {
                "subbasin": name,
                "source_area": area,
                "assigned_cell_count": 0,
                "receiving_area_square_meters": 0.0,
                "depth_multiplier": 0.0,
            }
            geometry_by_name[name] = row.geometry

        target_cells = []
        unassigned_positive_area = 0.0
        unassigned_positive_count = 0
        for source_cell in normalized_application["cells"]:
            effective_area = float(source_cell["effective_area_square_meters"])
            assigned_name = None
            if effective_area > 0:
                point = Point(*source_cell["center"])
                matches = [
                    name
                    for name in selected_names
                    if geometry_by_name[name].covers(point)
                ]
                if len(matches) > 1:
                    raise ValueError(
                        f"target cell {source_cell['cell_id']} has ambiguous HMS "
                        f"subbasin support: {', '.join(matches)}"
                    )
                if matches:
                    assigned_name = matches[0]
                    subbasin = subbasin_by_name[assigned_name]
                    subbasin["assigned_cell_count"] += 1
                    subbasin["receiving_area_square_meters"] += effective_area
                else:
                    unassigned_positive_count += 1
                    unassigned_positive_area += effective_area
            target_cells.append(
                {
                    "cell_id": source_cell["cell_id"],
                    "row_index": source_cell["row_index"],
                    "column_index": source_cell["column_index"],
                    "subbasin": assigned_name,
                    "effective_area_square_meters": effective_area,
                }
            )

        for name in selected_names:
            subbasin = subbasin_by_name[name]
            receiving_area = subbasin["receiving_area_square_meters"]
            if subbasin["assigned_cell_count"] == 0 or receiving_area <= 0:
                raise ValueError(
                    f"selected HMS subbasin {name!r} has no RAS receiving support"
                )
            rounded_receiving_area = round(
                receiving_area,
                HmsSubbasinTransfer.AREA_PRECISION_DECIMAL_PLACES,
            )
            subbasin["receiving_area_square_meters"] = rounded_receiving_area
            subbasin["depth_multiplier"] = (
                subbasin["source_area"]["square_meters"] / rounded_receiving_area
            )

        for cell in target_cells:
            cell["depth_multiplier"] = (
                0.0
                if cell["subbasin"] is None
                else subbasin_by_name[cell["subbasin"]]["depth_multiplier"]
            )

        ordered_subbasins = [subbasin_by_name[name] for name in selected_names]
        source_area_total = sum(
            item["source_area"]["square_meters"] for item in ordered_subbasins
        )
        receiving_area_total = sum(
            item["receiving_area_square_meters"] for item in ordered_subbasins
        )
        artifact: dict[str, Any] = {
            "schema": HmsSubbasinTransfer.SCHEMA,
            "method": HmsSubbasinTransfer.METHOD,
            "algorithm": HmsSubbasinTransfer.ALGORITHM,
            "hms_model": normalized_model,
            "source_subbasin_crs": source_crs_name,
            "source_subbasin_geometry_sha256": source_geometry_sha256,
            "ras_application_area": {
                "schema": normalized_application["schema"],
                "method": normalized_application["method"],
                "application_area_sha256": normalized_application[
                    "application_area_sha256"
                ],
                "model": normalized_application["model"],
            },
            "target_grid": target_grid,
            "assignment_predicate": "subbasin-covers-target-cell-center",
            "outside_support_behavior": "zero",
            "area_precision_decimal_places": (
                HmsSubbasinTransfer.AREA_PRECISION_DECIMAL_PLACES
            ),
            "compiler": {
                "shapely_version": str(shapely.__version__),
                "geos_version": str(shapely.geos_version_string),
            },
            "subbasins": ordered_subbasins,
            "cells": target_cells,
            "metrics": {
                "selected_subbasin_count": len(ordered_subbasins),
                "target_cell_count": len(target_cells),
                "assigned_target_cell_count": sum(
                    item["assigned_cell_count"] for item in ordered_subbasins
                ),
                "unassigned_positive_area_cell_count": unassigned_positive_count,
                "source_area_square_meters": round(
                    source_area_total,
                    HmsSubbasinTransfer.AREA_PRECISION_DECIMAL_PLACES,
                ),
                "assigned_receiving_area_square_meters": round(
                    receiving_area_total,
                    HmsSubbasinTransfer.AREA_PRECISION_DECIMAL_PLACES,
                ),
                "unassigned_positive_area_square_meters": round(
                    unassigned_positive_area,
                    HmsSubbasinTransfer.AREA_PRECISION_DECIMAL_PLACES,
                ),
            },
        }
        artifact["transfer_map_sha256"] = _canonical_sha256(artifact)
        return HmsSubbasinTransfer.validate_transfer_map(artifact)

    @staticmethod
    @log_call
    def validate_transfer_map(artifact: Mapping[str, Any]) -> dict[str, Any]:
        """Validate a transfer map's identity and volume accounting."""
        try:
            normalized = json.loads(
                json.dumps(artifact, sort_keys=True, allow_nan=False)
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("transfer map must be JSON serializable") from exc
        _required_mapping(
            normalized,
            {
                "schema",
                "method",
                "algorithm",
                "hms_model",
                "source_subbasin_crs",
                "source_subbasin_geometry_sha256",
                "ras_application_area",
                "target_grid",
                "assignment_predicate",
                "outside_support_behavior",
                "area_precision_decimal_places",
                "compiler",
                "subbasins",
                "cells",
                "metrics",
                "transfer_map_sha256",
            },
            label="subbasin volume transfer map",
        )
        expected_constants = {
            "schema": HmsSubbasinTransfer.SCHEMA,
            "method": HmsSubbasinTransfer.METHOD,
            "algorithm": HmsSubbasinTransfer.ALGORITHM,
            "assignment_predicate": "subbasin-covers-target-cell-center",
            "outside_support_behavior": "zero",
            "area_precision_decimal_places": (
                HmsSubbasinTransfer.AREA_PRECISION_DECIMAL_PLACES
            ),
        }
        if any(normalized[key] != value for key, value in expected_constants.items()):
            raise ValueError("transfer-map method or algorithm identity is unsupported")
        normalized["hms_model"] = _normalized_hms_model(normalized["hms_model"])
        source_crs_name = _non_empty(
            normalized["source_subbasin_crs"],
            label="source_subbasin_crs",
        )
        try:
            from pyproj import CRS

            CRS.from_user_input(source_crs_name)
        except (TypeError, ValueError) as exc:
            raise ValueError("source_subbasin_crs is invalid") from exc
        if not _SHA256_PATTERN.fullmatch(normalized["source_subbasin_geometry_sha256"]):
            raise ValueError("source_subbasin_geometry_sha256 is invalid")

        ras_application = normalized["ras_application_area"]
        _required_mapping(
            ras_application,
            {"schema", "method", "application_area_sha256", "model"},
            label="ras_application_area",
        )
        if (
            ras_application["schema"]
            != ("ras-commander/precipitation-application-area/1.0")
            or ras_application["method"] != "ras-mesh-effective-area"
        ):
            raise ValueError("ras_application_area identity is unsupported")
        if not _SHA256_PATTERN.fullmatch(ras_application["application_area_sha256"]):
            raise ValueError("ras_application_area hash is invalid")
        _required_mapping(
            ras_application["model"],
            {"project_id", "plan_id", "geometry_id", "two_d_flow_area"},
            label="ras_application_area.model",
        )
        for key, value in ras_application["model"].items():
            _non_empty(value, label=f"ras_application_area.model.{key}")

        grid = _validated_target_grid(normalized["target_grid"])
        normalized["target_grid"] = grid
        rows, columns = grid["shape"]
        cell_area = float(grid["cell_area_square_meters"])
        cells = normalized["cells"]
        if not isinstance(cells, list) or len(cells) != rows * columns:
            raise ValueError("transfer-map cells do not cover the target grid")

        subbasins = normalized["subbasins"]
        if not isinstance(subbasins, list) or not subbasins:
            raise ValueError("transfer-map subbasins must not be empty")
        if not all(isinstance(item, Mapping) for item in subbasins):
            raise ValueError("transfer-map subbasins must be objects")
        names = [item.get("subbasin") for item in subbasins]
        if (
            not all(isinstance(name, str) and name.strip() for name in names)
            or names != sorted(names)
            or len(set(names)) != len(names)
        ):
            raise ValueError("transfer-map subbasins must be uniquely sorted")
        by_name = {item["subbasin"]: item for item in subbasins}
        assigned_counts = {name: 0 for name in names}
        receiving_areas = {name: 0.0 for name in names}
        unassigned_count = 0
        unassigned_area = 0.0
        for expected_id, cell in enumerate(cells):
            _required_mapping(
                cell,
                {
                    "cell_id",
                    "row_index",
                    "column_index",
                    "subbasin",
                    "effective_area_square_meters",
                    "depth_multiplier",
                },
                label=f"cells[{expected_id}]",
            )
            expected_row, expected_column = divmod(expected_id, columns)
            if (
                cell.get("cell_id") != expected_id
                or cell.get("row_index") != expected_row
                or cell.get("column_index") != expected_column
            ):
                raise ValueError("transfer-map cells are not in row-major order")
            try:
                effective_area = float(cell["effective_area_square_meters"])
                depth_multiplier = float(cell["depth_multiplier"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("transfer-map cell values are invalid") from exc
            if (
                not math.isfinite(effective_area)
                or effective_area < 0
                or effective_area > cell_area
                or not math.isfinite(depth_multiplier)
                or depth_multiplier < 0
            ):
                raise ValueError("transfer-map cell values are outside valid bounds")
            name = cell.get("subbasin")
            if name is None:
                if depth_multiplier != 0:
                    raise ValueError("unassigned cells must have a zero multiplier")
                if effective_area > 0:
                    unassigned_count += 1
                    unassigned_area += effective_area
                continue
            if name not in by_name:
                raise ValueError("transfer-map cell names an unknown subbasin")
            if depth_multiplier != by_name[name].get("depth_multiplier"):
                raise ValueError("transfer-map cell multiplier disagrees with subbasin")
            assigned_counts[name] += 1
            receiving_areas[name] += effective_area

        source_area_total = 0.0
        receiving_area_total = 0.0
        for name, subbasin in by_name.items():
            _required_mapping(
                subbasin,
                {
                    "subbasin",
                    "source_area",
                    "assigned_cell_count",
                    "receiving_area_square_meters",
                    "depth_multiplier",
                },
                label=f"subbasin {name!r}",
            )
            source_area = subbasin["source_area"]
            _required_mapping(
                source_area,
                {"declared_value", "declared_units", "square_meters"},
                label=f"subbasin {name!r} source_area",
            )
            recalculated = _normalized_area(
                source_area["declared_value"],
                source_area["declared_units"],
                label=f"subbasin {name!r}",
            )
            if recalculated != source_area:
                raise ValueError(f"subbasin {name!r} source area is inconsistent")
            receiving_area = round(
                receiving_areas[name],
                HmsSubbasinTransfer.AREA_PRECISION_DECIMAL_PLACES,
            )
            if assigned_counts[name] <= 0 or receiving_area <= 0:
                raise ValueError(f"subbasin {name!r} has no receiving support")
            if (
                subbasin["assigned_cell_count"] != assigned_counts[name]
                or subbasin["receiving_area_square_meters"] != receiving_area
                or subbasin["depth_multiplier"]
                != source_area["square_meters"] / receiving_area
            ):
                raise ValueError(f"subbasin {name!r} denominator is inconsistent")
            source_area_total += source_area["square_meters"]
            receiving_area_total += receiving_area

        metrics = normalized["metrics"]
        expected_metrics = {
            "selected_subbasin_count": len(subbasins),
            "target_cell_count": len(cells),
            "assigned_target_cell_count": sum(assigned_counts.values()),
            "unassigned_positive_area_cell_count": unassigned_count,
            "source_area_square_meters": round(
                source_area_total,
                HmsSubbasinTransfer.AREA_PRECISION_DECIMAL_PLACES,
            ),
            "assigned_receiving_area_square_meters": round(
                receiving_area_total,
                HmsSubbasinTransfer.AREA_PRECISION_DECIMAL_PLACES,
            ),
            "unassigned_positive_area_square_meters": round(
                unassigned_area,
                HmsSubbasinTransfer.AREA_PRECISION_DECIMAL_PLACES,
            ),
        }
        if metrics != expected_metrics:
            raise ValueError("transfer-map metrics are inconsistent")

        _required_mapping(
            normalized["compiler"],
            {"shapely_version", "geos_version"},
            label="compiler",
        )
        _non_empty(
            normalized["compiler"]["shapely_version"],
            label="compiler.shapely_version",
        )
        _non_empty(
            normalized["compiler"]["geos_version"],
            label="compiler.geos_version",
        )
        recorded_hash = normalized.pop("transfer_map_sha256", None)
        if recorded_hash != _canonical_sha256(normalized):
            raise ValueError("transfer_map_sha256 is missing or invalid")
        normalized["transfer_map_sha256"] = recorded_hash
        return normalized

    @staticmethod
    @log_call
    def read_excess_series(
        source_dss: str | Path,
        transfer_map: Mapping[str, Any],
        *,
        source_a_part: str,
        source_run_name: str,
        model_start: datetime,
        model_end: datetime,
        interval_minutes: int,
        source_depth_units: str,
    ) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
        """Read exact interval-end ``PRECIP-EXCESS`` series for a map.

        The logical source series may span multiple dated DSS D-part records
        and must contain exactly one interval-end value for each requested
        model interval. Extra, missing, shifted, or duplicated timestamps are
        rejected rather than trimmed or resampled.
        """
        from .dss import HmsDss

        normalized_map = HmsSubbasinTransfer.validate_transfer_map(transfer_map)
        source = Path(source_dss).resolve()
        if not source.is_file():
            raise FileNotFoundError(f"Source HMS DSS does not exist: {source}")
        if (
            model_start.tzinfo is not None
            or model_end.tzinfo is not None
            or model_end <= model_start
            or model_start.second
            or model_end.second
            or model_start.microsecond
            or model_end.microsecond
        ):
            raise ValueError("Model window must use increasing naive whole minutes")
        if (
            isinstance(interval_minutes, bool)
            or not isinstance(interval_minutes, (int, np.integer))
            or int(interval_minutes) <= 0
        ):
            raise ValueError("interval_minutes must be a positive integer")
        interval_minutes = int(interval_minutes)
        duration_minutes = int((model_end - model_start).total_seconds() // 60)
        if duration_minutes % interval_minutes:
            raise ValueError("Model window is not interval aligned")
        source_units = str(source_depth_units).strip().upper()
        if source_units not in _DEPTH_UNIT_METERS:
            raise ValueError("source_depth_units must be IN or MM")
        if not isinstance(source_a_part, str):
            raise ValueError("source_a_part must be a string, including blank")
        a_part = source_a_part.strip()
        run_name = _non_empty(source_run_name, label="source_run_name")
        expected_f_part = f"RUN:{run_name}"
        interval_ends = pd.date_range(
            start=model_start + timedelta(minutes=interval_minutes),
            end=model_end,
            freq=pd.Timedelta(minutes=interval_minutes),
        )

        before = source.stat()
        source_sha256 = _sha256_file(source)
        catalog = [str(pathname) for pathname in HmsDss.get_catalog(source)]
        series_by_subbasin: dict[str, np.ndarray] = {}
        series_evidence = []
        for subbasin in normalized_map["subbasins"]:
            name = subbasin["subbasin"]
            matches = []
            for pathname in catalog:
                parts = HmsSubbasinTransfer._pathname_parts(pathname)
                if (
                    parts[0].casefold() == a_part.casefold()
                    and parts[1].casefold() == name.casefold()
                    and parts[2].casefold() == "precip-excess"
                    and HmsSubbasinTransfer._interval_minutes_from_part(parts[4])
                    == interval_minutes
                    and parts[5].casefold() == expected_f_part.casefold()
                ):
                    matches.append(pathname)
            if not matches:
                raise ValueError(
                    f"Expected one PRECIP-EXCESS pathname family for {name!r}; "
                    "found none"
                )
            if len(matches) != len(set(matches)):
                raise ValueError(
                    f"PRECIP-EXCESS catalog entries for {name!r} are duplicated"
                )
            interval_parts = {
                HmsSubbasinTransfer._pathname_parts(pathname)[4].casefold()
                for pathname in matches
            }
            if len(interval_parts) != 1:
                raise ValueError(
                    f"PRECIP-EXCESS pathname family for {name!r} has competing "
                    "interval encodings"
                )
            matches.sort(key=str.casefold)
            pathname = matches[0]
            frame = HmsDss.read_timeseries(source, pathname)
            actual_times = pd.DatetimeIndex(frame.index)
            if not actual_times.equals(interval_ends):
                raise ValueError(
                    f"PRECIP-EXCESS timestamps for {name!r} do not exactly "
                    "match interval-end model coverage"
                )
            if str(frame.attrs.get("units", "")).strip().upper() != source_units:
                raise ValueError(
                    f"PRECIP-EXCESS units for {name!r} do not match "
                    f"{source_units!r}"
                )
            if (
                str(frame.attrs.get("type", "")).strip().upper().replace("_", "-")
                != "PER-CUM"
            ):
                raise ValueError(f"PRECIP-EXCESS type for {name!r} is not PER-CUM")
            try:
                actual_interval = int(frame.attrs.get("interval", 0))
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"PRECIP-EXCESS interval for {name!r} is invalid"
                ) from exc
            if actual_interval != interval_minutes:
                raise ValueError(
                    f"PRECIP-EXCESS interval for {name!r} does not match request"
                )
            values = np.asarray(frame["value"], dtype=np.float64)
            if values.shape != (len(interval_ends),):
                raise ValueError(f"PRECIP-EXCESS values for {name!r} are incomplete")
            if not np.isfinite(values).all() or (values < 0).any():
                raise ValueError(f"PRECIP-EXCESS values for {name!r} are invalid")
            series_by_subbasin[name] = values
            series_evidence.append(
                {
                    "subbasin": name,
                    "pathname_family": {
                        "a_part": a_part,
                        "b_part": name,
                        "c_part": "PRECIP-EXCESS",
                        "e_part": HmsSubbasinTransfer._pathname_parts(pathname)[4],
                        "f_part": HmsSubbasinTransfer._pathname_parts(pathname)[5],
                    },
                    "catalog_pathnames": matches,
                    "read_pathname": pathname,
                    "value_sha256": _array_sha256(values),
                    "value_count": len(values),
                }
            )

        after = source.stat()
        if (
            after.st_size != before.st_size
            or after.st_mtime_ns != before.st_mtime_ns
            or after.st_ino != before.st_ino
        ):
            raise RuntimeError("Source HMS DSS changed while excess series were read")
        return series_by_subbasin, {
            "source_dss": {
                "name": source.name,
                "size_bytes": before.st_size,
                "sha256": source_sha256,
                "integrity_verification": {
                    "pre_read": ["sha256", "size", "mtime_ns", "file_id"],
                    "post_read": ["size", "mtime_ns", "file_id"],
                    "status": "unchanged",
                },
            },
            "a_part": a_part,
            "run_name": run_name,
            "parameter": "PRECIP-EXCESS",
            "units": source_units,
            "data_type": "PER-CUM",
            "interval_minutes": interval_minutes,
            "timestamp_semantics": "interval_end",
            "series": series_evidence,
        }

    @staticmethod
    @log_call
    def apply_transfer_map_to_dss(
        source_dss: str | Path,
        transfer_map: Mapping[str, Any],
        output_dss: str | Path,
        output_pathname_selector: str,
        *,
        source_a_part: str,
        source_run_name: str,
        model_start: datetime,
        model_end: datetime,
        interval_minutes: int,
        source_depth_units: str,
        volume_tolerance: Mapping[str, Any],
        readback_absolute_value_tolerance: float,
    ) -> dict[str, Any]:
        """Apply a compiled map and publish a verified RAS-grid DSS product."""
        normalized_map = HmsSubbasinTransfer.validate_transfer_map(transfer_map)
        tolerance = _normalized_volume_tolerance(volume_tolerance)
        try:
            readback_tolerance = float(readback_absolute_value_tolerance)
        except (TypeError, ValueError) as exc:
            raise ValueError("readback tolerance must be numeric") from exc
        if not math.isfinite(readback_tolerance) or readback_tolerance < 0:
            raise ValueError("readback tolerance must be finite and nonnegative")
        source_units = str(source_depth_units).strip().upper()
        if source_units not in _DEPTH_UNIT_METERS:
            raise ValueError("source_depth_units must be IN or MM")
        HmsSubbasinTransfer._validate_output_selector(output_pathname_selector)

        output = Path(output_dss).resolve()
        audit_path = output.with_suffix(".audit.json")
        manifest_path = output.with_suffix(".manifest.json")
        for destination in (output, audit_path, manifest_path):
            if destination.exists():
                raise FileExistsError(f"Refusing to overwrite: {destination}")

        series_by_subbasin, source_evidence = HmsSubbasinTransfer.read_excess_series(
            source_dss,
            normalized_map,
            source_a_part=source_a_part,
            source_run_name=source_run_name,
            model_start=model_start,
            model_end=model_end,
            interval_minutes=interval_minutes,
            source_depth_units=source_units,
        )
        interval_ends = pd.date_range(
            start=model_start + timedelta(minutes=interval_minutes),
            end=model_end,
            freq=pd.Timedelta(minutes=interval_minutes),
        )
        target_grid = normalized_map["target_grid"]
        rows, columns = target_grid["shape"]
        frames = np.zeros((len(interval_ends), rows * columns), dtype=np.float64)
        names = [item["subbasin"] for item in normalized_map["subbasins"]]
        name_to_index = {name: index for index, name in enumerate(names)}
        support_areas = np.zeros((len(names), rows * columns), dtype=np.float64)
        for cell in normalized_map["cells"]:
            name = cell["subbasin"]
            if name is None:
                continue
            cell_id = cell["cell_id"]
            frames[:, cell_id] = series_by_subbasin[name] * cell["depth_multiplier"]
            support_areas[
                name_to_index[name],
                cell_id,
            ] = cell["effective_area_square_meters"]
        frames = frames.reshape((len(interval_ends), rows, columns))
        support_areas = support_areas.reshape((len(names), rows, columns))
        depth_unit_meters = _DEPTH_UNIT_METERS[source_units]
        source_volumes = {
            item["subbasin"]: (
                series_by_subbasin[item["subbasin"]]
                * depth_unit_meters
                * item["source_area"]["square_meters"]
            )
            for item in normalized_map["subbasins"]
        }
        prepublication_volumes = {
            name: np.sum(
                frames * support_areas[index][np.newaxis, :, :],
                axis=(1, 2),
                dtype=np.float64,
            )
            * depth_unit_meters
            for index, name in enumerate(names)
        }
        prepublication_evidence = _volume_evidence(
            source_volumes,
            prepublication_volumes,
            interval_ends,
            tolerance,
        )
        boundaries = [model_start, *interval_ends.to_pydatetime().tolist()]
        write_result = HmsSubbasinTransfer._write_grid_subprocess(
            output,
            output_pathname_selector,
            frames,
            boundaries,
            {
                "cell_size": target_grid["cell_size_meters"],
                "origin": target_grid["origin"],
                "crs": target_grid["crs"],
                "units": source_units,
                "data_type": "PER-CUM",
                "compression": "PRECIP_2_BYTE",
                "interval_minutes": interval_minutes,
            },
            support_ids=names,
            support_areas=support_areas,
            readback_absolute_value_tolerance=readback_tolerance,
            source_volumes=source_volumes,
            interval_ends=interval_ends,
            depth_unit_meters=depth_unit_meters,
            volume_tolerance=tolerance,
        )
        if write_result.get("record_count") != len(interval_ends):
            raise RuntimeError("Grid writer did not publish every model interval")
        published_evidence = write_result.pop("published_volume_evidence")

        audit: dict[str, Any] = {
            "schema": HmsSubbasinTransfer.AUDIT_SCHEMA,
            "method": HmsSubbasinTransfer.METHOD,
            "algorithm": HmsSubbasinTransfer.ALGORITHM,
            "transfer_map_sha256": normalized_map["transfer_map_sha256"],
            "source": source_evidence,
            "model_window": {
                "start": model_start.isoformat(timespec="seconds"),
                "end": model_end.isoformat(timespec="seconds"),
                "interval_minutes": interval_minutes,
                "step_count": len(interval_ends),
                "timestamp_semantics": "interval_end",
            },
            "volume_tolerance": tolerance,
            "prepublication_volume": prepublication_evidence,
            "published_volume": published_evidence,
            "readback": write_result["verification"],
        }
        audit["audit_sha256"] = _canonical_sha256(audit)
        HmsSubbasinTransfer._write_json_new(audit_path, audit)

        manifest: dict[str, Any] = {
            "schema": HmsSubbasinTransfer.PRODUCT_SCHEMA,
            "status": "qualification_only",
            "forecast_eligible": False,
            "method": HmsSubbasinTransfer.METHOD,
            "algorithm": HmsSubbasinTransfer.ALGORITHM,
            "transfer_map_sha256": normalized_map["transfer_map_sha256"],
            "source": source_evidence,
            "audit": {
                "name": audit_path.name,
                "size_bytes": audit_path.stat().st_size,
                "sha256": _sha256_file(audit_path),
                "audit_sha256": audit["audit_sha256"],
            },
            "output": {
                "name": output.name,
                "size_bytes": output.stat().st_size,
                "sha256": _sha256_file(output),
                "pathname_selector": output_pathname_selector,
                "record_count": write_result["record_count"],
                "first_pathname": write_result["first_pathname"],
                "last_pathname": write_result["last_pathname"],
                "start": model_start.isoformat(timespec="seconds"),
                "end": model_end.isoformat(timespec="seconds"),
                "interval_minutes": interval_minutes,
                "units": source_units,
                "data_type": "PER-CUM",
            },
            "volume": published_evidence,
        }
        manifest["manifest_sha256"] = _canonical_sha256(manifest)
        HmsSubbasinTransfer._write_json_new(manifest_path, manifest)
        return manifest

    @staticmethod
    def _write_grid_subprocess(
        output: Path,
        pathname: str,
        frames: np.ndarray,
        boundaries: list[datetime],
        grid_info: Mapping[str, Any],
        *,
        support_ids: list[str],
        support_areas: np.ndarray,
        readback_absolute_value_tolerance: float,
        source_volumes: Mapping[str, np.ndarray],
        interval_ends: pd.DatetimeIndex,
        depth_unit_meters: float,
        volume_tolerance: Mapping[str, float],
    ) -> dict[str, Any]:
        """Write, reopen, and verify grid data before atomic publication."""
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=f".{output.stem}-writer-",
            dir=output.parent,
        ) as stage_name:
            stage = Path(stage_name)
            data_path = stage / "frames.npy"
            areas_path = stage / "effective-areas.npy"
            request_path = stage / "request.json"
            result_path = stage / "result.json"
            staged_output = stage / output.name
            np.save(data_path, np.asarray(frames, dtype=np.float32), allow_pickle=False)
            np.save(
                areas_path,
                np.asarray(support_areas, dtype=np.float64),
                allow_pickle=False,
            )
            request = {
                "schema": "hms-commander/grid-writer-request/1.0",
                "data": {"path": str(data_path), "sha256": _sha256_file(data_path)},
                "output_dss": str(staged_output),
                "pathname": pathname,
                "times": [value.isoformat(timespec="seconds") for value in boundaries],
                "grid_info": dict(grid_info),
                "verification": {
                    "effective_areas": {
                        "path": str(areas_path),
                        "sha256": _sha256_file(areas_path),
                    },
                    "absolute_value_tolerance": readback_absolute_value_tolerance,
                    "expected_units": grid_info["units"],
                    "expected_data_type": grid_info["data_type"],
                    "expected_grid": {
                        "shape": list(frames.shape[1:]),
                        "cell_size": grid_info["cell_size"],
                        "origin": list(grid_info["origin"]),
                        "crs": grid_info["crs"],
                    },
                    "support_ids": support_ids,
                },
            }
            request_path.write_text(
                json.dumps(request, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "hms_commander.HmsGridWriterWorker",
                    "--request",
                    str(request_path),
                    "--result",
                    str(result_path),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=600,
            )
            if not result_path.is_file():
                raise RuntimeError(
                    "Grid writer produced no result; "
                    f"exit={completed.returncode}, stderr={completed.stderr[-2000:]}"
                )
            result = json.loads(result_path.read_text(encoding="utf-8"))
            if completed.returncode != 0 or result.get("status") != "succeeded":
                error = result.get("error", {})
                raise RuntimeError(
                    "Grid writer failed: "
                    f"{error.get('type', 'unknown')}: {error.get('message', '')}"
                )
            verification = result.get("verification")
            if not isinstance(verification, dict) or verification.get("status") != (
                "verified"
            ):
                raise RuntimeError("Grid writer produced no readback verification")
            weighted = verification.get("weighted_depth_area_by_support")
            if not isinstance(weighted, dict) or set(weighted) != set(support_ids):
                raise RuntimeError("Grid writer readback support identities changed")
            published_volumes = {
                name: np.asarray(weighted[name], dtype=np.float64) * depth_unit_meters
                for name in support_ids
            }
            published_evidence = _volume_evidence(
                source_volumes,
                published_volumes,
                interval_ends,
                volume_tolerance,
            )
            if not staged_output.is_file():
                raise RuntimeError("Grid writer produced no staged DSS")
            staged_output.replace(output)
            result["published_volume_evidence"] = published_evidence
            return result

    @staticmethod
    def _pathname_parts(pathname: str) -> list[str]:
        if (
            not isinstance(pathname, str)
            or not pathname.startswith("/")
            or not pathname.endswith("/")
        ):
            raise ValueError(f"Invalid six-part DSS pathname: {pathname!r}")
        parts = pathname[1:-1].split("/")
        if len(parts) != 6:
            raise ValueError(f"Invalid six-part DSS pathname: {pathname!r}")
        return parts

    @staticmethod
    def _interval_minutes_from_part(value: str) -> int | None:
        normalized = re.sub(r"[^A-Z0-9]", "", str(value).upper())
        match = re.fullmatch(r"(\d+)(MIN|MINS|MINUTE|MINUTES)", normalized)
        if match:
            return int(match.group(1))
        match = re.fullmatch(r"(\d+)(HOUR|HOURS|HR|HRS)", normalized)
        if match:
            return int(match.group(1)) * 60
        match = re.fullmatch(r"(\d+)(DAY|DAYS)", normalized)
        if match:
            return int(match.group(1)) * 24 * 60
        return None

    @staticmethod
    def _validate_output_selector(pathname: str) -> None:
        parts = HmsSubbasinTransfer._pathname_parts(pathname)
        if (
            parts[3]
            or parts[4]
            or not all(parts[index] for index in (0, 1, 2, 5))
            or parts[2].casefold() != "precipitation"
        ):
            raise ValueError(
                "Output selector requires A/B/PRECIPITATION/F and blank D/E parts"
            )

    @staticmethod
    def _write_json_new(path: Path, payload: Mapping[str, Any]) -> None:
        if path.exists():
            raise FileExistsError(f"Refusing to overwrite JSON artifact: {path}")
        content = json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n"
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)

    @staticmethod
    @log_call
    def write_transfer_map(
        artifact: Mapping[str, Any],
        output_path: str | Path,
    ) -> Path:
        """Write an immutable transfer-map artifact idempotently."""
        normalized = HmsSubbasinTransfer.validate_transfer_map(artifact)
        destination = Path(output_path).resolve()
        content = json.dumps(normalized, indent=2, sort_keys=True) + "\n"
        if destination.exists():
            if destination.read_text(encoding="utf-8") == content:
                return destination
            raise FileExistsError(
                f"Refusing to replace different transfer map: {destination}"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=f".{destination.stem}-",
            dir=destination.parent,
        ) as stage_name:
            staged = Path(stage_name) / destination.name
            staged.write_text(content, encoding="utf-8")
            staged.replace(destination)
        return destination
