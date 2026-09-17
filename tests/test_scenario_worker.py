"""Contract tests for the package-owned HMS scenario worker."""

import hashlib
import importlib
import importlib.resources
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hms_commander import (
    HmsJython,
    HmsResultsProducts,
    HmsRunArtifact,
    HmsScenarioWorker,
    HmsScenarioWorkspace,
    HmsSubbasinTransfer,
)

FLOW_PATH = "//OUTLET/FLOW//5Minute/RUN:SCENARIO/"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _request(tmp_path: Path) -> tuple[dict, Path, Path]:
    source = tmp_path / "source"
    source.mkdir()
    project_file = source / "Example.hms"
    project_file.write_text("Project: Example\nEnd:\n", encoding="utf-8")
    forcing = tmp_path / "forcing.dss"
    forcing.write_bytes(b"forcing-identity")
    request = {
        "schema": HmsScenarioWorker.REQUEST_SCHEMA,
        "scenario": {
            "scenario_id": "lwi-r3-rank-001",
            "specification_sha256": "1" * 64,
        },
        "source_model": {
            "project": str(source),
            "project_file_sha256": _sha256(project_file),
            "run": "BaselineRun",
            "grid": "BaselineGrid",
        },
        "forcing": {
            "dss": str(forcing),
            "sha256": _sha256(forcing),
            "pathname": "/AORC/GRID/PRECIPITATION///INCREMENTAL/",
        },
        "model_window": {
            "start": "2019-09-18T13:00:00",
            "end": "2019-09-19T13:00:00",
            "time_zone": "America/Chicago",
            "interval_minutes": 5,
        },
        "workspace": str(tmp_path / "workspace"),
        "products": {
            "directory": str(tmp_path / "products"),
            "required_pathnames": [
                {
                    "mapping_id": "upstream-001",
                    "pathname": FLOW_PATH,
                    "ras_boundary": "Upstream BC",
                }
            ],
        },
        "execution": {"timeout_seconds": 60},
    }
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    return request, request_path, tmp_path / "result.json"


