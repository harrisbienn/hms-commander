"""Tests for package-owned HMS spatial-transfer audits."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("geopandas")
pytest.importorskip("h5py")
pytest.importorskip("scipy")
from geopandas import GeoDataFrame
from h5py import File
from shapely.geometry import box

from hms_commander import HmsSpatialTransfer, HmsSqlite

pytestmark = pytest.mark.requires_gis


def _grid(definition_id: str) -> dict[str, object]:
    return {
        "definition_id": definition_id,
        "crs": "EPSG:3857",
        "shape": [2, 2],
        "cell_size_meters": 1.0,
        "cell_area_square_meters": 1.0,
        "origin": [0.0, 0.0],
        "row_order": "south_to_north",
    }


@pytest.fixture
def audit_inputs(tmp_path, monkeypatch):
    hdf_path = tmp_path / "results.h5"
    with File(hdf_path, "w") as hdf:
        group = hdf.create_group("results/Basin")
        # Result columns are intentionally reversed relative to cell order.
        group.create_dataset(
            "lwe_precipitation_rate",
            data=np.asarray([[0.2, 0.1], [0.2, 0.1]], dtype=np.float64),
        )
        excess = group.create_dataset(
            "Incremental Excess",
            data=np.asarray([[2.0, 1.0], [2.0, 1.0]], dtype=np.float64),
        )
        excess.attrs["units"] = "IN"

    sqlite_path = tmp_path / "basin.sqlite"
    sqlite_path.write_bytes(b"representative HMS SQLite identity")
    cells = GeoDataFrame(
        {"subbasin": ["Basin", "Basin"]},
        geometry=[box(0.0, 0.0, 1.0, 1.0), box(0.0, 1.0, 1.0, 2.0)],
        crs="EPSG:3857",
    )
    monkeypatch.setattr(
        HmsSqlite,
        "get_discretization",
        staticmethod(lambda _: cells),
    )
    fingerprint_cube = np.asarray(
        [
            [[0.1, 0.0], [0.2, 0.0]],
            [[0.1, 0.0], [0.2, 0.0]],
        ],
        dtype=np.float64,
    )
    return hdf_path, sqlite_path, fingerprint_cube


def test_audit_reports_support_distance_area_and_volume(audit_inputs):
    hdf_path, sqlite_path, fingerprint_cube = audit_inputs

    audit = HmsSpatialTransfer.audit_excess_to_grid(
        hdf_path,
        sqlite_path,
        fingerprint_cube,
        _grid("source-grid"),
        _grid("target-grid"),
        excess_depth_units="IN",
    )

    assert audit["schema"] == "hms-commander/spatial-transfer-audit/1.0"
    assert len(audit["audit_sha256"]) == 64
    assert audit["source_grid"]["definition_sha256"]
    assert audit["target_grid"]["definition_sha256"]
    metrics = audit["metrics"]
    assert metrics["active_hms_element_count"] == 1
    assert metrics["active_hms_cell_count"] == 2
    assert metrics["target_cell_count"] == 4
    assert metrics["direct_target_cell_count"] == 2
    assert metrics["unsupported_target_cell_count"] == 2
    assert metrics["unsupported_area_square_meters"] == pytest.approx(2.0)
    assert metrics["nearest_fill_distance"] == {
        "count": 2,
        "minimum_meters": 1.0,
        "maximum_meters": 1.0,
        "mean_meters": 1.0,
        "percentiles_meters": {
            "p50": 1.0,
            "p90": 1.0,
            "p95": 1.0,
            "p99": 1.0,
        },
    }
    volume = metrics["volume_effect"]
    assert volume["baseline"] == "outside-support-zero"
    assert volume["comparison"] == "nearest-active-hms-cell"
    assert volume["cubic_meters"]["direct_supported"] == pytest.approx(
        6.0 * 0.0254
    )
    assert volume["cubic_meters"]["nearest_filled_assigned"] == pytest.approx(
        6.0 * 0.0254
    )
    assert volume["incremental_percent_of_comparison_total"] == pytest.approx(
        50.0
    )


def test_audit_is_deterministic_and_writer_reuses_identical_content(
    audit_inputs,
    tmp_path,
):
    hdf_path, sqlite_path, fingerprint_cube = audit_inputs
    arguments = (
        hdf_path,
        sqlite_path,
        fingerprint_cube,
        _grid("source-grid"),
        _grid("target-grid"),
    )

    first = HmsSpatialTransfer.audit_excess_to_grid(
        *arguments, excess_depth_units="IN"
    )
    second = HmsSpatialTransfer.audit_excess_to_grid(
        *arguments, excess_depth_units="IN"
    )
    assert first == second

    output = tmp_path / "spatial-transfer-audit.json"
    assert HmsSpatialTransfer.write_audit(first, output) == output
    assert HmsSpatialTransfer.write_audit(second, output) == output
    assert json.loads(output.read_text(encoding="utf-8")) == first

    changed = deepcopy(first)
    changed["metrics"]["unsupported_target_cell_count"] = 0
    with pytest.raises(ValueError, match="hash is missing or invalid"):
        HmsSpatialTransfer.write_audit(changed, tmp_path / "changed.json")


def test_audit_fails_closed_on_ambiguous_target_support(audit_inputs):
    hdf_path, sqlite_path, fingerprint_cube = audit_inputs
    target = _grid("target-grid")
    target["origin"] = [-0.5, 0.0]

    # Target centers on the shared cell boundary are not within either cell,
    # so they are explicitly nearest-filled rather than counted as direct.
    audit = HmsSpatialTransfer.audit_excess_to_grid(
        hdf_path,
        sqlite_path,
        fingerprint_cube,
        _grid("source-grid"),
        target,
        excess_depth_units="IN",
    )
    assert audit["metrics"]["direct_target_cell_count"] == 0
    assert audit["metrics"]["unsupported_target_cell_count"] == 4


def test_audit_rejects_changed_fingerprint(audit_inputs):
    hdf_path, sqlite_path, fingerprint_cube = audit_inputs
    changed = fingerprint_cube.copy()
    changed[:, 0, 0] = 99.0

    with pytest.raises(ValueError, match="residual exceeds tolerance"):
        HmsSpatialTransfer.audit_excess_to_grid(
            hdf_path,
            sqlite_path,
            changed,
            _grid("source-grid"),
            _grid("target-grid"),
            excess_depth_units="IN",
        )


def test_audit_rejects_ambiguous_fingerprints(audit_inputs):
    hdf_path, sqlite_path, fingerprint_cube = audit_inputs
    ambiguous = fingerprint_cube.copy()
    ambiguous[:, 1, 0] = ambiguous[:, 0, 0]
    with File(hdf_path, "r+") as hdf:
        hdf["results/Basin/lwe_precipitation_rate"][:, :] = 0.1

    with pytest.raises(ValueError, match="change transferred excess"):
        HmsSpatialTransfer.audit_excess_to_grid(
            hdf_path,
            sqlite_path,
            ambiguous,
            _grid("source-grid"),
            _grid("target-grid"),
            excess_depth_units="IN",
        )


def test_audit_accepts_permutation_invariant_ambiguity(audit_inputs):
    hdf_path, sqlite_path, fingerprint_cube = audit_inputs
    ambiguous = fingerprint_cube.copy()
    ambiguous[:, 1, 0] = ambiguous[:, 0, 0]
    with File(hdf_path, "r+") as hdf:
        hdf["results/Basin/lwe_precipitation_rate"][:, :] = 0.1
        hdf["results/Basin/Incremental Excess"][:, :] = 1.0

    audit = HmsSpatialTransfer.audit_excess_to_grid(
        hdf_path,
        sqlite_path,
        ambiguous,
        _grid("source-grid"),
        _grid("target-grid"),
        excess_depth_units="IN",
    )

    element = audit["fingerprint"]["elements"]["Basin"]
    assert element["ambiguous_fingerprint_group_count"] == 1
    assert element["ambiguously_identified_cell_count"] == 2
    assert element["ambiguous_result_fingerprint_group_count"] == 1
    assert element["ambiguously_identified_result_column_count"] == 2
    assert element["ambiguities_are_permutation_invariant"] is True


def test_audit_rejects_result_side_fingerprint_ambiguity(audit_inputs):
    hdf_path, sqlite_path, fingerprint_cube = audit_inputs
    nearly_equal_sources = fingerprint_cube.copy()
    nearly_equal_sources[:, 1, 0] = 0.1000015
    with File(hdf_path, "r+") as hdf:
        hdf["results/Basin/lwe_precipitation_rate"][:, :] = 0.10000075

    with pytest.raises(
        ValueError,
        match="HMS result fingerprints change transferred excess",
    ):
        HmsSpatialTransfer.audit_excess_to_grid(
            hdf_path,
            sqlite_path,
            nearly_equal_sources,
            _grid("source-grid"),
            _grid("target-grid"),
            excess_depth_units="IN",
        )


def test_audit_rejects_incompatible_grid_crs(audit_inputs):
    hdf_path, sqlite_path, fingerprint_cube = audit_inputs
    target = _grid("target-grid")
    target["crs"] = "EPSG:32615"

    with pytest.raises(ValueError, match="CRS values do not match"):
        HmsSpatialTransfer.audit_excess_to_grid(
            hdf_path,
            sqlite_path,
            fingerprint_cube,
            _grid("source-grid"),
            target,
            excess_depth_units="IN",
        )


def test_audit_rejects_unknown_excess_depth_units(audit_inputs):
    hdf_path, sqlite_path, fingerprint_cube = audit_inputs

    with pytest.raises(ValueError, match="excess_depth_units"):
        HmsSpatialTransfer.audit_excess_to_grid(
            hdf_path,
            sqlite_path,
            fingerprint_cube,
            _grid("source-grid"),
            _grid("target-grid"),
            excess_depth_units="FT",
        )


def test_audit_rejects_declared_units_that_disagree_with_hdf(audit_inputs):
    hdf_path, sqlite_path, fingerprint_cube = audit_inputs

    with pytest.raises(ValueError, match="are 'IN', not declared 'MM'"):
        HmsSpatialTransfer.audit_excess_to_grid(
            hdf_path,
            sqlite_path,
            fingerprint_cube,
            _grid("source-grid"),
            _grid("target-grid"),
            excess_depth_units="MM",
        )


def test_audit_allows_non_finite_values_outside_active_cells(audit_inputs):
    hdf_path, sqlite_path, fingerprint_cube = audit_inputs
    masked = fingerprint_cube.copy()
    masked[:, :, 1] = np.nan

    audit = HmsSpatialTransfer.audit_excess_to_grid(
        hdf_path,
        sqlite_path,
        masked,
        _grid("source-grid"),
        _grid("target-grid"),
        excess_depth_units="IN",
    )

    assert audit["metrics"]["active_hms_cell_count"] == 2


def test_read_fingerprint_cube_authenticates_grid_and_window(
    tmp_path,
    monkeypatch,
):
    from ras_commander import RasDss

    source = tmp_path / "forcing.dss"
    source.write_bytes(b"forcing")
    pathnames = [
        "/A/B/PRECIPITATION/01JAN2020:0000/01JAN2020:0100/F/",
        "/A/B/PRECIPITATION/01JAN2020:0100/01JAN2020:0200/F/",
    ]
    monkeypatch.setattr(
        RasDss,
        "get_catalog",
        staticmethod(lambda _: pd.DataFrame({"pathname": pathnames})),
    )
    monkeypatch.setattr(
        RasDss,
        "read_grid",
        staticmethod(
            lambda _source, pathname: {
                "shape": (2, 2),
                "cell_size": 1.0,
                "crs": "EPSG:3857",
                "data": np.asarray(
                    [
                        [pathnames.index(pathname) + 1.0, np.nan],
                        [pathnames.index(pathname) + 1.0] * 2,
                    ]
                ),
                "metadata": {"origin": (0.0, 0.0)},
            }
        ),
    )

    cube, evidence = HmsSpatialTransfer.read_fingerprint_cube(
        source,
        "/A/B/PRECIPITATION///F/",
        _grid("source-grid"),
        model_start=datetime(2020, 1, 1),
        model_end=datetime(2020, 1, 1, 2),
        interval_minutes=60,
    )

    assert cube.shape == (2, 2, 2)
    assert cube[0, 0, 0] == 1.0
    assert cube[1, 0, 0] == 2.0
    assert np.isnan(cube[0, 0, 1])
    assert evidence["record_count"] == 2
    assert evidence["source_dss"]["sha256"]
    assert evidence["cube_sha256"]
    assert HmsSpatialTransfer._grid_time("01JAN2020:2400") == datetime(
        2020,
        1,
        2,
    )


def test_read_fingerprint_cube_rejects_grid_drift(tmp_path, monkeypatch):
    from ras_commander import RasDss

    source = tmp_path / "forcing.dss"
    source.write_bytes(b"forcing")
    pathname = "/A/B/PRECIPITATION/01JAN2020:0000/01JAN2020:0100/F/"
    monkeypatch.setattr(
        RasDss,
        "get_catalog",
        staticmethod(lambda _: pd.DataFrame({"pathname": [pathname]})),
    )
    monkeypatch.setattr(
        RasDss,
        "read_grid",
        staticmethod(
            lambda *_: {
                "shape": (2, 2),
                "cell_size": 1.0,
                "crs": "EPSG:3857",
                "data": np.ones((2, 2)),
                "metadata": {"origin": (1.0, 0.0)},
            }
        ),
    )

    with pytest.raises(ValueError, match="grid definition has drifted"):
        HmsSpatialTransfer.read_fingerprint_cube(
            source,
            "/A/B/PRECIPITATION///F/",
            _grid("source-grid"),
            model_start=datetime(2020, 1, 1),
            model_end=datetime(2020, 1, 1, 1),
            interval_minutes=60,
        )


def test_export_excess_grid_writes_identity_bound_product(
    audit_inputs,
    tmp_path,
    monkeypatch,
):
    hdf_path, sqlite_path, fingerprint_cube = audit_inputs
    source = tmp_path / "forcing.dss"
    source.write_bytes(b"forcing")
    source_evidence = {
        "source_dss": {
            "path": str(source),
            "size_bytes": source.stat().st_size,
            "sha256": "1" * 64,
        },
        "source_grid": _grid("source-grid"),
        "pathname_selector": "/A/B/PRECIPITATION///F/",
        "record_count": 2,
        "start": "2020-01-01T00:00:00",
        "end": "2020-01-01T00:02:00",
        "interval_minutes": 1,
        "first_pathname": "first",
        "last_pathname": "last",
        "cube_sha256": "2" * 64,
    }
    monkeypatch.setattr(
        HmsSpatialTransfer,
        "read_fingerprint_cube",
        staticmethod(lambda *args, **kwargs: (fingerprint_cube, source_evidence)),
    )

    written_frames: list[tuple[int, int, int]] = []

    def write_grid(output, selector, frames, boundaries, grid_info):
        written_frames.append(frames.shape)
        output.write_bytes(b"transferred grid")
        return {
            "status": "succeeded",
            "record_count": 2,
            "first_pathname": (
                "/A/B/PRECIPITATION/01JAN2020:0000/01JAN2020:0001/EXCESS/"
            ),
            "last_pathname": (
                "/A/B/PRECIPITATION/01JAN2020:0001/01JAN2020:0002/EXCESS/"
            ),
        }

    monkeypatch.setattr(
        HmsSpatialTransfer,
        "_write_grid_subprocess",
        staticmethod(write_grid),
    )
    output = tmp_path / "products" / "ras-excess.dss"
    manifest = HmsSpatialTransfer.export_excess_to_grid(
        hdf_path,
        sqlite_path,
        source,
        "/A/B/PRECIPITATION///F/",
        _grid("source-grid"),
        _grid("target-grid"),
        output,
        "/A/B/PRECIPITATION///EXCESS/",
        model_start=datetime(2020, 1, 1),
        model_end=datetime(2020, 1, 1, 0, 2),
        model_interval_minutes=1,
        source_interval_minutes=1,
        excess_depth_units="IN",
        source_value_multiplier=1.0,
    )

    assert written_frames == [(2, 2, 2)]
    assert manifest["schema"] == "hms-commander/gridded-excess-product/1.0"
    assert manifest["status"] == "qualification_only"
    assert manifest["forecast_eligible"] is False
    assert manifest["output"]["record_count"] == 2
    assert output.with_suffix(".audit.json").is_file()
    assert output.with_suffix(".manifest.json").is_file()
    with pytest.raises(FileExistsError, match="Refusing to overwrite"):
        HmsSpatialTransfer.export_excess_to_grid(
            hdf_path,
            sqlite_path,
            source,
            "/A/B/PRECIPITATION///F/",
            _grid("source-grid"),
            _grid("target-grid"),
            output,
            "/A/B/PRECIPITATION///EXCESS/",
            model_start=datetime(2020, 1, 1),
            model_end=datetime(2020, 1, 1, 0, 2),
            model_interval_minutes=1,
            source_interval_minutes=1,
            excess_depth_units="IN",
            source_value_multiplier=1.0,
        )
