"""Package-owned spatial-transfer audits for gridded HMS results."""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from .Decorators import log_call
from .HmsSqlite import HmsSqlite

_ACRE_FOOT_CUBIC_METERS = 1233.48183754752
_DEPTH_UNIT_METERS = {"IN": 0.0254, "MM": 0.001}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(payload: Mapping[str, Any]) -> str:
    content = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def _array_sha256(values: np.ndarray) -> str:
    contiguous = np.ascontiguousarray(values)
    digest = hashlib.sha256()
    digest.update(str(contiguous.dtype).encode("ascii"))
    digest.update(json.dumps(list(contiguous.shape)).encode("ascii"))
    digest.update(contiguous.tobytes(order="C"))
    return digest.hexdigest()


def _hdf_text_attribute(dataset: Any, name: str, *, label: str) -> str:
    raw = dataset.attrs.get(name)
    if raw is None:
        raise ValueError(f"{label} is missing HDF attribute {name!r}")
    values = np.asarray(raw).reshape(-1)
    if len(values) != 1:
        raise ValueError(f"{label} HDF attribute {name!r} must be scalar")
    value = values[0]
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    normalized = str(value).strip().upper()
    if not normalized:
        raise ValueError(f"{label} HDF attribute {name!r} must not be empty")
    return normalized


def _require_optional_dependencies() -> tuple[Any, Any, Any, Any, Any]:
    try:
        import geopandas as gpd
        import h5py
        from pyproj import CRS
        from scipy.optimize import linear_sum_assignment
        from scipy.spatial import cKDTree
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise ImportError(
            "HmsSpatialTransfer requires hms-commander[gis]"
        ) from exc
    return gpd, h5py, CRS, linear_sum_assignment, cKDTree


def _grid_definition(
    configured: Mapping[str, Any],
    *,
    label: str,
    crs_factory: Any,
) -> dict[str, Any]:
    required = {
        "definition_id",
        "crs",
        "shape",
        "cell_size_meters",
        "origin",
        "row_order",
    }
    missing = sorted(required - set(configured))
    if missing:
        raise ValueError(f"{label} is missing: {', '.join(missing)}")

    shape = tuple(int(value) for value in configured["shape"])
    origin = tuple(float(value) for value in configured["origin"])
    cell_size = float(configured["cell_size_meters"])
    if len(shape) != 2 or any(value <= 0 for value in shape):
        raise ValueError(f"{label} shape must contain two positive integers")
    if len(origin) != 2 or not all(math.isfinite(value) for value in origin):
        raise ValueError(f"{label} origin must contain two finite coordinates")
    if not math.isfinite(cell_size) or cell_size <= 0:
        raise ValueError(f"{label} cell_size_meters must be positive")
    if str(configured["row_order"]) != "south_to_north":
        raise ValueError(f"{label} row_order must be 'south_to_north'")

    crs = crs_factory.from_user_input(str(configured["crs"]))
    if not crs.is_projected:
        raise ValueError(f"{label} CRS must be projected")
    axis_units = {axis.unit_name.lower() for axis in crs.axis_info if axis.unit_name}
    if axis_units and axis_units != {"metre"} and axis_units != {"meter"}:
        raise ValueError(f"{label} CRS axes must use meters")

    expected_area = cell_size * cell_size
    area = float(configured.get("cell_area_square_meters", expected_area))
    if not math.isclose(area, expected_area, rel_tol=1.0e-12, abs_tol=1.0e-9):
        raise ValueError(
            f"{label} cell_area_square_meters must equal cell_size_meters squared"
        )

    definition = {
        "definition_id": str(configured["definition_id"]),
        "crs": str(configured["crs"]),
        "shape": list(shape),
        "cell_size_meters": cell_size,
        "cell_area_square_meters": area,
        "origin": list(origin),
        "row_order": "south_to_north",
    }
    definition["definition_sha256"] = _canonical_sha256(definition)
    return definition


def _target_centers(grid: Mapping[str, Any]) -> np.ndarray:
    rows, columns = grid["shape"]
    origin_x, origin_y = grid["origin"]
    cell_size = grid["cell_size_meters"]
    return np.asarray(
        [
            (
                origin_x + (column + 0.5) * cell_size,
                origin_y + (row + 0.5) * cell_size,
            )
            for row in range(rows)
            for column in range(columns)
        ],
        dtype=np.float64,
    )