def _install_success_fakes(monkeypatch, request: dict) -> dict[str, int]:
    worker_module = importlib.import_module("hms_commander.HmsScenarioWorker")
    calls = {"prepare": 0, "execute": 0, "export": 0}

    def prepare(*_args, **_kwargs):
        calls["prepare"] += 1
        workspace_path = Path(request["workspace"])
        workspace_path.mkdir()
        output = workspace_path / "output" / "lwi-r3-rank-001_hms.dss"
        output.parent.mkdir()
        output.write_bytes(b"completed-hms-output")
        project_file = workspace_path / "Example.hms"
        project_file.write_text("Project: Example\nEnd:\n", encoding="utf-8")
        log_file = workspace_path / "lwi-r3-rank-001_hms.log"
        log_file.write_text(
            'NOTE 15302: Finished computing simulation run "FF_lwi-r3-rank-001"\n',
            encoding="utf-8",
        )
        if request.get("spatial_transfer") is not None:
            transfer = request["spatial_transfer"]
            if transfer.get("method") != HmsSubbasinTransfer.METHOD:
                basin_source = (
                    Path(request["source_model"]["project"]) / transfer["basin_sqlite"]
                )
                (workspace_path / transfer["basin_sqlite"]).write_bytes(
                    basin_source.read_bytes()
                )
                results = workspace_path / "results"
                results.mkdir()
                (results / "RUN_FF_lwi-r3-rank-001.h5").write_bytes(b"result hdf")
        return HmsScenarioWorkspace(
            scenario_id="lwi-r3-rank-001",
            source_project=Path(request["source_model"]["project"]),
            project_folder=workspace_path,
            project_file=project_file,
            run_name="FF_lwi-r3-rank-001",
            met_name="FF_lwi-r3-rank-001_Met",
            control_name="FF_lwi-r3-rank-001_Control",
            grid_name="FF_lwi-r3-rank-001_Precip",
            precipitation_source=Path(request["forcing"]["dss"]),
            precipitation_file=Path(request["forcing"]["dss"]),
            precipitation_pathname=request["forcing"]["pathname"],
            output_dss=output,
            log_file=log_file,
        )

    def execute(workspace, **_kwargs):
        calls["execute"] += 1
        return HmsRunArtifact(
            scenario_id=workspace.scenario_id,
            status="succeeded",
            run_name=workspace.run_name,
            project_folder=workspace.project_folder,
            dss_file=workspace.output_dss,
            log_file=workspace.log_file,
            started_at="2026-08-19T12:00:00Z",
            finished_at="2026-08-19T12:01:00Z",
            dss_exists=True,
            dss_size_bytes=workspace.output_dss.stat().st_size,
            process_succeeded=True,
            completion_marker_found=True,
        )

    def export(dss_file, mappings, output_directory, **_kwargs):
        calls["export"] += 1
        assert Path(dss_file).is_file()
        assert mappings[0]["pathname"] == FLOW_PATH
        output = Path(output_directory)
        output.mkdir()
        qualification = {
            "schema": "hms-commander/hydrologic-qualification/1.0",
            "all_required_pathnames_valid": True,
        }
        (output / HmsResultsProducts.QUALIFICATION_FILENAME).write_text(
            json.dumps(qualification),
            encoding="utf-8",
        )
        manifest = {
            "schema": HmsResultsProducts.SCHEMA,
            "status": {
                "all_required_pathnames_read": True,
                "all_required_pathnames_valid": True,
                "all_required_pathnames_enter_recession": True,
                "hydrologic_handoff": "not_evaluated",
            },
        }
        (output / HmsResultsProducts.MANIFEST_FILENAME).write_text(
            json.dumps(manifest),
            encoding="utf-8",
        )
        return manifest

    monkeypatch.setattr(
        worker_module.HmsScenario,
        "prepare_workspace",
        staticmethod(prepare),
    )
    monkeypatch.setattr(
        worker_module.HmsScenario,
        "validate_workspace",
        staticmethod(lambda _workspace: {"all_references_valid": True}),
    )
    monkeypatch.setattr(
        worker_module.HmsScenario,
        "execute",
        staticmethod(execute),
    )
    monkeypatch.setattr(
        worker_module.HmsResultsProducts,
        "export",
        staticmethod(export),
    )
    if request.get("spatial_transfer") is not None:
        calls["transfer"] = 0

        def export_transfer(*args, **kwargs):
            calls["transfer"] += 1
            output = Path(args[6])
            output.parent.mkdir(parents=True)
            output.write_bytes(b"ras gridded excess")
            audit = output.with_suffix(".audit.json")
            audit.write_text("{}", encoding="utf-8")
            manifest_path = output.with_suffix(".manifest.json")
            manifest = {
                "schema": "hms-commander/gridded-excess-product/1.0",
                "status": "qualification_only",
                "forecast_eligible": False,
                "output": {
                    "pathname_selector": args[7],
                    "record_count": 288,
                },
                "metrics": {
                    "direct_target_cell_count": 2,
                    "unsupported_target_cell_count": 2,
                },
            }
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            return manifest

        transfer = request["spatial_transfer"]
        if transfer.get("method") == HmsSubbasinTransfer.METHOD:

            def export_subbasin_transfer(*args, **kwargs):
                calls["transfer"] += 1
                output = Path(args[2])
                output.parent.mkdir(parents=True)
                output.write_bytes(b"ras subbasin volume excess")
                audit = output.with_suffix(".audit.json")
                audit.write_text("{}", encoding="utf-8")
                manifest_path = output.with_suffix(".manifest.json")
                manifest = {
                    "schema": HmsSubbasinTransfer.PRODUCT_SCHEMA,
                    "status": "qualification_only",
                    "forecast_eligible": False,
                    "output": {
                        "pathname_selector": args[3],
                        "record_count": 288,
                    },
                    "volume": {
                        "aggregate": {"within_tolerance": True},
                    },
                }
                assert kwargs["source_run_name"] == "FF_lwi-r3-rank-001"
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                return manifest

            monkeypatch.setattr(
                worker_module.HmsSubbasinTransfer,
                "apply_transfer_map_to_dss",
                staticmethod(export_subbasin_transfer),
            )
        else:
            monkeypatch.setattr(
                worker_module.HmsSpatialTransfer,
                "export_excess_to_grid",
                staticmethod(export_transfer),
            )
    return calls


