"""Compile deterministic HMS-subbasin to RAS-grid transfer maps."""

from __future__ import annotations

import hashlib
import json
import math
import re
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

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


def _canonical_sha256(payload: Mapping[str, Any]) -> str:
    content = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(content).hexdigest()


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


class HmsSubbasinTransfer:
    """Compile the volume-conserving HMS-subbasin transfer-map contract."""

    SCHEMA = "hms-commander/subbasin-volume-transfer-map/1.0"
    METHOD = "hms-subbasin-volume-conserving-v1"
    ALGORITHM = "target-center-subbasin-coverage-effective-area-scaling-v1"
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
