"""Tests for project-native HMS grid definition updates."""

from pathlib import Path

import pytest

from hms_commander import HmsGrid


def _write_modern_grid(path: Path) -> Path:
    path.write_text(
        """Grid Manager: Example
     Version: 4.9
     Filepath Separator: \\
End:

Grid: Baseline
     Grid Type: Precipitation
     Description: Baseline forcing
     Data Source Type: External DSS
     Variant: Variant-1
       Default Variant: Yes
       DSS File Name: data\\baseline.dss
       DSS Pathname: /HRAP/BASIN/PRECIP///STAGEIV/
     End Variant: Variant-1
     Use Lookup Table: No
End:

Grid: Soil
     Grid Type: Percolation Rate
     Description: Must remain untouched
     Data Source Type: External DSS
     Variant: Variant-1
       Default Variant: Yes
       DSS File Name: data\\soil.dss
       DSS Pathname: /SHG/SOIL/PERCOLATION///VALUE/
     End Variant: Variant-1
End:
""",
        encoding="utf-8",
    )
    return path


def test_clone_external_dss_grid_preserves_source_and_other_blocks(tmp_path):
    grid_path = _write_modern_grid(tmp_path / "Example.grid")
    pathname = "/AORC-TRANSPOSED/SHG_1000/PRECIPITATION///INCREMENTAL/"

    result = HmsGrid.clone_external_dss_grid(
        grid_path,
        "Baseline",
        "StormHub_Rank_001",
        r"forcing\r001_target.dss",
        pathname,
        description="StormHub scenario forcing",
    )

    content = grid_path.read_text(encoding="utf-8")
    assert result["grid_name"] == "StormHub_Rank_001"
    assert content.count("Grid: Baseline") == 1
    assert content.count("Grid: StormHub_Rank_001") == 1
    assert "DSS File Name: data\\baseline.dss" in content
    assert "DSS File Name: forcing\\r001_target.dss" in content
    assert f"DSS Pathname: {pathname}" in content
    assert "Description: Must remain untouched" in content


def test_set_external_dss_grid_changes_only_named_block(tmp_path):
    grid_path = _write_modern_grid(tmp_path / "Example.grid")
    pathname = "/AORC-TRANSPOSED/SHG_1000/PRECIPITATION///INCREMENTAL/"

    HmsGrid.set_external_dss_grid(
        grid_path,
        "Baseline",
        "forcing.dss",
        pathname,
    )

    content = grid_path.read_text(encoding="utf-8")
    assert "DSS File Name: forcing.dss" in content
    assert "DSS File Name: data\\soil.dss" in content
    assert content.count(f"DSS Pathname: {pathname}") == 1


def test_clone_external_dss_grid_rejects_duplicate_name(tmp_path):
    grid_path = _write_modern_grid(tmp_path / "Example.grid")

    with pytest.raises(ValueError, match="already exists"):
        HmsGrid.clone_external_dss_grid(
            grid_path,
            "Baseline",
            "Soil",
            "forcing.dss",
            "/A/B/C/D/E/F/",
        )


def test_set_external_dss_grid_rejects_invalid_pathname(tmp_path):
    grid_path = _write_modern_grid(tmp_path / "Example.grid")

    with pytest.raises(ValueError, match="Invalid DSS pathname"):
        HmsGrid.set_external_dss_grid(
            grid_path,
            "Baseline",
            "forcing.dss",
            "NOT/A/DSS/PATH",
        )
