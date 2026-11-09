# Project Management

Working with HEC-HMS projects using HMS Commander's DataFrame-based approach.

## Overview

HMS Commander provides the `HmsPrj` class for managing HEC-HMS projects. The project object maintains DataFrames for all project components (basins, meteorologic models, controls, runs, gages) and provides methods for project-wide operations.

## Quick Start

```python
from hms_commander import init_hms_project, hms

# Initialize project
init_hms_project(r"C:\path\to\project")

# Access project DataFrames
print(hms.basin_df)      # Basin models
print(hms.met_df)        # Meteorologic models
print(hms.control_df)    # Control specifications
print(hms.run_df)        # Simulation runs
print(hms.gage_df)       # Time-series gages
```

## External DSS input preflight

After initializing an explicit project object with `load_dss_metadata=False`,
call `project.preflight_external_inputs(read_records=False)` to inventory all
declared `.gage` and `.grid` external DSS inputs without Java. The report includes
the source block, exact file/selector, resolved file and issues. This mode checks
file presence but leaves records `unverified` and `passed=False`.

For a disposable input copy in a qualified native runtime,
`project.preflight_external_inputs()` catalogs each file and reads every matched
record. Grid decompression uses RAS Commander's existing `RasDss.read_grid()`;
time-series reads use `HmsDss`. Blank D/E selector parts expand over cataloged
dates/intervals; all exact matches are listed in the report. Missing records,
failed reads, conflicting aliases or repeated keys, malformed/unterminated blocks
and ambiguous multiple variants fail closed. Reports retain raw reference
declarations so conflicting source values remain inspectable.
HMS 4.9 `DSS File Name`/`DSS Pathname` and flat `Filename`/`Pathname` declarations
share the same alias resolution in gage and project APIs.

The preflight does not edit project files or execute HMS. It covers all declared
gage/grid blocks rather than inferring which model is active. Native DSS readers
may retain file locks; use a separate process when the caller needs immediate
release. Input readability, project-open success, run completion and hydrologic
acceptance remain distinct checks.

## Key features

- **DataFrame-based interface** - All project data in pandas DataFrames
- **Multi-project support** - Manage multiple projects simultaneously
- **Automatic discovery** - Scans project folder for all HMS files
- **Path management** - Maintains absolute paths to all project files

## Related Topics

- [Project Initialization](../getting_started/project_initialization.md) - How to initialize projects
- [API Reference: HmsPrj](../api/hms_prj.md) - Complete API documentation
- [Quick Start Guide](../getting_started/quick_start.md) - Basic workflow

The API reference is generated from current package docstrings. Agent-facing files such as `AGENTS.md` and `CLAUDE.md` describe coding-agent behavior and should not be used as the user API reference.