def test_worker_writes_identity_bound_success_and_verifies_repeat(
    tmp_path,
    monkeypatch,
):
    request, request_path, result_path = _request(tmp_path)
    calls = _install_success_fakes(monkeypatch, request)

    assert HmsScenarioWorker.run(request_path, result_path) == 0
    result_bytes = result_path.read_bytes()
    result = json.loads(result_bytes)

    assert result["schema"] == HmsScenarioWorker.RESULT_SCHEMA
    assert result["status"] == "succeeded"
    assert result["scenario"] == request["scenario"]
    assert result["preparation"]["status"] == "passed"
    assert result["execution"]["completion_marker_found"] is True
    assert result["output_dss"]["sha256"] == _sha256(Path(result["output_dss"]["path"]))
    assert result["products"]["schema"] == HmsResultsProducts.SCHEMA
    assert result["raw_qualification"]["all_required_pathnames_valid"] is True
    assert result["error"] is None
    assert calls == {"prepare": 1, "execute": 1, "export": 1}

    assert HmsScenarioWorker.run(request_path, result_path) == 0
    assert result_path.read_bytes() == result_bytes
    assert calls == {"prepare": 1, "execute": 1, "export": 1}

    Path(result["output_dss"]["path"]).write_bytes(b"tampered")
    assert HmsScenarioWorker.run(request_path, result_path) == 3
    assert result_path.read_bytes() == result_bytes
    assert calls == {"prepare": 1, "execute": 1, "export": 1}


def test_worker_rejects_invalid_forcing_identity(tmp_path):
    request, request_path, result_path = _request(tmp_path)
    request["forcing"]["sha256"] = "f" * 64
    request_path.write_text(json.dumps(request), encoding="utf-8")

    assert HmsScenarioWorker.run(request_path, result_path) == 2

    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["status"] == "failed"
    assert result["error"]["classification"] == "forcing_identity"
    assert result["request"]["sha256"]
    assert not Path(request["workspace"]).exists()


def test_worker_authenticates_gage_input_identity(tmp_path, monkeypatch):
    request, request_path, result_path = _request(tmp_path)
    gage_input = tmp_path / "qualification-gages.dss"
    gage_input.write_bytes(b"gage input")
    request["gage_inputs"] = [
        {
            "gage_name": "MVK_Ouachita",
            "dss": str(gage_input),
            "sha256": _sha256(gage_input),
            "pathname": "//OUJ_OUACHITAATFELSENTHAL/FLOW//1HOUR/QUALIFICATION/",
        }
    ]
    request_path.write_text(json.dumps(request), encoding="utf-8")
    _install_success_fakes(monkeypatch, request)

    assert HmsScenarioWorker.run(request_path, result_path) == 0
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["status"] == "succeeded"
    assert result["request"]["sha256"]


def test_worker_rejects_gage_input_checksum_drift(tmp_path):
    request, request_path, result_path = _request(tmp_path)
    gage_input = tmp_path / "qualification-gages.dss"
    gage_input.write_bytes(b"gage input")
    request["gage_inputs"] = [
        {
            "gage_name": "MVK_Ouachita",
            "dss": str(gage_input),
            "sha256": "f" * 64,
            "pathname": "//OUJ_OUACHITAATFELSENTHAL/FLOW//1HOUR/QUALIFICATION/",
        }
    ]
    request_path.write_text(json.dumps(request), encoding="utf-8")

    assert HmsScenarioWorker.run(request_path, result_path) == 2
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["error"]["classification"] == "gage_input_identity"
    assert not Path(request["workspace"]).exists()


