"""Tests for package-owned HMS spatial-transfer audits."""

from __future__ import annotations

import json
from copy import deepcopy

import numpy as np
import pytest

pytest.importorskip("geopandas")
pytest.importorskip("h5py")
pytest.importorskip("scipy")
from geopandas import GeoDataFrame
from h5py import File
from hms_commander import HmsSpatialTransfer, HmsSqlite
from shapely.geometry import box

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
        group.create_dataset(
            "Incremental Excess",
            data=np.asarray([[2.0, 1.0], [2.0, 1.0]], dtype=np.float64),
        )

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
    assert element["ambiguities_are_permutation_invariant"] is True


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
