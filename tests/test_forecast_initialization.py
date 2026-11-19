"""Portable initialization evidence tests; only native compute/export are faked."""

import hashlib
import json
from pathlib import Path

import pytest

from hms_commander import HmsForecastInitialization, HmsScenario, HmsScenarioWorker
from hms_commander import _forecast_initialization as initialization
from test_scenario import _write_project
from test_scenario_worker import _install_success_fakes, _request


def _hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _write(path, payload):
    Path(path).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _fixture(tmp_path):
    request, request_path, result_path = _request(tmp_path)
    source = _write_project(tmp_path / "model")
    project = source / "Example.hms"
    project.write_text(
        project.read_text().replace(
            "     Version:", "     Time Zone ID: America/Chicago\n     Version:", 1
        )
    )
    run = source / "Example.run"
    run.write_text(
        run.read_text().replace(
            "     Basin:", "     Save State Type: None\n     Basin:"
        )
    )
    engine = tmp_path / "engine.exe"
    engine.write_bytes(b"synthetic engine identity; never executed")
    request["schema"] = HmsScenarioWorker.FORECAST_REQUEST_SCHEMA
    request["source_model"].update(
        project=str(source),
        project_file_sha256=_hash(project),
        run_name_policy="preserve-source",
    )
    request["execution"]["hms_executable"] = str(engine)
    request["model_window"]["time_zone"] = "UTC"
    request["products"]["required_pathnames"][0][
        "pathname"
    ] = "//OUTLET/FLOW//5Minute/RUN:BaselineRun/"
    model = {
        "schema": initialization.MODEL_SCHEMA,
        "names": {
            "run": "BaselineRun",
            "basin": "BaselineBasin",
            "met": "BaselineMet",
            "control": "BaselineControl",
            "grid": "BaselineGrid",
        },
        "files": {
            key: {"path": name, "sha256": _hash(source / name)}
            for key, name in {
                "project": "Example.hms",
                "run": "Example.run",
                "basin": "BaselineBasin.basin",
                "met": "BaselineMet.met",
                "control": "BaselineControl.control",
                "grid": "Example.grid",
            }.items()
        },
    }
    model_path = tmp_path / "model.json"
    _write(model_path, model)
    plan = json.loads(
        (
            Path(__file__).parent / "fixtures/forecast_initialization/plan.json"
        ).read_text()
    )
    plan.update(
        valid_at=request["model_window"]["start"] + "Z",
        model_manifest_sha256=_hash(model_path),
        engine_sha256=_hash(engine),
        forcing_sha256=request["forcing"]["sha256"],
        forcing_pathname=request["forcing"]["pathname"],
    )
    plan_path = tmp_path / "plan.json"
    _write(plan_path, plan)
    request["initialization"] = {
        "evidence": {"path": str(plan_path), "sha256": _hash(plan_path)},
        "model_manifest": {"path": str(model_path), "sha256": _hash(model_path)},
    }
    _write(request_path, request)
    return request, request_path, result_path, plan


def _replace_plan(request, request_path, plan):
    path = request["initialization"]["evidence"]["path"]
    _write(path, plan)
    request["initialization"]["evidence"]["sha256"] = _hash(path)
    _write(request_path, request)


def _native_fakes(monkeypatch, request):
    prepare = HmsScenario.prepare_workspace
    validate = HmsScenario.validate_workspace
    calls = _install_success_fakes(monkeypatch, request)
    fake_execute = HmsScenario.execute
    monkeypatch.setattr(HmsScenario, "prepare_workspace", staticmethod(prepare))
    monkeypatch.setattr(HmsScenario, "validate_workspace", staticmethod(validate))

    def execute(workspace, **kwargs):
        workspace.output_dss.parent.mkdir(exist_ok=True)
        workspace.output_dss.write_bytes(b"synthetic completed output")
        workspace.log_file.write_text(
            'NOTE 15302: Finished computing simulation run "BaselineRun"\n'
        )
        return fake_execute(workspace, **kwargs)

    monkeypatch.setattr(HmsScenario, "execute", staticmethod(execute))
    return calls