def _distance_metrics(distances: np.ndarray) -> dict[str, Any]:
    if not len(distances):
        return {
            "count": 0,
            "minimum_meters": 0.0,
            "maximum_meters": 0.0,
            "mean_meters": 0.0,
            "percentiles_meters": {
                "p50": 0.0,
                "p90": 0.0,
                "p95": 0.0,
                "p99": 0.0,
            },
        }
    percentiles = np.percentile(distances, [50.0, 90.0, 95.0, 99.0])
    return {
        "count": int(len(distances)),
        "minimum_meters": float(np.min(distances)),
        "maximum_meters": float(np.max(distances)),
        "mean_meters": float(np.mean(distances)),
        "percentiles_meters": {
            key: float(value)
            for key, value in zip(
                ("p50", "p90", "p95", "p99"),
                percentiles,
                strict=True,
            )
        },
    }


def _equivalent_fingerprint_groups(
    values: np.ndarray,
    *,
    tolerance: float,
) -> list[np.ndarray]:
    parents = np.arange(len(values), dtype=int)

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = int(parents[index])
        return index

    for first in range(len(values)):
        residuals = np.max(np.abs(values[first + 1 :] - values[first]), axis=1)
        for second in np.flatnonzero(residuals <= tolerance) + first + 1:
            first_root = find(first)
            second_root = find(int(second))
            if first_root != second_root:
                parents[second_root] = first_root

    groups: dict[int, list[int]] = {}
    for index in range(len(values)):
        groups.setdefault(find(index), []).append(index)
    return [
        np.asarray(indexes, dtype=int)
        for indexes in groups.values()
        if len(indexes) > 1
    ]


def _validate_permutation_invariant_ambiguities(
    source_fingerprints: np.ndarray,
    result_fingerprints: np.ndarray,
    excess: np.ndarray,
    result_for_cell: np.ndarray,
    *,
    tolerance: float,
    element_name: str,
) -> dict[str, Any]:
    source_groups = _equivalent_fingerprint_groups(
        source_fingerprints,
        tolerance=tolerance,
    )
    for group in source_groups:
        result_columns = result_for_cell[group]
        assigned_fingerprints = result_fingerprints[result_columns]
        if np.max(np.ptp(assigned_fingerprints, axis=0)) > tolerance:
            raise ValueError(
                f"Ambiguous source fingerprints do not map to equivalent "
                f"HMS results for {element_name!r}"
            )
        assigned_excess = excess[:, result_columns]
        if not np.all(assigned_excess == assigned_excess[:, :1]):
            raise ValueError(
                f"Ambiguous fingerprints change transferred excess for "
                f"{element_name!r}"
            )

    result_groups = _equivalent_fingerprint_groups(
        result_fingerprints,
        tolerance=tolerance,
    )
    cell_for_result = np.empty(len(result_for_cell), dtype=int)
    cell_for_result[result_for_cell] = np.arange(len(result_for_cell))
    for group in result_groups:
        assigned_sources = source_fingerprints[cell_for_result[group]]
        if np.max(np.ptp(assigned_sources, axis=0)) > 2.0 * tolerance:
            raise ValueError(
                f"Ambiguous HMS result fingerprints do not map to equivalent "
                f"source cells for {element_name!r}"
            )
        assigned_excess = excess[:, group]
        if not np.all(assigned_excess == assigned_excess[:, :1]):
            raise ValueError(
                f"Ambiguous HMS result fingerprints change transferred excess "
                f"for {element_name!r}"
            )
    return {
        "ambiguous_fingerprint_group_count": len(source_groups),
        "ambiguously_identified_cell_count": int(
            sum(len(group) for group in source_groups)
        ),
        "ambiguous_result_fingerprint_group_count": len(result_groups),
        "ambiguously_identified_result_column_count": int(
            sum(len(group) for group in result_groups)
        ),
        "ambiguities_are_permutation_invariant": True,
    }


def _volume_metrics(
    transfer_values: np.ndarray,
    direct: np.ndarray,
    cell_area_square_meters: float,
    depth_units: str,
) -> dict[str, Any]:
    cumulative_depth = transfer_values.sum(axis=0, dtype=np.float64)
    cubic_meters_per_depth_unit = (
        cell_area_square_meters * _DEPTH_UNIT_METERS[depth_units]
    )
    direct_volume = float(
        cumulative_depth[direct].sum() * cubic_meters_per_depth_unit
    )
    filled_volume = float(
        cumulative_depth[~direct].sum() * cubic_meters_per_depth_unit
    )
    total_volume = direct_volume + filled_volume

    def values(scale: float) -> dict[str, float]:
        return {
            "direct_supported": direct_volume * scale,
            "nearest_filled_assigned": filled_volume * scale,
            "target_total": total_volume * scale,
            "incremental_vs_outside_support_zero": filled_volume * scale,
        }

    return {
        "quantity": "incremental_excess_volume",
        "source_depth_units": depth_units,
        "baseline": "outside-support-zero",
        "comparison": "nearest-active-hms-cell",
        "cubic_meters": values(1.0),
        "acre_feet": values(1.0 / _ACRE_FOOT_CUBIC_METERS),
        "incremental_percent_of_comparison_total": (
            0.0 if total_volume == 0.0 else filled_volume / total_volume * 100.0
        ),
    }


