"""Tests for volume-conserving HMS-subbasin transfer maps."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from importlib.resources import files

import geopandas as gpd
import pytest
from shapely.geometry import box

from hms_commander import HmsSubbasinTransfer

pytestmark = pytest.mark.requires_gis


def _canonical_sha256(value: dict[str, object]) -> str:
    content = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def _target_grid() -> dict[str, object]:
    grid: dict[str, object] = {
        "definition_id": "asymmetric-2x3-grid",
        "crs": "EPSG:3857",
        "shape": [2, 3],
        "cell_size_meters": 10.0,
        "cell_area_square_meters": 100.0,
        "origin": [0.0, 0.0],
        "row_order": "south_to_north",
    }
    grid["definition_sha256"] = _canonical_sha256(grid)
    return grid


def _application_area() -> dict[str, object]:
    grid = _target_grid()
    effective_areas = [100.0, 100.0, 50.0, 0.0, 100.0, 25.0]
    cells = []
    for cell_id, effective_area in enumerate(effective_areas):
        row, column = divmod(cell_id, 3)
        if effective_area == 0:
            membership = "outside"
        elif effective_area == 100:
            membership = "inside"
        else:
            membership = "partial"
        cells.append(
            {
                "cell_id": cell_id,
                "row_index": row,
                "column_index": column,
                "center": [column * 10.0 + 5.0, row * 10.0 + 5.0],
                "bounds": [
                    column * 10.0,
                    row * 10.0,
                    column * 10.0 + 10.0,
                    row * 10.0 + 10.0,
                ],
                "membership": membership,
                "effective_area_square_meters": effective_area,
            }
        )
    artifact: dict[str, object] = {
        "schema": "ras-commander/precipitation-application-area/1.0",
        "method": "ras-mesh-effective-area",
        "algorithm": "target-cell-intersection-with-mesh-union-v1",
        "model": {
            "project_id": "ras-project",
            "plan_id": "p01",
            "geometry_id": "g01",
            "two_d_flow_area": "Receiving Area",
        },
        "source_geometry_hdf": {
            "name": "fixture.g01.hdf",
            "size_bytes": 123,
            "sha256": "a" * 64,
        },
        "source_mesh_crs": "EPSG:3857",
        "source_mesh_cells_sha256": "b" * 64,
        "target_grid": grid,
        "spatial_reference": {
            "horizontal_crs": "EPSG:3857",
            "horizontal_datum": "World Geodetic System 1984",
            "horizontal_units": "meters",
            "vertical_datum": "not_applicable",
        },
        "boundary_predicate": "positive-area-intersection",
        "area_precision_decimal_places": 9,
        "compiler": {"shapely_version": "fixture", "geos_version": "fixture"},
        "cells": cells,
        "metrics": {
            "mesh_cell_count": 3,
            "mesh_union_area_square_meters": 375.0,
            "target_cell_count": 6,
            "inside_target_cell_count": 3,
            "partial_target_cell_count": 2,
            "outside_target_cell_count": 1,
            "receiving_area_square_meters": 375.0,
            "target_grid_area_square_meters": 600.0,
        },
    }
    artifact["application_area_sha256"] = _canonical_sha256(artifact)
    return artifact


def _subbasins() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {
            "subbasin": ["A", "B"],
            "source_area": [1.0, 0.001],
            "source_area_units": ["acres", "square kilometers"],
        },
        geometry=[box(0.0, 0.0, 10.0, 20.0), box(10.0, 0.0, 20.0, 20.0)],
        crs="EPSG:3857",
    )


def _hms_model() -> dict[str, object]:
    return {
        "project_id": "hms-project",
        "basin_model_id": "basin-1",
        "basin_file": {
            "name": "fixture.basin",
            "size_bytes": 100,
            "sha256": "c" * 64,
        },
        "geometry_source": {
            "name": "fixture.gpkg",
            "size_bytes": 200,
            "sha256": "d" * 64,
        },
    }


def _compile(
    subbasins: gpd.GeoDataFrame | None = None,
) -> dict[str, object]:
    return HmsSubbasinTransfer.compile_transfer_map(
        _subbasins() if subbasins is None else subbasins,
        _application_area(),
        ["B", "A"],
        _hms_model(),
    )


def test_compile_assigns_asymmetric_grid_and_conserves_each_subbasin() -> None:
    artifact = _compile()

    assert artifact["schema"] == ("hms-commander/subbasin-volume-transfer-map/1.0")
    assert artifact["method"] == "hms-subbasin-volume-conserving-v1"
    assert artifact["algorithm"] == (
        "target-center-subbasin-coverage-effective-area-scaling-v1"
    )
    assert [item["subbasin"] for item in artifact["subbasins"]] == ["A", "B"]
    assert [cell["subbasin"] for cell in artifact["cells"]] == [
        "A",
        "B",
        None,
        None,
        "B",
        None,
    ]

    by_name = {item["subbasin"]: item for item in artifact["subbasins"]}
    assert by_name["A"]["source_area"] == {
        "declared_value": 1.0,
        "declared_units": "ACRES",
        "square_meters": 4046.8564224,
    }
    assert by_name["A"]["receiving_area_square_meters"] == 100.0
    assert by_name["B"]["source_area"]["square_meters"] == 1000.0
    assert by_name["B"]["receiving_area_square_meters"] == 200.0

    for name, subbasin in by_name.items():
        output_area = sum(
            cell["effective_area_square_meters"] * cell["depth_multiplier"]
            for cell in artifact["cells"]
            if cell["subbasin"] == name
        )
        assert output_area == pytest.approx(subbasin["source_area"]["square_meters"])
    assert artifact["metrics"] == {
        "selected_subbasin_count": 2,
        "target_cell_count": 6,
        "assigned_target_cell_count": 3,
        "unassigned_positive_area_cell_count": 2,
        "source_area_square_meters": 5046.8564224,
        "assigned_receiving_area_square_meters": 300.0,
        "unassigned_positive_area_square_meters": 75.0,
    }


def test_compile_is_deterministic_and_write_is_idempotent(tmp_path) -> None:
    first = _compile()
    second = _compile()
    assert first == second

    destination = tmp_path / "transfer-map.json"
    assert HmsSubbasinTransfer.write_transfer_map(first, destination) == destination
    assert HmsSubbasinTransfer.write_transfer_map(second, destination) == destination
    assert json.loads(destination.read_text(encoding="utf-8")) == first

    changed = deepcopy(first)
    changed["hms_model"]["project_id"] = "different"
    with pytest.raises(ValueError, match="transfer_map_sha256 is missing or invalid"):
        HmsSubbasinTransfer.write_transfer_map(changed, tmp_path / "changed.json")


def test_compile_rejects_invalid_ras_application_area_hash() -> None:
    application_area = _application_area()
    application_area["cells"][0]["effective_area_square_meters"] = 99.0
    with pytest.raises(ValueError, match="application-area hash"):
        HmsSubbasinTransfer.compile_transfer_map(
            _subbasins(),
            application_area,
            ["A", "B"],
            _hms_model(),
        )


def test_compile_rejects_invalid_target_grid_hash_even_if_artifact_is_rehashed() -> (
    None
):
    application_area = _application_area()
    application_area["target_grid"]["definition_sha256"] = "0" * 64
    application_area.pop("application_area_sha256")
    application_area["application_area_sha256"] = _canonical_sha256(application_area)

    with pytest.raises(ValueError, match="target_grid definition_sha256"):
        HmsSubbasinTransfer.compile_transfer_map(
            _subbasins(),
            application_area,
            ["A", "B"],
            _hms_model(),
        )


def test_compile_rejects_ambiguous_center_assignment() -> None:
    subbasins = _subbasins()
    subbasins.geometry = [
        box(0.0, 0.0, 15.0, 20.0),
        box(15.0, 0.0, 30.0, 20.0),
    ]
    with pytest.raises(ValueError, match="ambiguous HMS subbasin support"):
        _compile(subbasins)


def test_compile_rejects_positive_area_overlap() -> None:
    subbasins = _subbasins()
    subbasins.geometry = [
        box(0.0, 0.0, 20.0, 20.0),
        box(10.0, 0.0, 30.0, 20.0),
    ]
    with pytest.raises(ValueError, match="overlap"):
        _compile(subbasins)


def test_compile_rejects_selected_subbasin_without_support() -> None:
    subbasins = _subbasins()
    subbasins.geometry = [
        box(0.0, 0.0, 10.0, 20.0),
        box(100.0, 100.0, 110.0, 110.0),
    ]
    with pytest.raises(ValueError, match="has no RAS receiving support"):
        _compile(subbasins)


def test_compile_rejects_unsupported_or_implicit_area_units() -> None:
    subbasins = _subbasins()
    subbasins.loc[subbasins["subbasin"] == "A", "source_area_units"] = "feet"
    with pytest.raises(ValueError, match="area units are unsupported"):
        _compile(subbasins)


def test_validate_rejects_denominator_drift() -> None:
    artifact = deepcopy(_compile())
    artifact["subbasins"][0]["receiving_area_square_meters"] = 101.0
    with pytest.raises(ValueError, match="denominator is inconsistent"):
        HmsSubbasinTransfer.validate_transfer_map(artifact)


def test_compile_records_source_crs_when_reprojection_is_required() -> None:
    subbasins = _subbasins().to_crs("EPSG:3395")
    artifact = _compile(subbasins)

    assert artifact["source_subbasin_crs"] == "EPSG:3395"
    assert [cell["subbasin"] for cell in artifact["cells"]] == [
        "A",
        "B",
        None,
        None,
        "B",
        None,
    ]


def test_packaged_schema_matches_public_contract() -> None:
    schema_path = files("hms_commander.contracts").joinpath(
        "subbasin-volume-transfer-map-v1.0.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    assert schema["$id"] == HmsSubbasinTransfer.SCHEMA
    assert schema["properties"]["method"]["const"] == (HmsSubbasinTransfer.METHOD)
