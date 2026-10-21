"""Geometry export boundaries plus real HMS example integration."""

from pathlib import Path

import pytest

gpd = pytest.importorskip("geopandas")
shapely = pytest.importorskip("shapely")
from shapely.geometry import Polygon, box

from hms_commander import HmsSqlite
from hms_commander._subbasin_regions import prepare_regions

pytestmark = pytest.mark.requires_gis


def frame(names, geometries, crs="EPSG:5070"):
    return gpd.GeoDataFrame({"name": names}, geometry=geometries, crs=crs)


def test_exact_selection_partition_and_order():
    polygons = frame(
        ["east", "unused", "west"],
        [box(1, 0, 2, 1), box(9, 9, 10, 10), box(0, 0, 1, 1)],
    )
    result = prepare_regions(
        polygons, ["west", "east"], repair="reject", basin_id="basin"
    )
    assert result["audit"]["basin_area_m2"] == 2
    assert result["audit"]["overlap_area_m2"] == 0
    assert result["audit"]["excluded_count"] == 1
    assert result["partition"]["children"] == ["east", "west"]
    assert result == prepare_regions(
        polygons.iloc[::-1], ["east", "west"], repair="reject", basin_id="basin"
    )
    assert result["engineering_review"] == "required"
    assert result["forecast_eligible"] is False


@pytest.mark.parametrize(
    "names,rows,match",
    [
        (["missing"], ["west"], "Missing"),
        (["west", "west"], ["west"], "unique"),
        (["west"], ["west", "west"], "Duplicate"),
    ],
)
def test_ambiguous_selection(names, rows, match):
    with pytest.raises(ValueError, match=match):
        prepare_regions(
            frame(rows, [box(0, 0, 1, 1)] * len(rows)),
            names,
            repair="reject",
            basin_id="basin",
        )


def test_repair_is_explicit_and_measured():
    crossing = Polygon([(0, 0), (2, 2), (0, 2), (2, 0), (0, 0)])
    polygons = frame(["crossing"], [crossing])
    with pytest.raises(ValueError, match="Invalid"):
        prepare_regions(polygons, ["crossing"], repair="reject", basin_id="basin")
    result = prepare_regions(
        polygons, ["crossing"], repair="make_valid", basin_id="basin"
    )
    assert result["audit"]["repaired_count"] == 1
    assert result["audit"]["subbasins"][0]["area_change_m2"] == 2
    assert not polygons.geometry.iloc[0].is_valid


def test_overlap_is_evidence_not_silent_acceptance():
    result = prepare_regions(
        frame(["a", "b"], [box(0, 0, 2, 1), box(1, 0, 3, 1)]),
        ["a", "b"],
        repair="reject",
        basin_id="basin",
    )
    assert result["audit"]["overlap_area_m2"] == 1
    assert result["engineering_review"] == "required"


def test_geographic_area_rejected():
    with pytest.raises(ValueError, match="metre CRS"):
        prepare_regions(
            frame(["a"], [box(0, 0, 1, 1)], "EPSG:4326"),
            ["a"],
            repair="reject",
            basin_id="basin",
        )


def test_real_example_export_is_immutable(tmp_path):
    folder = Path("hms_example_projects/river_bend")
    database = folder / "Minimum_Facility.sqlite"
    basin = folder / "Minimum_Facility.basin"
    if not database.exists() or not basin.exists():
        pytest.skip("Extracted river_bend example unavailable")
    output = tmp_path / "regions.json"
    result = HmsSqlite.export_subbasin_regions(
        database, basin, output, basin_id="river-bend", repair="make_valid"
    )
    assert result["audit"]["selected_count"] == 2
    assert result == HmsSqlite.export_subbasin_regions(
        database, basin, output, basin_id="river-bend", repair="make_valid"
    )
    with pytest.raises(FileExistsError):
        HmsSqlite.export_subbasin_regions(
            database, basin, output, basin_id="different", repair="make_valid"
        )
    with pytest.raises(ValueError, match="source"):
        HmsSqlite.export_subbasin_regions(
            database, basin, database, basin_id="river-bend"
        )
