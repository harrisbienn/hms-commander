"""Identity-bound provisional static initialization; no native I/O or execution."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from .HmsControl import HmsControl
from .HmsPrj import HmsPrj
from .HmsScenario import HmsScenario, HmsScenarioWorkspace
from ._parsing import HmsFileParser

PLAN_SCHEMA = "hms-commander/forecast-initialization/1.0"
MODEL_SCHEMA = "hms-commander/initialization-model/1.0"
EVIDENCE_SCHEMA = "hms-commander/forecast-initialization-evidence/1.0"
_FILES = {"project", "run", "basin", "met", "control", "grid"}
_NAMES = {"run", "basin", "met", "control", "grid"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def _fields(value: Any, names: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != names:
        raise ValueError(f"{label} requires exactly {sorted(names)}")
    return value


def _hash(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise ValueError("Expected a lowercase SHA-256 identity")
    return value


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError("Expected a nonempty string without surrounding whitespace")
    return value


def _json(path: Path, expected: str | None = None) -> dict[str, Any]:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate JSON field: {key}")
            result[key] = value
        return result

    with path.open("rb") as stream:
        raw = stream.read(4 * 1024 * 1024 + 1)
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError("Initialization JSON exceeds 4 MiB")
    if expected is not None and hashlib.sha256(raw).hexdigest() != _hash(expected):
        raise ValueError(f"Initialization evidence identity differs: {path.name}")
    value = json.loads(raw, object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise ValueError("Initialization JSON must be an object")
    _canonical(value)  # Reject non-finite JSON constants before interpreting them.
    return value


def _utc(value: Any) -> datetime:
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:00(?:Z|\+00:00)", value
    ):
        raise ValueError(
            "Initialization clocks require explicit UTC whole-minute timestamps"
        )
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _reference(value: Any) -> dict[str, str]:
    value = _fields(value, {"path", "sha256"}, "Initialization reference")
    return {
        "path": str(Path(_text(value["path"])).resolve()),
        "sha256": _hash(value["sha256"]),
    }


def normalize(value: Any) -> dict[str, dict[str, str]]:
    value = _fields(value, {"evidence", "model_manifest"}, "initialization")
    return {key: _reference(value[key]) for key in ("evidence", "model_manifest")}


def _one(frame, name: str, label: str) -> Mapping[str, Any]:
    if "name" not in frame:
        raise ValueError(f"No {label} entries exist")
    found = frame[frame["name"] == name]
    if len(found) != 1:
        raise ValueError(f"Expected exactly one {label} named {name!r}")
    return found.iloc[0]


def _run_attributes(path: Path, name: str) -> dict[str, str]:
    matches = [
        row
        for row in HmsFileParser.find_all_blocks(HmsFileParser.read_file(path), "Run")
        if row[1] == name
    ]
    if len(matches) != 1:
        raise ValueError("Selected run must have exactly one native text block")
    attributes = matches[0][2]
    if attributes.get("Save State Type") != "None":
        raise ValueError(
            "Provisional static initialization requires explicit Save State Type: None"
        )
    # Save State Type=None is emitted by existing package builders and observed
    # in accepted intake evidence. No other state grammar is admitted here.
    seen = set()
    for line in matches[0][0].group(3).splitlines():
        key, separator, value = line.strip().partition(":")
        if separator:
            if key in seen:
                raise ValueError("Ambiguous duplicate selected run control")
            seen.add(key)
        if separator and "state" in key.casefold():
            if key != "Save State Type" or value.strip() != "None":
                raise ValueError(
                    "Provisional static initialization refuses saved/restart state controls"
                )
    return attributes


def _selected(request: Mapping[str, Any], folder: Path | None = None, workspace=None):
    source = request["source_model"]
    project_path = Path(source["project"])
    root = (
        folder or (project_path.parent if project_path.is_file() else project_path)
    ).resolve()
    if len(list(root.glob("*.hms"))) != 1:
        raise ValueError("Initialization requires one unambiguous HMS project file")
    project = HmsPrj().initialize(root, load_dss_metadata=False)
    if project.project_file is None:
        raise ValueError("Source HMS project is unresolved")
    if (
        folder is None
        and project_path.is_file()
        and project.project_file.resolve() != project_path.resolve()
    ):
        raise ValueError("Requested project differs from the package-selected project")
    name = workspace.run_name if workspace else source["run"]
    run = _one(project.run_df, name, "run")
    names = {
        "run": name,
        "basin": _text(run["basin_model"]),
        "met": workspace.met_name if workspace else source.get("met", run["met_model"]),
        "control": (
            workspace.control_name
            if workspace
            else source.get("control", run["control_spec"])
        ),
        "grid": workspace.grid_name if workspace else source["grid"],
    }
    paths = {
        "project": project.project_file.resolve(),
        "run": Path(run["full_path"]).resolve(),
    }
    for key, frame in (
        ("basin", project.basin_df),
        ("met", project.met_df),
        ("control", project.control_df),
    ):
        paths[key] = Path(_one(frame, names[key], key)["full_path"]).resolve()
    paths["grid"] = HmsScenario._resolve_grid_file(root).resolve()
    for label, path in paths.items():
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError(
                f"Selected {label} must be a retained file inside the model root"
            )
    blocks = HmsFileParser.find_all_blocks(
        HmsFileParser.read_file(paths["project"]), "Project"
    )
    if len(blocks) != 1:
        raise ValueError("Forecast initialization requires one selected project block")
    grids = [
        row
        for row in HmsFileParser.find_all_blocks(
            HmsFileParser.read_file(paths["grid"]), "Grid"
        )
        if row[1] == names["grid"]
    ]
    if len(grids) != 1:
        raise ValueError("Selected precipitation grid must be unambiguous")
    _unique_block(paths["control"], "Control", names["control"])
    _unique_block(paths["met"], "Meteorology", names["met"])
    return root, names, paths, _run_attributes(paths["run"], names["run"])


def verify_source(request: Mapping[str, Any]) -> dict[str, Any]:
    """Verify independently selected model files and provisional initialization."""
    references = request["initialization"]
    plan = _json(Path(references["evidence"]["path"]), references["evidence"]["sha256"])
    manifest = _json(
        Path(references["model_manifest"]["path"]),
        references["model_manifest"]["sha256"],
    )
    _fields(
        plan,
        {
            "schema",
            "method",
            "disposition",
            "valid_at",
            "model_manifest_sha256",
            "engine_sha256",
            "forcing_sha256",
            "forcing_pathname",
            "clock",
            "warm_up",
            "predecessor",
            "observed_state_equivalent",
            "forecast_eligible",
        },
        "Initialization plan",
    )
    if (
        plan["schema"] != PLAN_SCHEMA
        or plan["method"] != "static"
        or plan["disposition"] != "provisional-static"
        or plan["predecessor"] is not None
        or plan["observed_state_equivalent"] is not False
        or plan["forecast_eligible"] is not False
    ):
        raise ValueError(
            "Only provisional static initialization without state promotion is supported"
        )
    if _hash(plan["model_manifest_sha256"]) != references["model_manifest"]["sha256"]:
        raise ValueError(
            "Initialization plan differs from the independently selected model manifest"
        )
    window = request["model_window"]
    if window["time_zone"] != "UTC":
        raise ValueError(
            "Forecast initialization supports only an explicit UTC model clock"
        )
    if plan["clock"] != {
        "physical_time_zone": "UTC",
        "model_wall_time_zone": "UTC",
        "forcing_time_zone": "UTC",
        "time_shift": "none",
    }:
        raise ValueError(
            "Initialization requires an explicit common UTC clock without shifting"
        )
    start = datetime.fromisoformat(window["start"]).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(window["end"]).replace(tzinfo=timezone.utc)
    if (
        start.second
        or start.microsecond
        or end.second
        or end.microsecond
        or _utc(plan["valid_at"]) != start
    ):
        raise ValueError(
            "Initialization valid instant differs from the physical model start"
        )
    if (end - start).total_seconds() % (window["interval_minutes"] * 60):
        raise ValueError("Model end must lie on the declared computation interval")
    if (start.hour * 60 + start.minute) % window["interval_minutes"]:
        raise ValueError("Model start must lie on the declared computation interval")
    if (
        _hash(plan["forcing_sha256"]) != request["forcing"]["sha256"]
        or plan["forcing_pathname"] != request["forcing"]["pathname"]
    ):
        raise ValueError(
            "Initialization forcing identity or selector differs from the request"
        )
    forcing_path = Path(request["forcing"]["dss"])
    if _sha256(forcing_path) != plan["forcing_sha256"]:
        raise ValueError("Initialization forcing bytes differ")
    executable = request["execution"].get("hms_executable")
    if (
        executable is None
        or not Path(executable).is_file()
        or _sha256(Path(executable)) != _hash(plan["engine_sha256"])
    ):
        raise ValueError(
            "Initialization requires the exact selected HMS executable file"
        )
    warm_up = plan["warm_up"]
    if warm_up is not None:
        _fields(warm_up, {"start", "end", "forcing_sha256"}, "Declared warm-up")
        stop = _utc(warm_up["end"])
        if (
            _utc(warm_up["start"]) != start
            or not start < stop < end
            or (stop - start).total_seconds() % (window["interval_minutes"] * 60)
        ):
            raise ValueError(
                "Declared warm-up must be an aligned prefix strictly inside the model window"
            )
        if _hash(warm_up["forcing_sha256"]) != plan["forcing_sha256"]:
            raise ValueError(
                "Declared warm-up forcing differs from the selected forcing"
            )
    _fields(manifest, {"schema", "names", "files"}, "Initialization model manifest")
    if manifest["schema"] != MODEL_SCHEMA:
        raise ValueError("Unsupported initialization model manifest")
    _fields(manifest["names"], _NAMES, "Model names")
    _fields(manifest["files"], _FILES, "Model files")
    root, names, paths, _ = _selected(request)
    if manifest["names"] != names:
        raise ValueError(
            "Selected model/run names differ from the initialization manifest"
        )
    identities = {}
    for key, path in paths.items():
        supplied = _fields(manifest["files"][key], {"path", "sha256"}, f"Model {key}")
        relative = PurePosixPath(_text(supplied["path"]))
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or "\\" in supplied["path"]
            or ":" in supplied["path"]
            or str(relative) != supplied["path"]
        ):
            raise ValueError("Model manifest paths must be portable relative paths")
        if root.joinpath(*relative.parts).resolve() != path or _sha256(path) != _hash(
            supplied["sha256"]
        ):
            raise ValueError(
                f"Actual selected {key} differs from its initialization identity"
            )
        identities[key] = dict(supplied)
    if (
        identities["project"]["sha256"]
        != request["source_model"]["project_file_sha256"]
    ):
        raise ValueError("Source project identity differs from the request")
    return {
        "schema": EVIDENCE_SCHEMA,
        "status": "source_verified",
        "method": "static",
        "disposition": "provisional-static",
        "valid_at": plan["valid_at"],
        "model_manifest_sha256": references["model_manifest"]["sha256"],
        "initialization_plan_sha256": references["evidence"]["sha256"],
        "model_files": identities,
        "names": names,
        "engine": {"path": executable, "sha256": plan["engine_sha256"]},
        "forcing": dict(request["forcing"]),
        "model_window": dict(window),
        "clock": dict(plan["clock"]),
        "native_clock": _clock_configuration(paths),
        "gage_inputs": request.get("gage_inputs", []),
        "warm_up": warm_up,
        "warm_up_status": "declared-not-qualified" if warm_up else "not-requested",
        "predecessor": None,
        "observed_state_equivalent": False,
        "forecast_eligible": False,
    }


def _met_invariants(path: Path) -> list[str]:
    # HmsMet.clone_met/set_precipitation may change only these metadata/forcing
    # lines. All remaining meteorologic parameters, including initial values,
    # must survive preparation unchanged.
    allowed = {
        "Meteorology",
        "Description",
        "Precipitation Method",
        "Precip Method",
        "Precip Grid Name",
        "Precipitation Grid",
    }
    return [
        line.strip()
        for line in HmsFileParser.read_file(path).splitlines()
        if line.strip() and line.strip().partition(":")[0] not in allowed
    ]


def _clock_configuration(paths: Mapping[str, Path]) -> dict[str, Any]:
    """Retain clock labels; do not mistake labels or absence for native proof."""
    labels = {}
    for role in ("project", "control", "met"):
        selected = {}
        for line in HmsFileParser.read_file(paths[role]).splitlines():
            key, separator, value = line.strip().partition(":")
            if not separator:
                continue
            if "shift" in key.casefold() or "normalize" in key.casefold():
                if key in selected:
                    raise ValueError("Ambiguous native time-shift control")
                if (
                    role != "control"
                    or key != "Grid Write Time Shift"
                    or value.strip() != "0"
                ):
                    raise ValueError(
                        "Unsupported native time-shift control for declared UTC initialization"
                    )
                selected[key] = value.strip()
            elif "time zone" in key.casefold():
                if key in selected:
                    raise ValueError("Ambiguous native time-zone label")
                selected[key] = value.strip()
        labels[role] = selected
    return {
        "retained_labels": labels,
        "status": "caller-declared",
        "native_clock_verified": False,
    }


def _unique_block(path: Path, kind: str, name: str) -> dict[str, str]:
    matches = [
        row
        for row in HmsFileParser.find_all_blocks(HmsFileParser.read_file(path), kind)
        if row[1] == name
    ]
    if len(matches) != 1:
        raise ValueError(f"Expected one selected {kind} text block")
    keys = set()
    for line in matches[0][0].group(3).splitlines():
        key, separator, _ = line.strip().partition(":")
        if separator and key in keys:
            raise ValueError(f"Duplicate selected {kind} attribute: {key}")
        keys.add(key)
    return matches[0][2]


def _prepared_forcing(
    paths: Mapping[str, Path], names: Mapping[str, str], workspace
) -> None:
    """Bind actual met/grid selection to the staged file, not a loose substring."""
    meteorology = _unique_block(paths["met"], "Meteorology", names["met"])
    if meteorology.get("Precipitation Method") != "Gridded Precipitation":
        raise ValueError("Prepared meteorologic precipitation method differs")
    bindings = []
    for line in HmsFileParser.read_file(paths["met"]).splitlines():
        key, separator, value = line.strip().partition(":")
        if separator and key in {"Precip Grid Name", "Precipitation Grid"}:
            bindings.append(value.strip())
    if bindings != [names["grid"]]:
        raise ValueError(
            "Prepared meteorologic grid selection is ambiguous or mismatched"
        )
    grid = _unique_block(paths["grid"], "Grid", names["grid"])
    files = [grid[key] for key in ("DSS File Name", "Filename") if key in grid]
    selectors = [grid[key] for key in ("DSS Pathname", "Pathname") if key in grid]
    if len(files) != 1 or selectors != [workspace.precipitation_pathname]:
        raise ValueError("Prepared selected grid file or exact selector differs")
    selected = (workspace.project_folder / files[0].replace("\\", "/")).resolve()
    if (
        not selected.is_relative_to(workspace.project_folder.resolve())
        or selected != workspace.precipitation_file.resolve()
    ):
        raise ValueError(
            "Prepared selected grid does not reference the staged forcing file"
        )


def verify_prepared(
    request: Mapping[str, Any], evidence: dict[str, Any], workspace
) -> dict[str, Any]:
    """Recheck source and exact preserved initialization before engine execution."""
    current = verify_source(request)
    if _canonical(current) != _canonical(evidence):
        raise ValueError("Initialization inputs changed during preparation")
    _, _, source_paths, source_run = _selected(request)
    _, names, paths, prepared_run = _selected(
        request, workspace.project_folder, workspace
    )
    _unique_block(paths["control"], "Control", names["control"])
    _prepared_forcing(paths, names, workspace)
    if _clock_configuration(source_paths) != _clock_configuration(paths):
        raise ValueError("Prepared clock labels or controls differ from the source")

    def project_attributes(path):
        blocks = HmsFileParser.find_all_blocks(HmsFileParser.read_file(path), "Project")
        attributes = _unique_block(path, "Project", blocks[0][1])
        return {
            key: value
            for key, value in attributes.items()
            if key not in {"Last Modified Date", "Last Modified Time"}
        }

    if project_attributes(source_paths["project"]) != project_attributes(
        paths["project"]
    ):
        raise ValueError("Prepared project changed fields outside execution metadata")
    if (
        names["run"] != evidence["names"]["run"]
        or names["basin"] != evidence["names"]["basin"]
    ):
        raise ValueError(
            "Prepared run or basin differs from the selected initialization"
        )
    if _sha256(paths["basin"]) != evidence["model_files"]["basin"]["sha256"]:
        raise ValueError("Prepared basin initialization parameters changed")
    if _met_invariants(paths["met"]) != _met_invariants(source_paths["met"]):
        raise ValueError("Prepared meteorologic initialization parameters changed")
    allowed_control = {
        "Control",
        "Description",
        "Start Date",
        "Start Time",
        "End Date",
        "End Time",
        "Time Interval",
    }

    def control_invariants(path):
        return [
            line.strip()
            for line in HmsFileParser.read_file(path).splitlines()
            if line.strip() and line.strip().partition(":")[0] not in allowed_control
        ]

    if control_invariants(source_paths["control"]) != control_invariants(
        paths["control"]
    ):
        raise ValueError(
            "Prepared control changed fields outside the requested model window"
        )
    allowed_run = {
        "Precip",
        "Precipitation",
        "Control",
        "DSS File",
        "Log File",
        "Last Execution Date",
        "Last Execution Time",
    }
    if {key: value for key, value in source_run.items() if key not in allowed_run} != {
        key: value for key, value in prepared_run.items() if key not in allowed_run
    }:
        raise ValueError("Prepared run changed initialization controls")
    control = HmsControl.get_time_window(paths["control"])
    if (
        control["start_date"].isoformat(timespec="seconds")
        != request["model_window"]["start"]
        or control["end_date"].isoformat(timespec="seconds")
        != request["model_window"]["end"]
    ):
        raise ValueError(
            "Prepared control window differs from the initialization clock"
        )
    if HmsControl.get_time_interval(paths["control"]) != str(
        request["model_window"]["interval_minutes"]
    ):
        raise ValueError(
            "Prepared control interval differs from the initialization clock"
        )
    if _sha256(workspace.precipitation_file) != evidence["forcing"]["sha256"]:
        raise ValueError("Prepared forcing differs from initialization evidence")
    return {
        **evidence,
        "status": "prepared",
        "prepared_files": {
            key: {"path": str(path), "sha256": _sha256(path)}
            for key, path in paths.items()
        },
    }


def verify_reuse(
    request: Mapping[str, Any], result: dict[str, Any], evidence: dict[str, Any]
) -> None:
    """Authenticate both source admission and retained prepared configuration."""
    preparation = result.get("preparation", {})
    raw_workspace = dict(preparation.get("workspace", {}))
    for key in (
        "source_project",
        "project_folder",
        "project_file",
        "precipitation_source",
        "precipitation_file",
        "output_dss",
        "log_file",
    ):
        raw_workspace[key] = Path(raw_workspace[key])
    workspace = HmsScenarioWorkspace(**raw_workspace)
    if workspace.project_folder.resolve() != Path(request["workspace"]).resolve():
        raise ValueError("Retained initialization workspace differs from request")
    HmsScenario.validate_workspace(workspace)
    current = verify_prepared(request, evidence, workspace)
    before = dict(preparation.get("initialization", {}))
    _fields(
        before, set(evidence) | {"prepared_files"}, "Precompute initialization receipt"
    )
    if before["status"] != "prepared":
        raise ValueError("Precompute initialization receipt must have prepared status")
    _fields(before["prepared_files"], _FILES, "Precompute initialization files")
    for role in _FILES:
        identity = _fields(
            before["prepared_files"][role],
            {"path", "sha256"},
            "Precompute file identity",
        )
        _hash(identity["sha256"])
        if identity["path"] != current["prepared_files"][role]["path"]:
            raise ValueError(
                "Precompute selected file path differs from retained selection"
            )
        if (
            role not in {"project", "run"}
            and identity != current["prepared_files"][role]
        ):
            raise ValueError(
                "Precompute static file identity differs from retained configuration"
            )
    before.pop("prepared_files", None)
    before["status"] = "source_verified"
    if _canonical(before) != _canonical(evidence):
        raise ValueError("Retained precompute initialization source evidence differs")
    if _canonical(current) != _canonical(preparation.get("retained_initialization")):
        raise ValueError(
            "Retained prepared initialization evidence differs from current inputs"
        )