def test_worker_exports_authenticated_spatial_transfer(tmp_path, monkeypatch):
    request, request_path, result_path = _request(tmp_path)
    source = Path(request["source_model"]["project"])
    basin_sqlite = source / "basin.sqlite"
    basin_sqlite.write_bytes(b"basin geometry")
    source_grid = tmp_path / "source-grid.json"
    source_grid.write_text('{"definition_id":"source"}', encoding="utf-8")
    target_grid = tmp_path / "target-grid.json"
    target_grid.write_text('{"definition_id":"target"}', encoding="utf-8")
    request["spatial_transfer"] = {
        "basin_sqlite": "basin.sqlite",
        "basin_sqlite_sha256": _sha256(basin_sqlite),
        "source_grid_definition": str(source_grid),
        "source_grid_definition_sha256": _sha256(source_grid),
        "target_grid_definition": str(target_grid),
        "target_grid_definition_sha256": _sha256(target_grid),
        "output_pathname": "/SHG/BASIN/PRECIPITATION///EXCESS/",
        "source_interval_minutes": 60,
        "source_value_multiplier": 1.0 / (12.0 * 25.4),
        "fingerprint_tolerance": 1.0e-6,
        "excess_depth_units": "IN",
        "status": "qualification_only",
        "forecast_eligible": False,
    }
    request_path.write_text(json.dumps(request), encoding="utf-8")
    calls = _install_success_fakes(monkeypatch, request)

    assert HmsScenarioWorker.run(request_path, result_path) == 0
    result = json.loads(result_path.read_text(encoding="utf-8"))
    transfer = result["products"]["spatial_transfer"]
    assert transfer["schema"] == "hms-commander/gridded-excess-product/1.0"
    assert transfer["status"] == "qualification_only"
    assert transfer["forecast_eligible"] is False
    assert transfer["record_count"] == 288
    assert transfer["output"]["sha256"]
    assert transfer["manifest"]["sha256"]
    assert transfer["audit"]["sha256"]
    assert calls == {"prepare": 1, "execute": 1, "export": 1, "transfer": 1}


def test_worker_rejects_spatial_transfer_identity_drift(tmp_path):
    request, request_path, result_path = _request(tmp_path)
    source = Path(request["source_model"]["project"])
    basin_sqlite = source / "basin.sqlite"
    basin_sqlite.write_bytes(b"basin geometry")
    source_grid = tmp_path / "source-grid.json"
    source_grid.write_text("{}", encoding="utf-8")
    target_grid = tmp_path / "target-grid.json"
    target_grid.write_text("{}", encoding="utf-8")
    request["spatial_transfer"] = {
        "basin_sqlite": "basin.sqlite",
        "basin_sqlite_sha256": "f" * 64,
        "source_grid_definition": str(source_grid),
        "source_grid_definition_sha256": _sha256(source_grid),
        "target_grid_definition": str(target_grid),
        "target_grid_definition_sha256": _sha256(target_grid),
        "output_pathname": "/SHG/BASIN/PRECIPITATION///EXCESS/",
        "source_interval_minutes": 60,
        "source_value_multiplier": 1.0,
        "fingerprint_tolerance": 1.0e-6,
        "excess_depth_units": "IN",
        "status": "qualification_only",
        "forecast_eligible": False,
    }
    request_path.write_text(json.dumps(request), encoding="utf-8")

    assert HmsScenarioWorker.run(request_path, result_path) == 2
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["error"]["classification"] == "spatial_transfer_identity"
    assert not Path(request["workspace"]).exists()