def test_source_receipt_and_worker_clone_are_rechecked(tmp_path, monkeypatch):
    request, path, result_path, _ = _fixture(tmp_path)
    source_before = {
        p: p.read_bytes()
        for p in Path(request["source_model"]["project"]).rglob("*")
        if p.is_file()
    }
    receipt = HmsForecastInitialization.prepare(path, tmp_path / "receipt.json")
    evidence = HmsForecastInitialization.verify(path, receipt)
    assert evidence["status"] == "source_verified"
    assert evidence["forecast_eligible"] is False
    assert not Path(request["workspace"]).exists()
    assert HmsForecastInitialization.prepare(path, receipt) == receipt
    calls = _native_fakes(monkeypatch, request)
    assert HmsScenarioWorker.run(path, result_path) == 0, result_path.read_text()
    result = json.loads(result_path.read_text())
    assert result["schema"] == HmsScenarioWorker.FORECAST_RESULT_SCHEMA
    assert result["preparation"]["initialization"]["status"] == "prepared"
    assert result["preparation"]["initialization"]["observed_state_equivalent"] is False
    assert result["preparation"]["run_identity"]["prepared_run"] == "BaselineRun"
    assert HmsScenarioWorker.run(path, result_path) == 0
    assert calls["execute"] == 1
    assert all(p.read_bytes() == contents for p, contents in source_before.items())
    Path(request["source_model"]["project"], "BaselineBasin.basin").write_text(
        "changed basin"
    )
    assert HmsScenarioWorker.run(path, result_path) != 0
    assert calls["execute"] == 1
    with pytest.raises(ValueError, match="basin"):
        HmsForecastInitialization.verify(path, receipt)


@pytest.mark.parametrize(
    "key,value",
    [
        ("valid_at", "2019-09-18T12:00:00Z"),
        ("valid_at", "2019-09-18T14:00:00Z"),
        ("model_manifest_sha256", "0" * 64),
        ("engine_sha256", "0" * 64),
        ("forcing_sha256", "0" * 64),
        ("forcing_pathname", "/OTHER/"),
        ("method", "saved-state"),
        ("method", "nwm-wetness"),
        ("predecessor", "previous-run"),
        ("observed_state_equivalent", True),
        ("forecast_eligible", True),
        ("disposition", "accepted-state"),
    ],
)
def test_invalid_plan_refuses_before_compute(tmp_path, monkeypatch, key, value):
    request, path, result_path, plan = _fixture(tmp_path)
    plan[key] = value
    _replace_plan(request, path, plan)
    calls = _native_fakes(monkeypatch, request)
    assert HmsScenarioWorker.run(path, result_path) == 2
    assert calls["execute"] == 0
    assert not Path(request["workspace"]).exists()


def test_declared_warm_up_is_not_verified_spinup(tmp_path):
    request, path, _, plan = _fixture(tmp_path)
    plan["warm_up"] = {
        "start": plan["valid_at"],
        "end": "2019-09-18T14:00:00Z",
        "forcing_sha256": request["forcing"]["sha256"],
    }
    _replace_plan(request, path, plan)
    receipt = HmsForecastInitialization.prepare(path, tmp_path / "receipt.json")
    evidence = HmsForecastInitialization.verify(path, receipt)
    assert evidence["warm_up"] == plan["warm_up"]
    assert evidence["warm_up_status"] == "declared-not-qualified"
    assert evidence["observed_state_equivalent"] is False


@pytest.mark.parametrize(
    "start,end",
    [
        ("2019-09-18T12:00:00Z", "2019-09-18T14:00:00Z"),
        ("2019-09-18T13:00:00Z", "2019-09-18T13:03:00Z"),
        ("2019-09-18T13:00:00Z", "2019-09-19T13:00:00Z"),
    ],
)
def test_bad_warm_up_bounds_refused(tmp_path, start, end):
    request, path, _, plan = _fixture(tmp_path)
    plan["warm_up"] = {
        "start": start,
        "end": end,
        "forcing_sha256": request["forcing"]["sha256"],
    }
    _replace_plan(request, path, plan)
    with pytest.raises(ValueError, match="warm-up"):
        HmsForecastInitialization.prepare(path, tmp_path / "receipt.json")


