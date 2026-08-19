"""Tests for immutable HMS scenario workspace preparation."""

from datetime import datetime
import importlib
from pathlib import Path

import pytest

from hms_commander import HmsScenario, HmsScenarioWorkspace


def _write_project(folder: Path) -> Path:
    folder.mkdir()
    (folder / "Example.hms").write_text(
        """Project: Example
     Version: 4.9
     Filepath Separator: \\
     DSS File Name: project_data.dss
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
     Last Execution Date: 2 January 2020
     Last Execution Time: 03:04:05
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

Grid: RootInputGrid
     Grid Type: Precipitation
     Description: Root DSS input
     Data Source Type: External DSS
     Variant: Variant-1
       Default Variant: Yes
       DSS File Name: root_grid_input.dss
       DSS Pathname: /HRAP/ROOT/PRECIP///STAGEIV/
     End Variant: Variant-1
End:
""",
        encoding="utf-8",
    )
    (folder / "Example.gage").write_text(
        """Gage: ObservedFlow
     Gage Type: Discharge Gage
     Data Source Type: External DSS
     Filename: root_gage_input.dss
     Pathname: //BASIN/OBS-FLOW/01JAN2020/15MIN/OBS/
End:

Gage: LegacyObservedFlow
     Gage Type: Discharge Gage
     Data Source Type: External DSS
     DSS File: root_legacy_gage_input.dss
     Pathname: //BASIN/LEGACY-FLOW/01JAN2020/15MIN/OBS/
End:
""",
        encoding="utf-8",
    )
    (folder / "results").mkdir()
    (folder / "results" / "old_run.h5").write_bytes(b"generated results")
    (folder / "baseline.dss").write_bytes(b"generated root output")
    (folder / "project_data.dss").write_bytes(b"required paired data")
    (folder / "root_grid_input.dss").write_bytes(b"required grid input")
    (folder / "root_gage_input.dss").write_bytes(b"required gage input")
    (folder / "root_legacy_gage_input.dss").write_bytes(b"required legacy input")
    (folder / "unreferenced.dss").write_bytes(b"unreferenced root artifact")
    (folder / "baseline.log").write_text("generated log", encoding="utf-8")
    (folder / "baseline.out").write_text("generated report", encoding="utf-8")
    (folder / "data").mkdir()
    (folder / "data" / "required_input.dss").write_bytes(b"required model input")
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
    assert prepared.clone_policy == "input-only"
    assert not (prepared.project_folder / "results").exists()
    assert not (prepared.project_folder / "baseline.dss").exists()
    assert (
        prepared.project_folder / "project_data.dss"
    ).read_bytes() == b"required paired data"
    assert (
        prepared.project_folder / "root_grid_input.dss"
    ).read_bytes() == b"required grid input"
    assert (
        prepared.project_folder / "root_gage_input.dss"
    ).read_bytes() == b"required gage input"
    assert (
        prepared.project_folder / "root_legacy_gage_input.dss"
    ).read_bytes() == b"required legacy input"
    assert not (prepared.project_folder / "unreferenced.dss").exists()
    assert not (prepared.project_folder / "baseline.log").exists()
    assert not (prepared.project_folder / "baseline.out").exists()
    assert (
        prepared.project_folder / "data" / "required_input.dss"
    ).read_bytes() == b"required model input"

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
    assert "Time Interval: 5" in control
    assert f"Run: {prepared.run_name}" in run
    assert f"DSS File: output\\{prepared.output_dss.name}" in run
    cloned_run_block = run.split(f"Run: {prepared.run_name}", maxsplit=1)[1]
    assert "Last Execution Date:" not in cloned_run_block
    assert "Last Execution Time:" not in cloned_run_block
    assert "Last Execution Date: 2 January 2020" in run
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


def _execution_workspace(tmp_path: Path) -> HmsScenarioWorkspace:
    project = tmp_path / "workspace"
    output = project / "output" / "scenario_hms.dss"
    log = project / "scenario_hms.log"
    project.mkdir()
    output.parent.mkdir()
    output.write_bytes(b"dss output")
    (project / "Example.hms").write_text("Project: Example\nEnd:\n", encoding="utf-8")
    return HmsScenarioWorkspace(
        scenario_id="scenario",
        source_project=tmp_path / "source",
        project_folder=project,
        project_file=project / "Example.hms",
        run_name="FF_scenario",
        met_name="FF_scenario_Met",
        control_name="FF_scenario_Control",
        grid_name="FF_scenario_Precip",
        precipitation_source=tmp_path / "source.dss",
        precipitation_file=project / "forcing" / "source.dss",
        precipitation_pathname="/A/B/C///F/",
        output_dss=output,
        log_file=log,
    )


def test_execute_requires_hms_completion_marker(tmp_path, monkeypatch):
    workspace = _execution_workspace(tmp_path)
    scenario_module = importlib.import_module("hms_commander.HmsScenario")
    workspace.log_file.write_text(
        'NOTE 15301: Began computing simulation run "FF_scenario".\n'
        "ERROR 40516: Precipitation is missing or invalid.\n"
        'WARNING 15303: Aborted run "FF_scenario".\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        scenario_module.HmsPrj,
        "initialize",
        lambda self, *args, **kwargs: self,
    )
    monkeypatch.setattr(
        scenario_module.HmsCmdr,
        "compute_run",
        lambda *args, **kwargs: True,
    )

    artifact = HmsScenario.execute(workspace)

    assert artifact.status == "failed"
    assert artifact.process_succeeded is True
    assert artifact.completion_marker_found is False
    assert artifact.abort_marker_found is True
    assert artifact.error_count == 1


def test_execute_accepts_clean_hms_completion(tmp_path, monkeypatch):
    workspace = _execution_workspace(tmp_path)
    scenario_module = importlib.import_module("hms_commander.HmsScenario")
    workspace.log_file.write_text(
        'NOTE 15301: Began computing simulation run "FF_scenario".\n'
        'NOTE 15302: Finished computing simulation run "FF_scenario".\n'
        "NOTE 15312: The total runtime for this simulation is 00:01.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        scenario_module.HmsPrj,
        "initialize",
        lambda self, *args, **kwargs: self,
    )
    monkeypatch.setattr(
        scenario_module.HmsCmdr,
        "compute_run",
        lambda *args, **kwargs: True,
    )

    artifact = HmsScenario.execute(workspace)

    assert artifact.status == "succeeded"
    assert artifact.process_succeeded is True
    assert artifact.completion_marker_found is True
    assert artifact.abort_marker_found is False
    assert artifact.error_count == 0