def test_worker_exports_authenticated_subbasin_volume_transfer(
    tmp_path,
    monkeypatch,
):
    request, request_path, result_path = _request(tmp_path)
    transfer_map = tmp_path / "subbasin-volume-transfer-map.json"
    transfer_map.write_text('{"schema":"test-map"}', encoding="utf-8")
    request["spatial_transfer"] = {
        "method": HmsSubbasinTransfer.METHOD,
        "transfer_map": str(transfer_map),
        "transfer_map_sha256": _sha256(transfer_map),
        "output_pathname": "/SHG/BASIN/PRECIPITATION///EXCESS/",
        "source_a_part": "",
        "source_depth_units": "IN",
        "volume_tolerance": {
            "absolute_cubic_meters": 1.0e-6,
            "relative_fraction": 1.0e-12,
        },
        "readback_absolute_value_tolerance": 0.01,
        "status": "qualification_only",
        "forecast_eligible": False,
    }
    request_path.write_text(json.dumps(request), encoding="utf-8")
    calls = _install_success_fakes(monkeypatch, request)

    assert HmsScenarioWorker.run(request_path, result_path) == 0
    result = json.loads(result_path.read_text(encoding="utf-8"))
    transfer = result["products"]["spatial_transfer"]
    assert transfer["schema"] == HmsSubbasinTransfer.PRODUCT_SCHEMA
    assert transfer["status"] == "qualification_only"
    assert transfer["forecast_eligible"] is False
    assert transfer["record_count"] == 288
    assert transfer["volume"]["aggregate"]["within_tolerance"] is True
    assert "metrics" not in transfer
    assert calls == {"prepare": 1, "execute": 1, "export": 1, "transfer": 1}


def test_worker_rejects_subbasin_transfer_map_identity_drift(tmp_path):
    request, request_path, result_path = _request(tmp_path)
    transfer_map = tmp_path / "subbasin-volume-transfer-map.json"
    transfer_map.write_text('{"schema":"test-map"}', encoding="utf-8")
    request["spatial_transfer"] = {
        "method": HmsSubbasinTransfer.METHOD,
        "transfer_map": str(transfer_map),
        "transfer_map_sha256": "f" * 64,
        "output_pathname": "/SHG/BASIN/PRECIPITATION///EXCESS/",
        "source_a_part": "",
        "source_depth_units": "IN",
        "volume_tolerance": {
            "absolute_cubic_meters": 1.0e-6,
            "relative_fraction": 1.0e-12,
        },
        "readback_absolute_value_tolerance": 0.01,
        "status": "qualification_only",
        "forecast_eligible": False,
    }
    request_path.write_text(json.dumps(request), encoding="utf-8")

    assert HmsScenarioWorker.run(request_path, result_path) == 2
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["error"]["classification"] == "spatial_transfer_identity"
    assert not Path(request["workspace"]).exists()


def test_worker_refuses_an_existing_workspace(tmp_path):
    request, request_path, result_path = _request(tmp_path)
    Path(request["workspace"]).mkdir()

    assert HmsScenarioWorker.run(request_path, result_path) == 3

    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["error"]["classification"] == "existing_workspace"


def test_worker_writes_failure_result_for_timeout(tmp_path, monkeypatch):
    request, request_path, result_path = _request(tmp_path)
    _install_success_fakes(monkeypatch, request)
    worker_module = importlib.import_module("hms_commander.HmsScenarioWorker")
    monkeypatch.setattr(
        worker_module.HmsScenario,
        "execute",
        staticmethod(
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                TimeoutError("HEC-HMS exceeded 60 seconds")
            )
        ),
    )

    assert HmsScenarioWorker.run(request_path, result_path) == 4

    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["error"] == {
        "classification": "timeout",
        "exception_type": "TimeoutError",
        "message": "HEC-HMS exceeded 60 seconds",
        "retryable": True,
    }
    assert result["preparation"]["status"] == "passed"
    assert result["execution"]["status"] == "in_progress"
    assert result["timings"]["execution_seconds"] >= 0