@pytest.mark.parametrize(
    "role",
    [
        "project",
        "run",
        "basin",
        "met",
        "control",
        "grid",
        "engine",
        "forcing",
        "plan",
        "manifest",
    ],
)
def test_changed_admitted_inputs_refuse_identical_result_reuse(
    tmp_path, monkeypatch, role
):
    request, path, result_path, _ = _fixture(tmp_path)
    calls = _native_fakes(monkeypatch, request)
    assert HmsScenarioWorker.run(path, result_path) == 0, result_path.read_text()
    before = result_path.read_bytes()
    if role == "engine":
        target = Path(request["execution"]["hms_executable"])
    elif role == "forcing":
        target = Path(request["forcing"]["dss"])
    elif role in {"plan", "manifest"}:
        target = Path(
            request["initialization"][
                "evidence" if role == "plan" else "model_manifest"
            ]["path"]
        )
    else:
        model = json.loads(
            Path(request["initialization"]["model_manifest"]["path"]).read_text()
        )
        target = Path(request["source_model"]["project"]) / model["files"][role]["path"]
    target.write_bytes(target.read_bytes() + b"\nchanged")
    assert HmsScenarioWorker.run(path, result_path) != 0
    assert result_path.read_bytes() == before
    assert calls["execute"] == 1


@pytest.mark.parametrize(
    "field",
    [
        "Save State Type: Specified Date",
        "Start State Name: prior",
        "State File: previous.state",
    ],
)
def test_unsupported_native_state_controls_refused(tmp_path, field):
    request, path, _, plan = _fixture(tmp_path)
    source = Path(request["source_model"]["project"])
    run = source / "Example.run"
    run.write_text(
        run.read_text().replace("     Save State Type: None", "     " + field)
    )
    manifest_path = Path(request["initialization"]["model_manifest"]["path"])
    model = json.loads(manifest_path.read_text())
    model["files"]["run"]["sha256"] = _hash(run)
    _write(manifest_path, model)
    request["initialization"]["model_manifest"]["sha256"] = _hash(manifest_path)
    plan["model_manifest_sha256"] = _hash(manifest_path)
    _replace_plan(request, path, plan)
    with pytest.raises(ValueError, match="static initialization"):
        HmsForecastInitialization.prepare(path, tmp_path / "receipt.json")


def test_prepared_basin_edit_refuses_before_compute(tmp_path, monkeypatch):
    request, path, result_path, _ = _fixture(tmp_path)
    calls = _native_fakes(monkeypatch, request)
    real_prepare = HmsScenario.prepare_workspace

    def changed(*args, **kwargs):
        workspace = real_prepare(*args, **kwargs)
        (workspace.project_folder / "BaselineBasin.basin").write_text(
            "Basin: BaselineBasin\n     Initial Deficit: 99\nEnd:\n"
        )
        return workspace

    monkeypatch.setattr(HmsScenario, "prepare_workspace", staticmethod(changed))
    assert HmsScenarioWorker.run(path, result_path) == 2
    assert calls["execute"] == 0
    assert (
        "basin initialization"
        in json.loads(result_path.read_text())["error"]["message"]
    )


@pytest.mark.parametrize("role", ["met", "control", "run", "grid", "forcing"])
def test_prepared_input_changes_refuse_reuse(tmp_path, monkeypatch, role):
    request, path, result_path, _ = _fixture(tmp_path)
    calls = _native_fakes(monkeypatch, request)
    assert HmsScenarioWorker.run(path, result_path) == 0
    result = json.loads(result_path.read_text())
    prepared = result["preparation"]
    target = Path(
        prepared["workspace"]["precipitation_file"]
        if role == "forcing"
        else prepared["initialization"]["prepared_files"][role]["path"]
    )
    target.write_bytes(target.read_bytes() + b"\nchanged")
    assert HmsScenarioWorker.run(path, result_path) != 0
    assert calls["execute"] == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("start", "2019-09-18T13:00:00.001"),
        ("start", "2019-09-18T13:00:01"),
        ("end", "2019-09-19T13:03:00"),
        ("time_zone", "America/Chicago"),
    ],
)
def test_ambiguous_model_clock_refused(tmp_path, field, value):
    request, path, _, _ = _fixture(tmp_path)
    request["model_window"][field] = value
    _write(path, request)
    with pytest.raises(ValueError):
        HmsForecastInitialization.prepare(path, tmp_path / "receipt.json")


def test_manifest_must_bind_actual_selected_source(tmp_path):
    request, path, _, plan = _fixture(tmp_path)
    manifest_path = Path(request["initialization"]["model_manifest"]["path"])
    model = json.loads(manifest_path.read_text())
    model["files"]["basin"]["path"] = "unrelated.basin"
    _write(manifest_path, model)
    request["initialization"]["model_manifest"]["sha256"] = _hash(manifest_path)
    plan["model_manifest_sha256"] = _hash(manifest_path)
    _replace_plan(request, path, plan)
    with pytest.raises(ValueError, match="selected basin"):
        HmsForecastInitialization.prepare(path, tmp_path / "receipt.json")


