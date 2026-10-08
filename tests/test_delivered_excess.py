"""Regression coverage for an explicitly distinct delivered publication method."""

import json
from datetime import datetime, timedelta
from importlib.resources import files

import geopandas as gpd
import numpy as np
import pytest
from shapely.geometry import box

from hms_commander import HmsSubbasinTransfer
from hms_commander.HmsSubbasinTransfer import _volume_evidence
from test_subbasin_transfer import (
    _canonical_sha256,
    _compile,
    _hms_model,
    _mock_source_dss,
    _target_grid,
)


def _center_map(origin):
    from ras_commander.precip import PrecipitationApplicationArea

    x, y = origin
    grid = _target_grid()
    grid.pop("definition_sha256")
    grid["origin"] = origin
    mesh = gpd.GeoDataFrame(
        {"mesh_name": ["Area"], "cell_id": [1]},
        geometry=[box(x, y, x + 30, y + 20)],
        crs="EPSG:3857",
    )
    area = PrecipitationApplicationArea.compile_from_mesh_cells(
        mesh,
        "Area",
        grid,
        project_id="ras",
        plan_id="p01",
        geometry_id="g01",
        source_geometry_hdf={
            "name": "fixture.hdf",
            "size_bytes": 1,
            "sha256": "a" * 64,
        },
        method=PrecipitationApplicationArea.CENTER_METHOD,
    )
    subbasins = gpd.GeoDataFrame(
        {"subbasin": ["A", "B"]},
        geometry=[box(x, y, x + 10, y + 20), box(x + 10, y, x + 30, y + 20)],
        crs="EPSG:3857",
    )
    return HmsSubbasinTransfer.compile_transfer_map(
        subbasins,
        area,
        ["A", "B"],
        _hms_model(),
        method=HmsSubbasinTransfer.CENTER_METHOD,
    )


@pytest.mark.parametrize(
    "origin,snapped",
    [
        ([3.25, -2.75], [0.0, -10.0]),
        ([0.0, 0.0], [0.0, 0.0]),
        ([-13.25, 28.75], [-20.0, 20.0]),
    ],
)
def test_delivered_publication_preserves_values_and_reports_actual_shift(
    tmp_path, monkeypatch, origin, snapped
):
    source = _mock_source_dss(tmp_path, monkeypatch)
    mapping = _center_map(origin)
    captured = []

    def writer(destination, pathname, frames, boundaries, grid_info, **kwargs):
        captured.append((frames.copy(), boundaries, grid_info))
        destination.write_bytes(b"verified DSS")
        return {
            "record_count": len(frames),
            "first_pathname": "first",
            "last_pathname": "last",
            "verification": {
                "status": "verified",
                "absolute_value_tolerance": 0.01,
                "maximum_absolute_value_difference": 0.0,
                "weighted_depth_area_by_support": {
                    name: (values / 0.0254).tolist()
                    for name, values in kwargs["source_volumes"].items()
                },
            },
            "published_volume_evidence": _volume_evidence(
                kwargs["source_volumes"],
                kwargs["source_volumes"],
                kwargs["interval_ends"],
                kwargs["volume_tolerance"],
            ),
        }

    monkeypatch.setattr(
        HmsSubbasinTransfer, "_write_grid_subprocess", staticmethod(writer)
    )
    options = dict(
        source_a_part="",
        source_run_name="Accepted",
        model_start=datetime(2020, 1, 1),
        model_end=datetime(2020, 1, 1, 0, 15),
        interval_minutes=5,
        source_depth_units="IN",
        volume_tolerance={"absolute_cubic_meters": 1e-6, "relative_fraction": 1e-12},
        readback_absolute_value_tolerance=0.01,
    )
    normal = HmsSubbasinTransfer.apply_transfer_map_to_dss(
        source,
        mapping,
        tmp_path / "normal.dss",
        "/SHG/BASIN/PRECIPITATION///EXCESS/",
        **options,
    )
    delivered = HmsSubbasinTransfer.apply_delivered_transfer_map_to_dss(
        source,
        mapping,
        tmp_path / "delivered.dss",
        "/SHG/BASIN/PRECIPITATION///EXCESS/",
        **options,
    )
    np.testing.assert_array_equal(captured[0][0], captured[1][0])
    assert captured[0][2]["origin"] == origin
    assert captured[1][2]["origin"] == snapped
    assert captured[1][1] == [t + timedelta(minutes=5) for t in captured[0][1]]
    assert normal["output"]["start"] == "2020-01-01T00:00:00"
    assert normal["method"] == HmsSubbasinTransfer.CENTER_METHOD
    assert "publication" not in normal
    assert delivered["method"] == HmsSubbasinTransfer.DELIVERED_METHOD
    assert delivered["output"]["start"] == "2020-01-01T00:05:00"
    assert delivered["output"]["end"] == "2020-01-01T00:20:00"
    assert delivered["publication"]["allocation_origin"] == origin
    assert delivered["publication"]["dss_origin"] == snapped
    assert (
        delivered["publication"]["source_model_window"]["start"]
        == normal["output"]["start"]
    )
    assert delivered["forecast_eligible"] is False
    for name, document in (
        ("subbasin-volume-excess-product", delivered),
        (
            "subbasin-volume-transfer-audit",
            json.loads((tmp_path / "delivered.audit.json").read_text()),
        ),
    ):
        schema = json.loads(
            files("hms_commander")
            .joinpath(f"contracts/{name}-v3.0.schema.json")
            .read_text()
        )
        pytest.importorskip("jsonschema").validate(document, schema)
        key = "manifest_sha256" if "product" in name else "audit_sha256"
        unsigned = dict(document)
        assert unsigned.pop(key) == _canonical_sha256(unsigned)
    with pytest.raises(FileExistsError):
        HmsSubbasinTransfer.apply_delivered_transfer_map_to_dss(
            source,
            mapping,
            tmp_path / "delivered.dss",
            "/SHG/BASIN/PRECIPITATION///EXCESS/",
            **options,
        )
    with pytest.raises(ValueError, match="requires a centroid/full-cell"):
        HmsSubbasinTransfer.apply_delivered_transfer_map_to_dss(
            source,
            _compile(),
            tmp_path / "invalid.dss",
            "/SHG/BASIN/PRECIPITATION///EXCESS/",
            **options,
        )