@pytest.mark.parametrize(
    ("changes", "classification"),
    [
        (
            {"completion_marker_found": False},
            "missing_completion_marker",
        ),
        (
            {"status": "failed", "abort_marker_found": True},
            "aborted_run",
        ),
        (
            {"status": "failed", "dss_size_bytes": 0},
            "empty_output",
        ),
    ],
)
def test_worker_classifies_failed_execution_artifacts(
    tmp_path,
    monkeypatch,
    changes,
    classification,
):
    request, request_path, result_path = _request(tmp_path)
    _install_success_fakes(monkeypatch, request)
    worker_module = importlib.import_module("hms_commander.HmsScenarioWorker")

    def failed_execute(workspace, **_kwargs):
        values = {
            "scenario_id": workspace.scenario_id,
            "status": "failed",
            "run_name": workspace.run_name,
            "project_folder": workspace.project_folder,
            "dss_file": workspace.output_dss,
            "log_file": workspace.log_file,
            "started_at": "2026-08-19T12:00:00Z",
            "finished_at": "2026-08-19T12:01:00Z",
            "dss_exists": True,
            "dss_size_bytes": workspace.output_dss.stat().st_size,
            "process_succeeded": True,
            "completion_marker_found": True,
            "abort_marker_found": False,
            "error_count": 0,
        }
        values.update(changes)
        return HmsRunArtifact(**values)

    monkeypatch.setattr(
        worker_module.HmsScenario,
        "execute",
        staticmethod(failed_execute),
    )

    assert HmsScenarioWorker.run(request_path, result_path) == 4
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["error"]["classification"] == classification
    assert result["execution"]["status"] == "failed"
    assert result["products"] is None


def test_worker_rejects_request_contract_drift(tmp_path):
    request, request_path, result_path = _request(tmp_path)
    request["unexpected"] = True
    request_path.write_text(json.dumps(request), encoding="utf-8")

    assert HmsScenarioWorker.run(request_path, result_path) == 2
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["error"]["classification"] == "invalid_request"
    assert "unknown unexpected" in result["error"]["message"]


def test_packaged_worker_schemas_match_public_contract_constants():
    package = importlib.resources.files("hms_commander") / "contracts"
    request_schema = json.loads(
        (package / "scenario-worker-request-v1.0.schema.json").read_text(
            encoding="utf-8"
        )
    )
    result_schema = json.loads(
        (package / "scenario-worker-result-v1.0.schema.json").read_text(
            encoding="utf-8"
        )
    )

    assert request_schema["$id"] == HmsScenarioWorker.REQUEST_SCHEMA
    assert result_schema["$id"] == HmsScenarioWorker.RESULT_SCHEMA


def test_jython_can_propagate_a_subprocess_timeout(tmp_path, monkeypatch):
    installation = tmp_path / "HEC-HMS"
    installation.mkdir()
    (installation / "hms.jar").write_bytes(b"jar")
    monkeypatch.setattr(HmsJython, "_get_hms_version", staticmethod(lambda _path: 413))
    monkeypatch.setattr(
        HmsJython, "_check_version_supported", staticmethod(lambda _version: None)
    )
    monkeypatch.setattr(
        HmsJython, "_format_version", staticmethod(lambda _version: "4.13")
    )
    monkeypatch.setattr(HmsJython, "_is_hms_3x", staticmethod(lambda _version: False))
    monkeypatch.setattr(
        HmsJython,
        "_execute_via_java",
        staticmethod(lambda **_kwargs: (False, "", "Timeout after 7 seconds")),
    )

    with pytest.raises(TimeoutError, match="exceeded 7 seconds"):
        HmsJython.execute_script(
            "print('test')",
            installation,
            working_dir=tmp_path / "workspace",
            timeout=7,
            raise_on_timeout=True,
        )


