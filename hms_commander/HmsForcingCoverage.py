"""Raw geometric coverage of selected HMS computation cells by forcing masks."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping
from typing import Any

import numpy as np

from .Decorators import log_call
from .LoggingConfig import get_logger

logger = get_logger(__name__)


def _digest(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            payload, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def _grid(value: Mapping[str, Any]) -> dict[str, Any]:
    from pyproj import CRS

    required = {
        "definition_id",
        "crs",
        "shape",
        "origin",
        "cell_size_meters",
        "row_order",
    }
    if not isinstance(value, Mapping) or not required.issubset(value):
        raise ValueError(
            "grid_definition requires ID, CRS, shape, origin, cell size and row order"
        )
    shape = value["shape"]
    if (
        not isinstance(shape, (list, tuple))
        or len(shape) != 2
        or any(isinstance(v, bool) or not isinstance(v, int) or v <= 0 for v in shape)
    ):
        raise ValueError("grid shape must contain two positive integers")
    if (
        not isinstance(value["definition_id"], str)
        or not value["definition_id"].strip()
    ):
        raise ValueError("grid definition_id must be nonempty")
    if value["row_order"] != "south_to_north":
        raise ValueError("grid row_order must be south_to_north")
    try:
        origin = [float(v) for v in value["origin"]]
        size = float(value["cell_size_meters"])
    except (TypeError, ValueError) as exc:
        raise ValueError("grid origin and cell size must be numeric") from exc
    if len(origin) != 2 or not all(math.isfinite(v) for v in origin):
        raise ValueError("grid origin must contain two finite coordinates")
    if (
        isinstance(value["cell_size_meters"], bool)
        or not math.isfinite(size)
        or size <= 0
    ):
        raise ValueError("grid cell size must be finite and positive")
    crs = CRS.from_user_input(value["crs"])
    if (
        not crs.is_projected
        or not crs.axis_info
        or any(
            axis.unit_conversion_factor != 1
            or axis.unit_name.lower() not in {"metre", "meter"}
            for axis in crs.axis_info
        )
    ):
        raise ValueError("grid CRS must be projected with meter axes")
    return {
        "definition_id": value["definition_id"],
        "crs": crs.to_wkt(),
        "shape": list(shape),
        "origin": origin,
        "cell_size_meters": size,
        "row_order": "south_to_north",
    }


class HmsForcingCoverage:
    """Audit forcing support without changing inputs or assigning acceptance."""

    @staticmethod
    @log_call
    def audit_grid_mask(
        computation_cells: Any,
        *,
        selected_subbasins: Iterable[str],
        grid_definition: Mapping[str, Any],
        valid_mask: np.ndarray,
    ) -> dict[str, Any]:
        """Intersect selected computation-cell polygons with a finite-data mask.

        Args:
            computation_cells: GeoDataFrame from HmsSqlite.get_discretization(),
                with exact subbasin names and polygon geometry in a declared CRS.
            selected_subbasins: Explicit, unique active basin subbasin names.
                Unselected SQLite rows are excluded and counted separately.
            grid_definition: Grid ID, projected metric CRS, shape (rows, columns),
                lower-left outer-edge origin, cell size in meters and
                south_to_north row order. No implicit axis flip is performed.
            valid_mask: Boolean array shaped like the grid. True means finite
                precipitation, including zero; False means missing data.

        Returns:
            Content-hashed raw audit with per-subbasin and aggregate areas in
            square meters: covered, inside-grid no-data and outside-grid.
            Areas sum computation-cell polygons, not a cross-subbasin union or
            the model's parameter area. Missing subbasins remain explicit.
            This geometric audit does not infer the engine's sampling kernel,
            qualify rainfall amounts or grant hydraulic/library acceptance.

        Raises:
            ImportError: Optional GIS dependencies are unavailable.
            ValueError: Names, mask, grid or selected cell geometry is invalid,
                or no selected computation cells exist.
        """
        import geopandas as gpd
        import shapely

        grid = _grid(grid_definition)
        mask = np.asarray(valid_mask)
        if mask.dtype != np.bool_ or mask.shape != tuple(grid["shape"]):
            raise ValueError("valid_mask must be a boolean array matching grid shape")
        if isinstance(selected_subbasins, (str, bytes)):
            raise ValueError("selected_subbasins must be an iterable of exact names")
        names = list(selected_subbasins)
        if not names or any(not isinstance(v, str) or not v.strip() for v in names):
            raise ValueError("selected_subbasins must contain nonempty names")
        if len(set(names)) != len(names):
            raise ValueError("selected_subbasins must not contain duplicates")
        names = sorted(names)
        if (
            not isinstance(computation_cells, gpd.GeoDataFrame)
            or "subbasin" not in computation_cells
        ):
            raise ValueError(
                "computation_cells requires a GeoDataFrame with subbasin names"
            )
        if computation_cells.crs is None:
            raise ValueError("computation_cells must declare a CRS")
        if any(
            not isinstance(v, str) or not v.strip()
            for v in computation_cells["subbasin"]
        ):
            raise ValueError("computation cell subbasin names must be nonempty strings")
        selected = computation_cells["subbasin"].isin(names)
        working = computation_cells.loc[selected, ["subbasin", "geometry"]].copy()
        if working.empty:
            raise ValueError("No selected computation cells exist")
        if (
            working.geometry.isna().any()
            or working.is_empty.any()
            or not working.is_valid.all()
            or not working.geom_type.isin(["Polygon", "MultiPolygon"]).all()
        ):
            raise ValueError(
                "Selected computation cells must be valid nonempty polygons"
            )
        working = working.to_crs(grid["crs"])
        polygons = working.geometry.to_numpy()
        areas = shapely.area(polygons)
        if (
            not np.isfinite(areas).all()
            or (areas <= 0).any()
            or not shapely.is_valid(polygons).all()
        ):
            raise ValueError(
                "Projected computation cells must have valid positive finite area"
            )

        rows, columns = np.nonzero(mask)
        x0, y0 = grid["origin"]
        size = grid["cell_size_meters"]
        footprint = shapely.union_all(
            shapely.box(
                x0 + columns * size,
                y0 + rows * size,
                x0 + (columns + 1) * size,
                y0 + (rows + 1) * size,
            )
        )
        extent = shapely.box(
            x0, y0, x0 + grid["shape"][1] * size, y0 + grid["shape"][0] * size
        )
        inside = shapely.intersection(polygons, extent)
        covered = shapely.area(shapely.intersection(inside, footprint))
        missing = shapely.area(shapely.difference(inside, footprint))
        outside = shapely.area(shapely.difference(polygons, extent))
        labels = working["subbasin"].to_numpy()

        def metrics(selected_rows: np.ndarray) -> dict[str, int | float]:
            return {
                "cell_count": int(np.count_nonzero(selected_rows)),
                "cell_polygon_area_square_meters": math.fsum(areas[selected_rows]),
                "covered_area_square_meters": math.fsum(covered[selected_rows]),
                "nodata_area_square_meters": math.fsum(missing[selected_rows]),
                "outside_grid_area_square_meters": math.fsum(outside[selected_rows]),
                "cells_intersecting_nodata": int(
                    np.count_nonzero(missing[selected_rows] > 0)
                ),
                "cells_outside_grid": int(np.count_nonzero(outside[selected_rows] > 0)),
            }

        geometry_records = sorted(
            zip(
                labels.tolist(),
                shapely.to_wkb(
                    shapely.normalize(polygons), hex=True, byte_order=1
                ).tolist(),
            )
        )
        audit = {
            "schema": "hms-commander/forcing-mask-coverage/1.0",
            "method": "computation-cell-polygon-mask-intersection-v1",
            "engineering_acceptance": "not_evaluated",
            "grid_definition": grid,
            "grid_definition_sha256": _digest(grid),
            "mask_sha256": hashlib.sha256(
                np.ascontiguousarray(mask, dtype=np.uint8).tobytes()
            ).hexdigest(),
            "valid_grid_cell_count": int(mask.sum()),
            "selected_subbasins": names,
            "missing_discretization_subbasins": sorted(set(names) - set(labels)),
            "inactive_cell_count": int((~selected).sum()),
            "inactive_subbasins": sorted(
                set(computation_cells.loc[~selected, "subbasin"])
            ),
            "selected_geometry_sha256": _digest(geometry_records),
            "metrics": metrics(np.ones(len(working), dtype=bool)),
            "subbasins": [
                {"subbasin": name, **metrics(labels == name)} for name in names
            ],
        }
        audit["audit_sha256"] = _digest(audit)
        logger.info(
            "Audited %s selected HMS cells against the forcing mask", len(working)
        )
        return audit
