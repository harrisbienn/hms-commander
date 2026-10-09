"""Analytic geometry regressions for the package-owned forcing support audit."""

import copy

import numpy as np
import pytest

gpd = pytest.importorskip("geopandas")
shapely = pytest.importorskip("shapely")

from hms_commander import HmsForcingCoverage

pytestmark = pytest.mark.requires_gis


@pytest.fixture
def grid():
    return {
        "definition_id": "test-grid",
        "crs": "EPSG:5070",
        "shape": [2, 2],
        "origin": [0, 0],
        "cell_size_meters": 10,
        "row_order": "south_to_north",
    }


@pytest.fixture
def cells():
    return gpd.GeoDataFrame(
        {
            "subbasin": ["south", "north", "edge", "inactive"],
            "geometry": [
                shapely.box(0, 0, 10, 10),
                shapely.box(0, 10, 10, 20),
                shapely.box(15, 0, 25, 10),
                shapely.box(100, 100, 110, 110),
            ],
        },
        crs="EPSG:5070",
    )


def audit(cells, grid, mask, names=("south", "north", "edge")):
    return HmsForcingCoverage.audit_grid_mask(
        cells,
        selected_subbasins=names,
        grid_definition=grid,
        valid_mask=mask,
    )


def test_separates_nodata_extent_and_inactive_cells(cells, grid):
    result = audit(cells, grid, np.array([[True, True], [False, True]]))
    metrics = result["metrics"]
    assert metrics == {
        "cell_count": 3,
        "cell_polygon_area_square_meters": 300.0,
        "covered_area_square_meters": 150.0,
        "nodata_area_square_meters": 100.0,
        "outside_grid_area_square_meters": 50.0,
        "cells_intersecting_nodata": 1,
        "cells_outside_grid": 1,
    }
    by_name = {row["subbasin"]: row for row in result["subbasins"]}
    assert by_name["south"]["covered_area_square_meters"] == 100
    assert by_name["north"]["nodata_area_square_meters"] == 100
    assert result["inactive_cell_count"] == 1
    assert result["inactive_subbasins"] == ["inactive"]
    assert result["engineering_acceptance"] == "not_evaluated"


@pytest.mark.parametrize("finite,covered,missing", [(True, 250, 0), (False, 0, 250)])
def test_full_and_empty_masks_preserve_outside_grid(
    cells, grid, finite, covered, missing
):
    result = audit(cells, grid, np.full((2, 2), finite))
    assert result["metrics"]["covered_area_square_meters"] == covered
    assert result["metrics"]["nodata_area_square_meters"] == missing
    assert result["metrics"]["outside_grid_area_square_meters"] == 50


def test_missing_discretization_is_explicit_and_not_a_zero_area_success(cells, grid):
    result = audit(cells, grid, np.ones((2, 2), dtype=bool), names=("south", "missing"))
    assert result["missing_discretization_subbasins"] == ["missing"]
    assert result["subbasins"][0]["cell_count"] == 0
    with pytest.raises(ValueError, match="No selected"):
        audit(cells, grid, np.ones((2, 2), dtype=bool), names=("missing",))


def test_audit_is_order_independent_and_inputs_unchanged(cells, grid):
    mask = np.ones((2, 2), dtype=bool)
    before = cells.copy(deep=True)
    grid_before = copy.deepcopy(grid)
    first = audit(cells, grid, mask)
    second = audit(cells.iloc[::-1], grid, mask, names=("edge", "south", "north"))
    assert first == second
    assert cells.equals(before) and grid == grid_before and mask.all()
    changed = mask.copy()
    changed[0, 0] = False
    assert audit(cells, grid, changed)["audit_sha256"] != first["audit_sha256"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("shape", [2.5, 2]),
        ("shape", [True, 2]),
        ("shape", [2, 0]),
        ("origin", [0, float("nan")]),
        ("origin", [0]),
        ("cell_size_meters", 0),
        ("cell_size_meters", True),
        ("row_order", "north_to_south"),
        ("crs", "EPSG:4326"),
        ("crs", "EPSG:2278"),
        ("definition_id", ""),
    ],
)
def test_rejects_invalid_or_ambiguous_grid(cells, grid, field, value):
    grid[field] = value
    with pytest.raises(ValueError):
        audit(cells, grid, np.ones((2, 2), dtype=bool))


@pytest.mark.parametrize(
    "mask", [np.ones((2, 2)), np.array([[True]]), np.zeros((2, 2), dtype=int)]
)
def test_requires_explicit_boolean_mask(cells, grid, mask):
    with pytest.raises(ValueError, match="boolean"):
        audit(cells, grid, mask)


@pytest.mark.parametrize("names", [[], ["south", "south"], "south", [""]])
def test_requires_explicit_unique_subbasins(cells, grid, names):
    with pytest.raises(ValueError):
        audit(cells, grid, np.ones((2, 2), dtype=bool), names=names)


def test_rejects_invalid_selected_geometry_but_excludes_historical_rows(cells, grid):
    cells.loc[0, "geometry"] = shapely.Polygon([(0, 0), (10, 10), (0, 10), (10, 0)])
    with pytest.raises(ValueError, match="valid nonempty polygons"):
        audit(cells, grid, np.ones((2, 2), dtype=bool))
    result = audit(cells, grid, np.ones((2, 2), dtype=bool), names=("north",))
    assert result["metrics"]["covered_area_square_meters"] == 100


def test_reprojects_input_cells_without_changing_original(cells, grid):
    projected = cells.to_crs("EPSG:3857")
    result = audit(projected, grid, np.ones((2, 2), dtype=bool))
    assert result["metrics"]["covered_area_square_meters"] == pytest.approx(
        250, abs=1e-5
    )
    assert projected.crs.to_epsg() == 3857