class HmsSpatialTransfer:
    """Static namespace for deterministic HMS grid-transfer evidence."""

    SCHEMA = "hms-commander/spatial-transfer-audit/1.0"

    @staticmethod
    def _transfer_excess_to_grid(
        hms_result_hdf: str | Path,
        hms_basin_sqlite: str | Path,
        source_fingerprint_cube: np.ndarray,
        source_grid_definition: Mapping[str, Any],
        target_grid_definition: Mapping[str, Any],
        *,
        excess_depth_units: str,
        fingerprint_stride: int = 1,
        source_value_multiplier: float = 1.0,
        fingerprint_tolerance: float = 1.0e-6,
        result_group: str = "results",
        excess_dataset: str = "Incremental Excess",
        precipitation_dataset: str = "lwe_precipitation_rate",
    ) -> tuple[dict[str, Any], np.ndarray]:
        """Audit nearest-fill transfer of HMS incremental excess.

        The caller supplies portable source and target grid definitions plus a
        source precipitation cube normalized through ``source_value_multiplier``
        to the HMS precipitation dataset's units and interval. HMS Commander
        resolves result columns to computation cells, transfers incremental
        excess by polygon containment with nearest-centroid fill, and returns
        raw metrics without applying qualification thresholds.

        Args:
            hms_result_hdf: Completed HMS result HDF containing per-element
                incremental excess and precipitation datasets.
            hms_basin_sqlite: HMS basin SQLite database containing the
                ``discretization`` computation-cell layer.
            source_fingerprint_cube: Source precipitation values arranged as
                ``(time, row, column)``.
            source_grid_definition: Portable definition for the fingerprint
                cube grid.
            target_grid_definition: Portable definition for the target grid.
            excess_depth_units: Units of the incremental-excess values. Only
                ``IN`` and ``MM`` are accepted.
            fingerprint_stride: HMS result timestep stride corresponding to
                one source fingerprint frame.
            source_value_multiplier: Multiplier converting source cube values
                to one HMS result interval before matching.
            fingerprint_tolerance: Maximum absolute precipitation residual
                allowed after one-to-one column matching.
            result_group: HDF group containing element result groups.
            excess_dataset: Per-element incremental excess dataset name.
            precipitation_dataset: Per-element precipitation dataset name.

        Returns:
            A JSON-serializable, content-addressed raw audit record.
        """
        gpd, h5py, CRS, linear_sum_assignment, cKDTree = (
            _require_optional_dependencies()
        )
        hdf_path = Path(hms_result_hdf).resolve()
        sqlite_path = Path(hms_basin_sqlite).resolve()
        for path, label in (
            (hdf_path, "HMS result HDF"),
            (sqlite_path, "HMS basin SQLite"),
        ):
            if not path.is_file():
                raise FileNotFoundError(f"{label} does not exist: {path}")
        if fingerprint_stride <= 0:
            raise ValueError("fingerprint_stride must be positive")
        normalized_depth_units = str(excess_depth_units).upper()
        if normalized_depth_units not in _DEPTH_UNIT_METERS:
            raise ValueError("excess_depth_units must be 'IN' or 'MM'")
        if (
            not math.isfinite(source_value_multiplier)
            or source_value_multiplier <= 0
        ):
            raise ValueError("source_value_multiplier must be positive")
        if fingerprint_tolerance < 0 or not math.isfinite(fingerprint_tolerance):
            raise ValueError("fingerprint_tolerance must be finite and non-negative")

        source_grid = _grid_definition(
            source_grid_definition,
            label="source grid definition",
            crs_factory=CRS,
        )
        target_grid = _grid_definition(
            target_grid_definition,
            label="target grid definition",
            crs_factory=CRS,
        )
        if not CRS.from_user_input(source_grid["crs"]).equals(
            CRS.from_user_input(target_grid["crs"])
        ):
            raise ValueError("source and target grid CRS values do not match")

        fingerprint_cube = np.asarray(source_fingerprint_cube, dtype=np.float64)
        if fingerprint_cube.ndim != 3:
            raise ValueError("source_fingerprint_cube must have three dimensions")
        if tuple(fingerprint_cube.shape[1:]) != tuple(source_grid["shape"]):
            raise ValueError(
                "source_fingerprint_cube spatial shape does not match source grid"
            )
        computation_cells = HmsSqlite.get_discretization(sqlite_path).copy()
        required_columns = {"subbasin", "geometry"}
        missing_columns = required_columns - set(computation_cells.columns)
        if missing_columns:
            raise ValueError(
                "HMS discretization is missing: "
                + ", ".join(sorted(missing_columns))
            )
        if computation_cells.empty or computation_cells.crs is None:
            raise ValueError("HMS discretization must contain cells with a CRS")
        if not CRS.from_user_input(computation_cells.crs).equals(
            CRS.from_user_input(source_grid["crs"])
        ):
            raise ValueError("HMS discretization CRS does not match grid CRS")

        active_geometries: list[Any] = []
        active_excess: list[np.ndarray] = []
        element_metrics: dict[str, Any] = {}
        source_rows, source_columns = source_grid["shape"]
        source_origin_x, source_origin_y = source_grid["origin"]
        source_cell_size = source_grid["cell_size_meters"]

        with h5py.File(hdf_path, "r") as hdf:
            if result_group not in hdf:
                raise ValueError(f"HMS result HDF is missing group {result_group!r}")
            for element_name, group in sorted(hdf[result_group].items()):
                if excess_dataset not in group:
                    continue
                if precipitation_dataset not in group:
                    raise ValueError(
                        f"HMS result element {element_name!r} is missing "
                        f"{precipitation_dataset!r}"
                    )
                element_cells = computation_cells.loc[
                    computation_cells["subbasin"].astype(str).str.casefold()
                    == str(element_name).casefold()
                ].copy()
                excess_source = group[excess_dataset]
                hdf_depth_units = _hdf_text_attribute(
                    excess_source,
                    "units",
                    label=(
                        f"HMS result element {element_name!r} dataset "
                        f"{excess_dataset!r}"
                    ),
                )
                if hdf_depth_units != normalized_depth_units:
                    raise ValueError(
                        f"HMS incremental excess units for {element_name!r} "
                        f"are {hdf_depth_units!r}, not declared "
                        f"{normalized_depth_units!r}"
                    )
                excess = np.asarray(excess_source, dtype=np.float64)
                precipitation = np.asarray(
                    group[precipitation_dataset], dtype=np.float64
                )
                if not len(element_cells):
                    raise ValueError(
                        f"HMS result element {element_name!r} has no computation cells"
                    )
                if excess.ndim != 2:
                    raise ValueError(
                        f"HMS result/cell shape mismatch for {element_name!r}"
                    )
                expected_shape = (excess.shape[0], len(element_cells))
                if excess.shape != expected_shape:
                    raise ValueError(
                        f"HMS result/cell shape mismatch for {element_name!r}"
                    )
                if precipitation.shape != excess.shape:
                    raise ValueError(
                        f"HMS excess/precipitation shape mismatch for {element_name!r}"
                    )
                if not np.isfinite(excess).all() or (excess < 0).any():
                    raise ValueError(
                        f"HMS incremental excess is invalid for {element_name!r}"
                    )
                if not np.isfinite(precipitation).all() or (
                    precipitation < 0
                ).any():
                    raise ValueError(
                        f"HMS precipitation is invalid for {element_name!r}"
                    )

                centers = element_cells.geometry.centroid
                columns = np.floor(
                    (centers.x.to_numpy(dtype=np.float64) - source_origin_x)
                    / source_cell_size
                ).astype(int)
                rows = np.floor(
                    (centers.y.to_numpy(dtype=np.float64) - source_origin_y)
                    / source_cell_size
                ).astype(int)
                if (
                    rows.min() < 0
                    or columns.min() < 0
                    or rows.max() >= source_rows
                    or columns.max() >= source_columns
                ):
                    raise ValueError(
                        f"HMS cells fall outside the source grid for {element_name!r}"
                    )

                source_fingerprints = (
                    fingerprint_cube[:, rows, columns].T
                    * source_value_multiplier
                )
                if not np.isfinite(source_fingerprints).all():
                    raise ValueError(
                        f"Source fingerprints are invalid for {element_name!r}"
                    )
                result_fingerprints = precipitation[
                    : fingerprint_cube.shape[0] * fingerprint_stride
                    : fingerprint_stride,
                    :,
                ].T
                if result_fingerprints.shape != source_fingerprints.shape:
                    raise ValueError(
                        f"Fingerprint window does not match for {element_name!r}"
                    )
                differences = (
                    result_fingerprints[:, None, :]
                    - source_fingerprints[None, :, :]
                )
                costs = np.mean(np.square(differences), axis=2)
                result_columns, cell_rows = linear_sum_assignment(costs)
                if len(result_columns) != len(element_cells):
                    raise ValueError(
                        f"Incomplete result-to-cell match for {element_name!r}"
                    )
                result_for_cell = np.empty(len(element_cells), dtype=int)
                result_for_cell[cell_rows] = result_columns
                residual = (
                    precipitation[
                        : fingerprint_cube.shape[0] * fingerprint_stride
                        : fingerprint_stride,
                        result_for_cell,
                    ].T
                    - source_fingerprints
                )
                maximum_residual = float(np.max(np.abs(residual)))
                if maximum_residual > fingerprint_tolerance:
                    raise ValueError(
                        f"Fingerprint residual exceeds tolerance for "
                        f"{element_name!r}: {maximum_residual}"
                    )
                ambiguity_metrics = _validate_permutation_invariant_ambiguities(
                    source_fingerprints,
                    result_fingerprints,
                    excess,
                    result_for_cell,
                    tolerance=fingerprint_tolerance,
                    element_name=str(element_name),
                )

                element_cells = element_cells.reset_index(drop=True)
                active_geometries.extend(element_cells.geometry.tolist())
                active_excess.extend(
                    excess[:, result_for_cell[index]]
                    for index in range(len(element_cells))
                )
                element_metrics[str(element_name)] = {
                    "cell_count": int(len(element_cells)),
                    "maximum_absolute_residual": maximum_residual,
                    "root_mean_square_residual": float(
                        np.sqrt(np.mean(np.square(residual)))
                    ),
                    **ambiguity_metrics,
                }

        if not active_excess:
            raise ValueError("HMS result HDF contains no incremental excess datasets")
        time_steps = {len(values) for values in active_excess}
        if len(time_steps) != 1:
            raise ValueError("HMS incremental excess time axes do not align")
        excess_by_cell = np.stack(active_excess, axis=1)

        source_cells = gpd.GeoDataFrame(
            {"active_index": np.arange(len(active_geometries), dtype=int)},
            geometry=active_geometries,
            crs=computation_cells.crs,
        )
        target_centers = _target_centers(target_grid)
        target_points = gpd.GeoDataFrame(
            {"target_index": np.arange(len(target_centers), dtype=int)},
            geometry=gpd.points_from_xy(
                target_centers[:, 0], target_centers[:, 1]
            ),
            crs=computation_cells.crs,
        )
        contained = gpd.sjoin(
            target_points,
            source_cells[["active_index", "geometry"]],
            how="left",
            predicate="within",
        )
        if contained.index.duplicated().any():
            duplicate_count = int(contained.index.duplicated().sum())
            raise ValueError(
                f"{duplicate_count} target centers intersect multiple HMS cells"
            )
        contained = contained.reindex(target_points.index)

        source_centroids = source_cells.geometry.centroid
        source_centers = np.column_stack(
            (
                source_centroids.x.to_numpy(dtype=np.float64),
                source_centroids.y.to_numpy(dtype=np.float64),
            )
        )
        nearest_distances, nearest_indexes = cKDTree(source_centers).query(
            target_centers,
            k=1,
        )
        direct = contained["active_index"].notna().to_numpy(dtype=bool)
        selected_indexes = nearest_indexes.astype(int)
        selected_indexes[direct] = contained.loc[
            direct, "active_index"
        ].to_numpy(dtype=int)
        transferred = excess_by_cell[:, selected_indexes]
        if not np.isfinite(transferred).all() or (transferred < 0).any():
            raise ValueError("Transferred excess contains invalid values")

        target_count = len(target_centers)
        unsupported_count = int((~direct).sum())
        cell_area = target_grid["cell_area_square_meters"]
        report: dict[str, Any] = {
            "schema": HmsSpatialTransfer.SCHEMA,
            "method": "polygon-containment-then-nearest-active-centroid",
            "source_identity": {
                "hms_result_hdf_sha256": _sha256_file(hdf_path),
                "hms_basin_sqlite_sha256": _sha256_file(sqlite_path),
                "source_fingerprint_cube_sha256": _array_sha256(
                    fingerprint_cube
                ),
            },
            "source_grid": source_grid,
            "target_grid": target_grid,
            "fingerprint": {
                "frame_count": int(fingerprint_cube.shape[0]),
                "stride": int(fingerprint_stride),
                "source_value_multiplier": float(source_value_multiplier),
                "maximum_absolute_tolerance": float(fingerprint_tolerance),
                "elements": element_metrics,
                "maximum_absolute_residual": max(
                    item["maximum_absolute_residual"]
                    for item in element_metrics.values()
                ),
            },
            "metrics": {
                "active_hms_element_count": len(element_metrics),
                "active_hms_cell_count": len(active_geometries),
                "time_step_count": int(next(iter(time_steps))),
                "target_cell_count": target_count,
                "direct_target_cell_count": int(direct.sum()),
                "unsupported_target_cell_count": unsupported_count,
                "target_area_square_meters": target_count * cell_area,
                "direct_area_square_meters": int(direct.sum()) * cell_area,
                "unsupported_area_square_meters": unsupported_count * cell_area,
                "nearest_fill_distance": _distance_metrics(
                    nearest_distances[~direct]
                ),
                "volume_effect": _volume_metrics(
                    transferred,
                    direct,
                    cell_area,
                    normalized_depth_units,
                ),
            },
        }
        report["audit_sha256"] = _canonical_sha256(report)
        return report, transferred

    @staticmethod
    @log_call
    def audit_excess_to_grid(
        hms_result_hdf: str | Path,
        hms_basin_sqlite: str | Path,
        source_fingerprint_cube: np.ndarray,
        source_grid_definition: Mapping[str, Any],
        target_grid_definition: Mapping[str, Any],
        *,
        excess_depth_units: str,
        fingerprint_stride: int = 1,
        source_value_multiplier: float = 1.0,
        fingerprint_tolerance: float = 1.0e-6,
        result_group: str = "results",
        excess_dataset: str = "Incremental Excess",
        precipitation_dataset: str = "lwe_precipitation_rate",
    ) -> dict[str, Any]:
        """Return content-addressed raw metrics for one excess-grid transfer."""
        report, _ = HmsSpatialTransfer._transfer_excess_to_grid(
            hms_result_hdf,
            hms_basin_sqlite,
            source_fingerprint_cube,
            source_grid_definition,
            target_grid_definition,
            excess_depth_units=excess_depth_units,
            fingerprint_stride=fingerprint_stride,
            source_value_multiplier=source_value_multiplier,
            fingerprint_tolerance=fingerprint_tolerance,
            result_group=result_group,
            excess_dataset=excess_dataset,
            precipitation_dataset=precipitation_dataset,
        )
        return report

    @staticmethod
    @log_call
    def read_fingerprint_cube(
        source_dss: str | Path,
        pathname_selector: str,
        source_grid_definition: Mapping[str, Any],
        *,
        model_start: datetime,
        model_end: datetime,
        interval_minutes: int,
    ) -> tuple[np.ndarray, dict[str, Any]]:
        """Read and authenticate a regular source-forcing grid family."""
        try:
            from pyproj import CRS
            from ras_commander import RasDss
        except ImportError as exc:  # pragma: no cover - optional environment
            raise ImportError(
                "Grid transfer export requires hms-commander[gis,dss]"
            ) from exc
        source = Path(source_dss).resolve()
        if not source.is_file():
            raise FileNotFoundError(f"Source fingerprint DSS does not exist: {source}")
        source_stat = source.stat()
        source_sha256 = _sha256_file(source)
        if interval_minutes <= 0 or model_end <= model_start:
            raise ValueError("Source fingerprint window or interval is invalid")
        selector_parts = HmsSpatialTransfer._pathname_parts(pathname_selector)
        if selector_parts[3] or selector_parts[4]:
            raise ValueError("Source fingerprint selector D and E parts must be blank")
        normalized_grid = _grid_definition(
            source_grid_definition,
            label="source grid definition",
            crs_factory=CRS,
        )
        catalog = RasDss.get_catalog(source)
        records: list[tuple[datetime, datetime, str]] = []
        for raw_pathname in catalog["pathname"].astype(str):
            parts = HmsSpatialTransfer._pathname_parts(raw_pathname)
            if any(
                parts[index].casefold() != selector_parts[index].casefold()
                for index in (0, 1, 2, 5)
            ):
                continue
            start = HmsSpatialTransfer._grid_time(parts[3])
            end = HmsSpatialTransfer._grid_time(parts[4])
            if model_start <= start and end <= model_end:
                records.append((start, end, raw_pathname))
        records.sort(key=lambda value: (value[0], value[1], value[2].casefold()))
        if not records:
            raise ValueError(
                "Source fingerprint DSS has no records in the model window"
            )
        expected_count = int((model_end - model_start).total_seconds() // 60)
        if expected_count % interval_minutes:
            raise ValueError("Source fingerprint window is not interval-aligned")
        expected_count //= interval_minutes
        if len(records) != expected_count:
            raise ValueError(
                "Source fingerprint record count does not cover the model window: "
                f"expected {expected_count}, found {len(records)}"
            )
        current = model_start
        frames: list[np.ndarray] = []
        pathname_evidence: list[str] = []
        for start, end, pathname in records:
            if start != current or end != start + timedelta(minutes=interval_minutes):
                raise ValueError(
                    "Source fingerprint DSS time coverage is not contiguous"
                )
            grid = RasDss.read_grid(source, pathname)
            actual_origin = grid["metadata"].get("origin")
            if (
                list(grid["shape"]) != normalized_grid["shape"]
                or not math.isclose(
                    float(grid["cell_size"]),
                    normalized_grid["cell_size_meters"],
                )
                or list(actual_origin or ()) != normalized_grid["origin"]
                or not CRS.from_user_input(str(grid["crs"])).equals(
                    CRS.from_user_input(normalized_grid["crs"])
                )
            ):
                raise ValueError("Source fingerprint DSS grid definition has drifted")
            values = np.asarray(grid["data"], dtype=np.float64)
            finite = values[np.isfinite(values)]
            if np.isinf(values).any() or (finite < 0).any():
                raise ValueError(
                    f"Source fingerprint grid contains invalid values: {pathname}"
                )
            frames.append(values)
            pathname_evidence.append(pathname)
            current = end
        if current != model_end:
            raise ValueError("Source fingerprint DSS does not end at the model window")
        source_stat_after = source.stat()
        if (
            source_stat_after.st_size != source_stat.st_size
            or source_stat_after.st_mtime_ns != source_stat.st_mtime_ns
            or source_stat_after.st_ino != source_stat.st_ino
        ):
            raise RuntimeError("Source fingerprint DSS changed while it was read")
        cube = np.stack(frames)
        return cube, {
            "source_dss": {
                "path": str(source),
                "size_bytes": source_stat.st_size,
                "sha256": source_sha256,
                "integrity_verification": {
                    "pre_read": ["sha256", "size", "mtime_ns", "file_id"],
                    "post_read": ["size", "mtime_ns", "file_id"],
                    "status": "unchanged",
                },
            },
            "source_grid": normalized_grid,
            "pathname_selector": pathname_selector,
            "record_count": len(records),
            "start": model_start.isoformat(timespec="seconds"),
            "end": model_end.isoformat(timespec="seconds"),
            "interval_minutes": interval_minutes,
            "first_pathname": pathname_evidence[0],
            "last_pathname": pathname_evidence[-1],
            "cube_sha256": _array_sha256(cube),
        }

    @staticmethod
    @log_call
    def export_excess_to_grid(
        hms_result_hdf: str | Path,
        hms_basin_sqlite: str | Path,
        source_dss: str | Path,
        source_pathname_selector: str,
        source_grid_definition: Mapping[str, Any],
        target_grid_definition: Mapping[str, Any],
        output_dss: str | Path,
        output_pathname_selector: str,
        *,
        model_start: datetime,
        model_end: datetime,
        model_interval_minutes: int,
        source_interval_minutes: int,
        excess_depth_units: str,
        source_value_multiplier: float,
        fingerprint_tolerance: float = 1.0e-6,
    ) -> dict[str, Any]:
        """Export one immutable, qualification-only RAS-grid excess product."""
        output = Path(output_dss).resolve()
        audit_path = output.with_suffix(".audit.json")
        manifest_path = output.with_suffix(".manifest.json")
        for destination in (output, audit_path, manifest_path):
            if destination.exists():
                raise FileExistsError(f"Refusing to overwrite: {destination}")
        if model_interval_minutes <= 0 or source_interval_minutes <= 0:
            raise ValueError("Transfer intervals must be positive")
        if source_interval_minutes % model_interval_minutes:
            raise ValueError("Source interval must be a whole model-interval multiple")
        HmsSpatialTransfer._validate_family_selector(output_pathname_selector)
        source_cube, source_evidence = HmsSpatialTransfer.read_fingerprint_cube(
            source_dss,
            source_pathname_selector,
            source_grid_definition,
            model_start=model_start,
            model_end=model_end,
            interval_minutes=source_interval_minutes,
        )
        stride = source_interval_minutes // model_interval_minutes
        audit, transferred = HmsSpatialTransfer._transfer_excess_to_grid(
            hms_result_hdf,
            hms_basin_sqlite,
            source_cube,
            source_grid_definition,
            target_grid_definition,
            excess_depth_units=excess_depth_units,
            fingerprint_stride=stride,
            source_value_multiplier=source_value_multiplier,
            fingerprint_tolerance=fingerprint_tolerance,
        )
        target = audit["target_grid"]
        rows, columns = target["shape"]
        frames = transferred.reshape((len(transferred), rows, columns))
        expected_steps = int((model_end - model_start).total_seconds() // 60)
        if expected_steps % model_interval_minutes:
            raise ValueError("Model window is not transfer-interval aligned")
        expected_steps //= model_interval_minutes
        if len(frames) != expected_steps:
            raise ValueError(
                f"Transferred frame count is {len(frames)}, expected {expected_steps}"
            )
        boundaries = [
            model_start + timedelta(minutes=model_interval_minutes * index)
            for index in range(expected_steps + 1)
        ]
        output.parent.mkdir(parents=True, exist_ok=True)
        write_result = HmsSpatialTransfer._write_grid_subprocess(
            output,
            output_pathname_selector,
            frames,
            boundaries,
            {
                "cell_size": target["cell_size_meters"],
                "origin": target["origin"],
                "crs": target["crs"],
                "units": str(excess_depth_units).upper(),
                "data_type": "PER-CUM",
                "compression": "PRECIP_2_BYTE",
                "interval_minutes": model_interval_minutes,
            },
        )
        if write_result["record_count"] != expected_steps or not output.is_file():
            raise RuntimeError("RAS-grid excess DSS write did not produce every frame")
        HmsSpatialTransfer.write_audit(audit, audit_path)
        manifest: dict[str, Any] = {
            "schema": "hms-commander/gridded-excess-product/1.0",
            "status": "qualification_only",
            "forecast_eligible": False,
            "method": audit["method"],
            "source": source_evidence,
            "hms_result_hdf": {
                "path": str(Path(hms_result_hdf).resolve()),
                "sha256": _sha256_file(Path(hms_result_hdf).resolve()),
            },
            "hms_basin_sqlite": {
                "path": str(Path(hms_basin_sqlite).resolve()),
                "sha256": _sha256_file(Path(hms_basin_sqlite).resolve()),
            },
            "target_grid": target,
            "audit": {
                "path": str(audit_path),
                "sha256": _sha256_file(audit_path),
                "audit_sha256": audit["audit_sha256"],
            },
            "output": {
                "path": str(output),
                "size_bytes": output.stat().st_size,
                "sha256": _sha256_file(output),
                "pathname_selector": output_pathname_selector,
                "record_count": write_result["record_count"],
                "first_pathname": write_result["first_pathname"],
                "last_pathname": write_result["last_pathname"],
                "start": model_start.isoformat(timespec="seconds"),
                "end": model_end.isoformat(timespec="seconds"),
                "interval_minutes": model_interval_minutes,
                "units": str(excess_depth_units).upper(),
                "data_type": "PER-CUM",
            },
            "metrics": audit["metrics"],
        }
        manifest["manifest_sha256"] = _canonical_sha256(manifest)
        content = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
        temporary = manifest_path.with_suffix(manifest_path.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(manifest_path)
        return manifest

    @staticmethod
    def _write_grid_subprocess(
        output: Path,
        pathname: str,
        frames: np.ndarray,
        boundaries: list[datetime],
        grid_info: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Write grid data in a child so native DSS locks end before hashing."""
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=f".{output.stem}-writer-",
            dir=output.parent,
        ) as stage_name:
            stage = Path(stage_name)
            data_path = stage / "frames.npy"
            request_path = stage / "request.json"
            result_path = stage / "result.json"
            np.save(data_path, np.asarray(frames, dtype=np.float32), allow_pickle=False)
            request = {
                "schema": "hms-commander/grid-writer-request/1.0",
                "data": {
                    "path": str(data_path),
                    "sha256": _sha256_file(data_path),
                },
                "output_dss": str(output),
                "pathname": pathname,
                "times": [value.isoformat(timespec="seconds") for value in boundaries],
                "grid_info": dict(grid_info),
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
    def _validate_family_selector(pathname: str) -> None:
        parts = HmsSpatialTransfer._pathname_parts(pathname)
        if parts[3] or parts[4] or not all(parts[index] for index in (0, 1, 2, 5)):
            raise ValueError(
                "Grid pathname selector requires A/B/C/F and blank D/E parts"
            )

    @staticmethod
    def _grid_time(value: str) -> datetime:
        try:
            date_part, clock_part = value.split(":", maxsplit=1)
            if clock_part == "2400":
                return datetime.strptime(date_part, "%d%b%Y") + timedelta(days=1)
            return datetime.strptime(value, "%d%b%Y:%H%M")
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Unsupported DSS grid time part: {value!r}") from exc

    @staticmethod
    @log_call
    def write_audit(audit: Mapping[str, Any], output_path: str | Path) -> Path:
        """Write an audit idempotently and refuse different replacement."""
        destination = Path(output_path).resolve()
        expected = str(audit.get("audit_sha256", ""))
        unsigned = dict(audit)
        unsigned.pop("audit_sha256", None)
        if not expected or expected != _canonical_sha256(unsigned):
            raise ValueError("spatial-transfer audit hash is missing or invalid")
        content = json.dumps(audit, indent=2, sort_keys=True) + "\n"
        if destination.exists():
            if destination.read_text(encoding="utf-8") == content:
                return destination
            raise FileExistsError(
                f"Refusing to replace a different spatial-transfer audit: "
                f"{destination}"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(destination)
        return destination