@pytest.mark.parametrize(
    "role,field",
    [
        ("met", "Time Shift: 6"),
        ("met", "Time Shift Method: Normalize Start"),
        ("control", "Grid Write Time Shift: 6"),
    ],
)
def test_unsupported_clock_controls_refuse(tmp_path, role, field):
    request, path, _, plan = _fixture(tmp_path)
    manifest_path = Path(request["initialization"]["model_manifest"]["path"])
    model = json.loads(manifest_path.read_text())
    target = Path(request["source_model"]["project"]) / model["files"][role]["path"]
    target.write_text(target.read_text().replace("End:", "     " + field + "\nEnd:", 1))
    model["files"][role]["sha256"] = _hash(target)
    _write(manifest_path, model)
    request["initialization"]["model_manifest"]["sha256"] = _hash(manifest_path)
    plan["model_manifest_sha256"] = _hash(manifest_path)
    _replace_plan(request, path, plan)
    with pytest.raises(ValueError, match="time-shift"):
        HmsForecastInitialization.prepare(path, tmp_path / "receipt.json")


def test_conflicting_source_receipt_is_never_overwritten(tmp_path):
    _, path, _, _ = _fixture(tmp_path)
    receipt = tmp_path / "receipt.json"
    receipt.write_text('{"unrelated": true}')
    with pytest.raises(FileExistsError):
        HmsForecastInitialization.prepare(path, receipt)
    assert receipt.read_text() == '{"unrelated": true}'


def test_schema_resources_preserve_legacy_and_name_successors():
    contracts = Path(initialization.__file__).parent / "contracts"
    for stem, identity in [
        ("scenario-worker-request-v1.4", HmsScenarioWorker.FORECAST_REQUEST_SCHEMA),
        ("scenario-worker-result-v1.1", HmsScenarioWorker.FORECAST_RESULT_SCHEMA),
        ("forecast-initialization-v1.0", initialization.PLAN_SCHEMA),
        ("initialization-model-v1.0", initialization.MODEL_SCHEMA),
        ("forecast-initialization-evidence-v1.0", initialization.EVIDENCE_SCHEMA),
    ]:
        schema = json.loads((contracts / (stem + ".schema.json")).read_text())
        assert schema["$id"] == identity
        assert schema["properties"]["schema"]["const"] == identity
    legacy = json.loads(
        (contracts / "scenario-worker-request-v1.3.schema.json").read_text()
    )
    assert "initialization" not in legacy["properties"]


def test_native_execution_metadata_does_not_invalidate_reuse(tmp_path, monkeypatch):
    request, path, result_path, _ = _fixture(tmp_path)
    calls = _native_fakes(monkeypatch, request)
    fake_execute = HmsScenario.execute

    def execute(workspace, **kwargs):
        artifact = fake_execute(workspace, **kwargs)
        run = workspace.project_folder / "Example.run"
        run.write_text(
            run.read_text().replace(
                "End:",
                "     Last Execution Date: 19 September 2019\n     Last Execution Time: 13:01:00\nEnd:",
                1,
            )
        )
        project = workspace.project_file
        project.write_text(
            project.read_text().replace(
                "Project: Example",
                "Project: Example\n     Last Modified Date: 19 September 2019",
            )
        )
        return artifact

    monkeypatch.setattr(HmsScenario, "execute", staticmethod(execute))
    assert HmsScenarioWorker.run(path, result_path) == 0, result_path.read_text()
    preparation = json.loads(result_path.read_text())["preparation"]
    assert (
        preparation["initialization"]["prepared_files"]["run"]
        != preparation["retained_initialization"]["prepared_files"]["run"]
    )
    assert HmsScenarioWorker.run(path, result_path) == 0
    assert calls["execute"] == 1


