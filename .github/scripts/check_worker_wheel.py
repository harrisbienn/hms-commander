"""Check installed wheel resources and CLI startup without a native runtime."""

import importlib.metadata
import importlib.resources
import json
from pathlib import Path
import subprocess
import sysconfig

import hms_commander


def main() -> None:
    distribution = importlib.metadata.distribution("hms-commander")
    installed_root = Path(distribution.locate_file("")).resolve()
    source_root = Path(__file__).resolve().parents[2] / "hms_commander"
    imported = Path(hms_commander.__file__).resolve()
    assert imported.is_relative_to(installed_root), imported
    assert not imported.is_relative_to(source_root), (
        "Source import masks wheel contents"
    )

    contracts = importlib.resources.files("hms_commander").joinpath("contracts")
    required = {
        "scenario-worker-request-v1.3.schema.json",
        "scenario-worker-result-v1.0.schema.json",
        "timeseries-window-request-v1.0.schema.json",
        "timeseries-window-result-v1.0.schema.json",
        "hydrologic-handoff-request-v1.0.schema.json",
        "hydrologic-handoff-result-v1.0.schema.json",
    }
    schemas = {
        entry.name: entry
        for entry in contracts.iterdir()
        if entry.name.endswith(".schema.json")
    }
    assert required <= schemas.keys(), required - schemas.keys()
    for resource in schemas.values():
        assert isinstance(json.loads(resource.read_text(encoding="utf-8")), dict)

    scripts = Path(sysconfig.get_path("scripts"))
    entry_points = {
        entry.name: entry
        for entry in distribution.entry_points
        if entry.group == "console_scripts"
    }
    suffix = ".exe" if sysconfig.get_platform().startswith("win") else ""
    for name in ("hms-scenario-worker", "hms-timeseries-worker", "hms-handoff-worker"):
        assert callable(entry_points[name].load())
        executable = scripts / (name + suffix)
        completed = subprocess.run(
            [str(executable), "--help"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert "--request" in completed.stdout and "--result" in completed.stdout
    print(
        f"hms-commander {distribution.version}: "
        f"{len(schemas)} schemas and 3 worker entry points verified"
    )


if __name__ == "__main__":
    main()
