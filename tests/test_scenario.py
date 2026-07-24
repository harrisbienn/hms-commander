"""Tests for immutable HMS scenario workspace preparation."""

from datetime import datetime
from pathlib import Path

import pytest

from hms_commander import HmsScenario


def _write_project(folder: Path) -> Path:
    folder.mkdir()
    (folder / "Example.hms").write_text(
        """Project: Example
     Version: 4.9
     Filepath Separator: \\
End:

Precipitation: BaselineMet
     Filename: BaselineMet.met
End:

Basin: BaselineBasin
     Filename: BaselineBasin.basin
End:

Control: BaselineControl
     FileName: BaselineControl.control
End:
""",
        encoding="utf-8",
    )
    (folder / "BaselineMet.met").write_text(
        """Meteorology: BaselineMet
     Version: 4.9
     Precipitation Method: Gridded Precipitation
End:

Precip Method Parameters: Gridded Precipitation
     Precip Grid Name: BaselineGrid
End:
""",
        encoding="utf-8",
    )
    (folder / "BaselineControl.control").write_text(
        """Control: BaselineControl
     Start Date: 1 January 2020
     Start Time: 00:00
     End Date: 2 January 2020
     End Time: 00:00
     Time Interval: 15
End:
""",
        encoding="utf-8",
    )
    (folder / "BaselineBasin.basin").write_text(
        "Basin: BaselineBasin\nEnd:\n",
        encoding="utf-8",
    )
    (folder / "Example.run").write_text(
        """Run: BaselineRun
     Log File: baseline.log
     DSS File: baseline.dss
     Basin: BaselineBasin
     Precip: BaselineMet
     Control: BaselineControl
End:
""",
        encoding="utf-8",
    )
    (folder / "Example.grid").write_text(
        """Grid Manager: Example
     Version: 4.9
End:

Grid: BaselineGrid
     Grid Type: Precipitation
     Description: Baseline
     Data Source Type: External DSS
     Variant: Variant-1
       Default Variant: Yes
       DSS File Name: data\\baseline.dss
       DSS Pathname: /HRAP/BASIN/PRECIP///STAGEIV/
     End Variant: Variant-1
End:
""",
        encoding="utf-8",
    )
    return folder


def test_prepare_workspace_clones_and_rewires_without_mutating_source(tmp_path):
    source = _write_project(tmp_path / "source")
    forcing = tmp_path / "rank001.dss"
    forcing.write_bytes(b"not-a-real-dss")
    original_files = {
        path.name: path.read_bytes()
        for path in source.iterdir()
        if path.is_file()
    }

    prepared = HmsScenario.prepare_workspace(
        source,
        tmp_path / "workspace",
        "lwi-r3-rank-001",
        "BaselineRun",
        "BaselineGrid",
        forcing,
        "/AORC-TRANSPOSED/SHG_1000/PRECIPITATION///INCREMENTAL/",
        datetime(2019, 9, 18, 13, 0),
        datetime(2019, 9, 19, 13, 0),
        time_interval_minutes=5,
    )

    assert prepared.project_file.is_file()
    assert prepared.precipitation_file.read_bytes() == b"not-a-real-dss"
    assert prepared.output_dss.parent.is_dir()
    assert prepared.output_dss.name == "lwi-r3-rank-001_hms.dss"

    met = (prepared.project_folder / f"{prepared.met_name}.met").read_text(
        encoding="utf-8"
    )
    grid = (prepared.project_folder / "Example.grid").read_text(encoding="utf-8")
    control = (
        prepared.project_folder / f"{prepared.control_name}.control"
    ).read_text(encoding="utf-8")
    run = (prepared.project_folder / "Example.run").read_text(encoding="utf-8")

    assert f"Precip Grid Name: {prepared.grid_name}" in met
    assert f"Grid: {prepared.grid_name}" in grid
    assert "DSS File Name: forcing\\rank001.dss" in grid
    assert "Start Date: 18 September 2019" in control
    assert "Time Interval: 5 Minutes" in control
    assert f"Run: {prepared.run_name}" in run
    assert f"DSS File: output\\{prepared.output_dss.name}" in run
    assert {
        path.name: path.read_bytes()
        for path in source.iterdir()
        if path.is_file()
    } == original_files


def test_prepare_workspace_rejects_timezone_aware_model_times(tmp_path):
    source = _write_project(tmp_path / "source")
    forcing = tmp_path / "forcing.dss"
    forcing.write_bytes(b"dss")

    with pytest.raises(ValueError, match="naive datetimes"):
        HmsScenario.prepare_workspace(
            source,
            tmp_path / "workspace",
            "scenario",
            "BaselineRun",
            "BaselineGrid",
            forcing,
            "/A/B/C/D/E/F/",
            datetime.fromisoformat("2020-01-01T00:00:00+00:00"),
            datetime.fromisoformat("2020-01-02T00:00:00+00:00"),
        )


def test_prepare_workspace_is_non_destructive_by_default(tmp_path):
    source = _write_project(tmp_path / "source")
    forcing = tmp_path / "forcing.dss"
    forcing.write_bytes(b"dss")
    destination = tmp_path / "workspace"
    destination.mkdir()

    with pytest.raises(FileExistsError, match="Workspace already exists"):
        HmsScenario.prepare_workspace(
            source,
            destination,
            "scenario",
            "BaselineRun",
            "BaselineGrid",
            forcing,
            "/A/B/C/D/E/F/",
            datetime(2020, 1, 1),
            datetime(2020, 1, 2),
        )
