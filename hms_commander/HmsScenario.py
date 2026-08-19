"""Scenario workspace preparation and execution for external precipitation.

This module deliberately accepts ordinary paths and HMS component names.  It
does not import StormHub or ras-commander, which keeps hms-commander usable as
an independent package while giving a higher-level orchestrator a stable API.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Union

from .Decorators import log_call
from .HmsCmdr import HmsCmdr
from .HmsControl import HmsControl
from .HmsGrid import HmsGrid
from .HmsMet import HmsMet
from .HmsPrj import HmsPrj
from .HmsRun import HmsRun
from .HmsUtils import HmsUtils
from .LoggingConfig import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class HmsScenarioWorkspace:
    """Prepared, GUI-verifiable HMS scenario workspace."""

    scenario_id: str
    source_project: Path
    project_folder: Path
    project_file: Path
    run_name: str
    met_name: str
    control_name: str
    grid_name: str
    precipitation_source: Path
    precipitation_file: Path
    precipitation_pathname: str
    output_dss: Path
    log_file: Path
    clone_policy: str = "input-only"

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serializable workspace record."""
        return {
            key: str(value) if isinstance(value, Path) else value
            for key, value in asdict(self).items()
        }

    def write_manifest(self, path: Union[str, Path]) -> Path:
        """Write the prepared-workspace record atomically."""
        output = Path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(output.suffix + ".tmp")
        temporary.write_text(
            json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(output)
        return output


@dataclass(frozen=True)
class HmsRunArtifact:
    """Result of one HMS scenario execution."""

    scenario_id: str
    status: str
    run_name: str
    project_folder: Path
    dss_file: Path
    log_file: Path
    started_at: str
    finished_at: str
    dss_exists: bool
    dss_size_bytes: int
    process_succeeded: bool = False
    completion_marker_found: bool = False
    abort_marker_found: bool = False
    error_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serializable run artifact."""
        return {
            key: str(value) if isinstance(value, Path) else value
            for key, value in asdict(self).items()
        }

    def write_manifest(self, path: Union[str, Path]) -> Path:
        """Write the execution artifact atomically."""
        output = Path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(output.suffix + ".tmp")
        temporary.write_text(
            json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(output)
        return output


class HmsScenario:
    """Static namespace for preparing and executing isolated HMS scenarios."""

    @staticmethod
    @log_call
    def prepare_workspace(
        source_project: Union[str, Path],
        workspace: Union[str, Path],
        scenario_id: str,
        source_run: str,
        source_grid: str,
        precipitation_dss: Union[str, Path],
        precipitation_pathname: str,
        start_time: datetime,
        end_time: datetime,
        *,
        source_met: Optional[str] = None,
        source_control: Optional[str] = None,
        time_interval_minutes: Optional[int] = None,
        copy_precipitation: bool = True,
        include_generated_outputs: bool = False,
        overwrite: bool = False,
        hms_exe_path: Optional[Union[str, Path]] = None,
    ) -> HmsScenarioWorkspace:
        """Clone and configure an immutable HMS template for one scenario.

        ``start_time`` and ``end_time`` must already be expressed in the HMS
        project's local/model time zone.  Time-zone conversion belongs in the
        calling orchestrator, where the scenario contract is available.

        By default, the clone excludes root-level HMS computation artifacts
        and the root ``results`` directory. Required model inputs in nested
        folders, including DSS files under ``data``, remain part of the clone.
        Set ``include_generated_outputs=True`` only when historical results
        are intentionally needed in the scenario workspace.
        """
        source_folder = HmsScenario._resolve_project_folder(source_project)
        forcing_source = Path(precipitation_dss).resolve()
        workspace_path = Path(workspace).resolve()
        slug = HmsScenario._scenario_slug(scenario_id)

        if not forcing_source.is_file():
            raise FileNotFoundError(
                f"Precipitation DSS file not found: {forcing_source}"
            )
        HmsGrid._validate_dss_pathname(precipitation_pathname)
        if end_time <= start_time:
            raise ValueError("end_time must be later than start_time")
        if start_time.tzinfo is not None or end_time.tzinfo is not None:
            raise ValueError(
                "start_time and end_time must be naive datetimes in HMS model time"
            )

        HmsScenario._validate_copy_boundaries(source_folder, workspace_path)
        if workspace_path.exists():
            if not overwrite:
                raise FileExistsError(f"Workspace already exists: {workspace_path}")
            shutil.rmtree(workspace_path)

        workspace_path.parent.mkdir(parents=True, exist_ok=True)
        copy_ignore = None
        if not include_generated_outputs:
            copy_ignore = HmsScenario._generated_output_ignore(source_folder)
        HmsUtils.copy_project(
            source_folder,
            workspace_path,
            ignore=copy_ignore,
        )

        project = HmsPrj().initialize(workspace_path, hms_exe_path=hms_exe_path)
        run_config = project.get_run_configuration(source_run)
        if not run_config:
            raise ValueError(f"Source run '{source_run}' was not found")

        resolved_met = source_met or str(run_config.get("met_name", ""))
        resolved_control = source_control or str(run_config.get("control_name", ""))
        if not resolved_met:
            raise ValueError(f"Source run '{source_run}' has no meteorologic model")
        if not resolved_control:
            raise ValueError(f"Source run '{source_run}' has no control specification")

        met_name = f"FF_{slug}_Met"
        control_name = f"FF_{slug}_Control"
        run_name = f"FF_{slug}"
        grid_name = f"FF_{slug}_Precip"

        forcing_dir = workspace_path / "forcing"
        forcing_dir.mkdir(parents=True, exist_ok=True)
        if copy_precipitation:
            forcing_path = forcing_dir / forcing_source.name
            shutil.copy2(forcing_source, forcing_path)
            grid_dss_reference = HmsScenario._windows_relative_path(
                forcing_path.relative_to(workspace_path)
            )
        else:
            forcing_path = forcing_source
            grid_dss_reference = str(forcing_source)

        met_path = HmsMet.clone_met(
            resolved_met,
            met_name,
            description=f"FloodForecast scenario {scenario_id}",
            hms_object=project,
        )
        HmsMet.set_precipitation(
            met_path,
            "Gridded Precipitation",
            {"grid_name": grid_name},
            hms_object=project,
        )

        grid_file = HmsScenario._resolve_grid_file(project.project_folder)
        HmsGrid.clone_external_dss_grid(
            grid_file,
            source_grid,
            grid_name,
            grid_dss_reference,
            precipitation_pathname,
            description=f"FloodForecast scenario {scenario_id}",
        )

        control_path = HmsControl.clone_control(
            resolved_control,
            control_name,
            hms_object=project,
        )
        HmsControl.set_time_window(control_path, start_time, end_time)
        if time_interval_minutes is not None:
            if time_interval_minutes <= 0:
                raise ValueError("time_interval_minutes must be positive")
            HmsControl.set_time_interval(control_path, time_interval_minutes)

        output_dir = workspace_path / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_reference = rf"output\{slug}_hms.dss"
        HmsRun.clone_run(
            source_run,
            run_name,
            new_met=met_name,
            new_control=control_name,
            output_dss=output_reference,
            description=f"FloodForecast scenario {scenario_id}",
            hms_object=project,
        )
        project.initialize(project.project_folder, hms_exe_path=hms_exe_path)

        project_file = project.project_file
        if project_file is None:
            raise RuntimeError("Prepared project does not have an HMS project file")
        artifact = HmsScenarioWorkspace(
            scenario_id=scenario_id,
            source_project=source_folder,
            project_folder=workspace_path,
            project_file=project_file,
            run_name=run_name,
            met_name=met_name,
            control_name=control_name,
            grid_name=grid_name,
            precipitation_source=forcing_source,
            precipitation_file=forcing_path,
            precipitation_pathname=precipitation_pathname,
            output_dss=output_dir / f"{slug}_hms.dss",
            log_file=workspace_path / f"{slug}_hms.log",
            clone_policy=(
                "full-project-copy"
                if include_generated_outputs
                else "input-only"
            ),
        )
        HmsScenario.validate_workspace(artifact)
        logger.info("Prepared HMS scenario workspace: %s", workspace_path)
        return artifact

    @staticmethod
    @log_call
    def validate_workspace(workspace: HmsScenarioWorkspace) -> Dict[str, bool]:
        """Validate references written by :meth:`prepare_workspace`."""
        project = HmsPrj().initialize(workspace.project_folder)
        config = project.get_run_configuration(workspace.run_name)
        checks = {
            "project_file_exists": workspace.project_file.is_file(),
            "precipitation_file_exists": workspace.precipitation_file.is_file(),
            "run_registered": bool(config),
            "met_matches": config.get("met_name") == workspace.met_name,
            "control_matches": config.get("control_name") == workspace.control_name,
            "output_matches": HmsScenario._normalize_hms_path(
                str(config.get("dss_file", ""))
            ) == HmsScenario._normalize_hms_path(
                str(workspace.output_dss.relative_to(workspace.project_folder))
            ),
        }

        grid_file = HmsScenario._resolve_grid_file(workspace.project_folder)
        grid_content = grid_file.read_text(encoding="utf-8")
        grid_match = HmsGrid._find_grid_block(grid_content, workspace.grid_name)
        checks["grid_registered"] = grid_match is not None
        if grid_match is not None:
            checks["grid_pathname_matches"] = (
                workspace.precipitation_pathname in grid_match.group(0)
            )
        else:
            checks["grid_pathname_matches"] = False

        if not all(checks.values()):
            failed = [name for name, passed in checks.items() if not passed]
            raise ValueError(
                "Prepared HMS workspace failed validation: " + ", ".join(failed)
            )
        return checks

    @staticmethod
    def _generated_output_ignore(source_folder: Path):
        """Return a copy filter for root-level HMS computation artifacts."""
        source_root = source_folder.resolve()
        project_dss_names: set[str] = set()
        project_file = HmsPrj.find_hms_project(source_root)
        if project_file is not None:
            project_content = project_file.read_text(
                encoding="utf-8",
                errors="ignore",
            )
            for match in re.finditer(
                r"^\s*DSS File Name:\s*(.+?)\s*$",
                project_content,
                flags=re.IGNORECASE | re.MULTILINE,
            ):
                referenced = Path(match.group(1).replace("\\", "/"))
                if len(referenced.parts) == 1:
                    project_dss_names.add(referenced.name.lower())
        generated_suffixes = (
            ".dss",
            ".dsc",
            ".dsc.h5",
            ".log",
            ".out",
        )

        def ignore(directory: str, names: list[str]) -> set[str]:
            current = Path(directory).resolve()
            if current != source_root:
                return set()
            ignored = set()
            for name in names:
                lowered = name.lower()
                if lowered == "results":
                    ignored.add(name)
                elif lowered in project_dss_names:
                    continue
                elif lowered.endswith(generated_suffixes):
                    ignored.add(name)
                elif lowered.endswith(".dss.cyberducksegment"):
                    ignored.add(name)
            return ignored

        return ignore

    @staticmethod
    @log_call
    def execute(
        workspace: HmsScenarioWorkspace,
        *,
        hms_exe_path: Optional[Union[str, Path]] = None,
        timeout: int = 3600,
        max_memory: Optional[str] = None,
    ) -> HmsRunArtifact:
        """Execute a prepared scenario and return a machine-readable artifact."""
        project = HmsPrj().initialize(
            workspace.project_folder,
            hms_exe_path=hms_exe_path,
        )
        started = datetime.now(timezone.utc)
        success = HmsCmdr.compute_run(
            workspace.run_name,
            hms_object=project,
            timeout=timeout,
            max_memory=max_memory,
            raise_on_timeout=True,
        )
        finished = datetime.now(timezone.utc)
        dss_exists = workspace.output_dss.is_file()
        dss_size = workspace.output_dss.stat().st_size if dss_exists else 0
        log_text = ""
        if workspace.log_file.is_file():
            log_text = workspace.log_file.read_text(
                encoding="utf-8",
                errors="replace",
            )
        completion_marker = re.search(
            rf'^NOTE 15302:\s+Finished computing simulation run '
            rf'"{re.escape(workspace.run_name)}"',
            log_text,
            flags=re.MULTILINE,
        )
        abort_marker = re.search(
            rf'^WARNING 15303:\s+Aborted run '
            rf'"{re.escape(workspace.run_name)}"',
            log_text,
            flags=re.MULTILINE,
        )
        error_count = sum(
            1
            for line in log_text.splitlines()
            if line.lstrip().startswith("ERROR")
        )
        completed = completion_marker is not None
        aborted = abort_marker is not None
        status = (
            "succeeded"
            if success
            and dss_size > 0
            and completed
            and not aborted
            and error_count == 0
            else "failed"
        )
        return HmsRunArtifact(
            scenario_id=workspace.scenario_id,
            status=status,
            run_name=workspace.run_name,
            project_folder=workspace.project_folder,
            dss_file=workspace.output_dss,
            log_file=workspace.log_file,
            started_at=started.isoformat().replace("+00:00", "Z"),
            finished_at=finished.isoformat().replace("+00:00", "Z"),
            dss_exists=dss_exists,
            dss_size_bytes=dss_size,
            process_succeeded=success,
            completion_marker_found=completed,
            abort_marker_found=aborted,
            error_count=error_count,
        )

    @staticmethod
    def _resolve_project_folder(source_project: Union[str, Path]) -> Path:
        source = Path(source_project).resolve()
        folder = source.parent if source.is_file() else source
        if not folder.is_dir():
            raise FileNotFoundError(f"HMS project folder not found: {folder}")
        if HmsPrj.find_hms_project(folder) is None:
            raise FileNotFoundError(f"No .hms project file found in: {folder}")
        return folder

    @staticmethod
    def _resolve_grid_file(project_folder: Union[str, Path]) -> Path:
        folder = Path(project_folder)
        project_file = HmsPrj.find_hms_project(folder)
        if project_file is not None:
            matching = project_file.with_suffix(".grid")
            if matching.is_file():
                return matching
        candidates = sorted(folder.glob("*.grid"))
        if len(candidates) != 1:
            raise ValueError(
                f"Expected one project .grid file in {folder}, found {len(candidates)}"
            )
        return candidates[0]

    @staticmethod
    def _scenario_slug(scenario_id: str) -> str:
        slug = re.sub(r"[^A-Za-z0-9_-]+", "_", str(scenario_id)).strip("_")
        if not slug:
            raise ValueError("scenario_id must contain at least one letter or number")
        return slug[:48]

    @staticmethod
    def _windows_relative_path(path: Path) -> str:
        return str(path).replace("/", "\\")

    @staticmethod
    def _normalize_hms_path(path: str) -> str:
        return path.replace("\\", "/").lower()

    @staticmethod
    def _validate_copy_boundaries(source: Path, destination: Path) -> None:
        source = source.resolve()
        destination = destination.resolve()
        if source == destination or source in destination.parents or destination in source.parents:
            raise ValueError(
                "Source project and scenario workspace must not overlap"
            )