@pytest.mark.requires_hms
def test_installed_engine_executes_one_scenario(tmp_path):
    names = {
        "project": "HMS_WORKER_TEST_SOURCE_PROJECT",
        "run": "HMS_WORKER_TEST_SOURCE_RUN",
        "grid": "HMS_WORKER_TEST_SOURCE_GRID",
        "forcing": "HMS_WORKER_TEST_FORCING_DSS",
        "forcing_pathname": "HMS_WORKER_TEST_FORCING_PATHNAME",
        "hydrograph_pathname": "HMS_WORKER_TEST_HYDROGRAPH_PATHNAME",
        "hms_executable": "HMS_WORKER_TEST_EXECUTABLE",
    }
    values = {key: os.environ.get(name) for key, name in names.items()}
    missing = [names[key] for key, value in values.items() if not value]
    if missing:
        pytest.skip(
            "Installed-engine worker inputs not configured: " + ", ".join(missing)
        )

    project = Path(values["project"])
    project_folder = project.parent if project.is_file() else project
    project_files = sorted(project_folder.glob("*.hms"))
    assert len(project_files) == 1
    forcing = Path(values["forcing"])
    request = {
        "schema": HmsScenarioWorker.REQUEST_SCHEMA,
        "scenario": {
            "scenario_id": "installed-engine-canary",
            "specification_sha256": "2" * 64,
        },
        "source_model": {
            "project": str(project),
            "project_file_sha256": _sha256(project_files[0]),
            "run": values["run"],
            "grid": values["grid"],
        },
        "forcing": {
            "dss": str(forcing),
            "sha256": _sha256(forcing),
            "pathname": values["forcing_pathname"],
        },
        "model_window": {
            "start": os.environ.get("HMS_WORKER_TEST_START", "2019-09-18T13:00:00"),
            "end": os.environ.get("HMS_WORKER_TEST_END", "2019-09-19T13:00:00"),
            "time_zone": os.environ.get("HMS_WORKER_TEST_TIME_ZONE", "America/Chicago"),
            "interval_minutes": int(
                os.environ.get("HMS_WORKER_TEST_INTERVAL_MINUTES", "5")
            ),
        },
        "workspace": str(tmp_path / "workspace"),
        "products": {
            "directory": str(tmp_path / "products"),
            "required_pathnames": [
                {
                    "mapping_id": "installed-engine-canary",
                    "pathname": values["hydrograph_pathname"],
                }
            ],
        },
        "execution": {
            "timeout_seconds": int(
                os.environ.get("HMS_WORKER_TEST_TIMEOUT_SECONDS", "3600")
            ),
            "hms_executable": values["hms_executable"],
        },
    }
    request_path = tmp_path / "request.json"
    result_path = tmp_path / "result.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")

    # Exercise the worker's process boundary so HMS/JVM state cannot leak into
    # the pytest process used to validate the returned contract.
    worker_environment = os.environ.copy()
    worker_environment["PYTHONIOENCODING"] = "utf-8"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "hms_commander.HmsScenarioWorker",
            "--request",
            str(request_path),
            "--result",
            str(result_path),
        ],
        capture_output=True,
        check=False,
        encoding="utf-8",
        errors="replace",
        env=worker_environment,
        timeout=request["execution"]["timeout_seconds"] + 60,
    )
    assert completed.returncode == 0, (
        "Installed HMS scenario worker failed.\n"
        f"stdout:\n{completed.stdout}\n"
        f"stderr:\n{completed.stderr}"
    )
    assert "Windows fatal exception" not in completed.stderr
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["status"] == "succeeded"
    assert result["execution"]["completion_marker_found"] is True
    assert all(result["preparation"]["checks"].values())
    assert result["products"]["status"]["all_required_pathnames_read"] is True
    assert Path(result["preparation"]["workspace"]["project_file"]).is_file()