@pytest.mark.parametrize(
    "role,old,new",
    [
        (
            "grid",
            "DSS File Name: forcing\\forcing.dss",
            "DSS File Name: root_grid_input.dss",
        ),
        (
            "grid",
            "DSS Pathname: /AORC/GRID/PRECIPITATION///INCREMENTAL/",
            "DSS Pathname: /AORC/GRID/PRECIPITATION///INCREMENTAL/EXTRA/",
        ),
        (
            "met",
            "Precip Grid Name: FF_lwi-r3-rank-001_Precip",
            "Precip Grid Name: BaselineGrid",
        ),
    ],
)
def test_actual_prepared_forcing_selection_checked_before_compute(
    tmp_path, monkeypatch, role, old, new
):
    request, path, result_path, _ = _fixture(tmp_path)
    calls = _native_fakes(monkeypatch, request)
    real_prepare = HmsScenario.prepare_workspace

    def prepare(*args, **kwargs):
        workspace = real_prepare(*args, **kwargs)
        target = workspace.project_folder / (
            "Example.grid" if role == "grid" else workspace.met_name + ".met"
        )
        text = target.read_text()
        assert old in text
        target.write_text(text.replace(old, new))
        return workspace

    monkeypatch.setattr(HmsScenario, "prepare_workspace", staticmethod(prepare))
    assert HmsScenarioWorker.run(path, result_path) != 0
    assert calls["execute"] == 0


@pytest.mark.parametrize(
    "old,new",
    [
        ("     Save State Type: None\n", ""),
        (
            "     Save State Type: None",
            "     Save State Type: None\n     Save State Type: None",
        ),
        ("     Basin: BaselineBasin", "     Basin: Other\n     Basin: BaselineBasin"),
    ],
)
def test_missing_or_duplicate_state_and_run_controls_refuse(tmp_path, old, new):
    request, path, _, plan = _fixture(tmp_path)
    target = Path(request["source_model"]["project"]) / "Example.run"
    target.write_text(target.read_text().replace(old, new))
    manifest_path = Path(request["initialization"]["model_manifest"]["path"])
    model = json.loads(manifest_path.read_text())
    model["files"]["run"]["sha256"] = _hash(target)
    _write(manifest_path, model)
    request["initialization"]["model_manifest"]["sha256"] = _hash(manifest_path)
    plan["model_manifest_sha256"] = _hash(manifest_path)
    _replace_plan(request, path, plan)
    with pytest.raises(ValueError):
        HmsForecastInitialization.prepare(path, tmp_path / "receipt.json")


@pytest.mark.parametrize("change", ["status", "missing", "sha", "path", "basin"])
def test_precompute_receipt_corruption_refuses_reuse(tmp_path, monkeypatch, change):
    request, path, result_path, _ = _fixture(tmp_path)
    calls = _native_fakes(monkeypatch, request)
    assert HmsScenarioWorker.run(path, result_path) == 0
    result = json.loads(result_path.read_text())
    receipt = result["preparation"]["initialization"]
    if change == "status":
        receipt["status"] = "source_verified"
    elif change == "missing":
        del receipt["prepared_files"]
    elif change == "sha":
        receipt["prepared_files"]["run"]["sha256"] = "invalid"
    elif change == "path":
        receipt["prepared_files"]["run"]["path"] = "unrelated.run"
    else:
        receipt["prepared_files"]["basin"]["sha256"] = "0" * 64
    _write(result_path, result)
    assert HmsScenarioWorker.run(path, result_path) != 0
    assert calls["execute"] == 1


def test_receipt_cleanup_failure_preserves_published_success(
    tmp_path, monkeypatch, caplog
):
    _, path, _, _ = _fixture(tmp_path)
    unlink = Path.unlink

    def refuse_owned_temporary(self, *args, **kwargs):
        if self.parent == tmp_path and self.suffix == ".tmp":
            raise PermissionError("synthetic cleanup failure")
        return unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", refuse_owned_temporary)
    receipt = HmsForecastInitialization.prepare(path, tmp_path / "receipt.json")
    assert receipt.is_file()
    assert "owned initialization receipt temporary" in caplog.text
    assert (
        HmsForecastInitialization.verify(path, receipt)["status"] == "source_verified"
    )


def test_unaligned_start_refuses_even_when_valid_instant_matches(tmp_path):
    request, path, _, plan = _fixture(tmp_path)
    request["model_window"].update(
        start="2019-09-18T13:02:00", end="2019-09-19T13:02:00"
    )
    plan["valid_at"] = "2019-09-18T13:02:00Z"
    _replace_plan(request, path, plan)
    with pytest.raises(ValueError, match="Model start"):
        HmsForecastInitialization.prepare(path, tmp_path / "receipt.json")
