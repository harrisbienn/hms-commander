---
name: hms_extract_dss-results
shared_corpus: true
harness_scope: shared
source_owner: gpt-cmdr
security_review: internal
description: |
  Extracts and analyzes HEC-HMS simulation results from DSS files using HmsDss,
  HmsResults, and HmsResultsProducts. Handles peak flows, hydrographs, volume
  summaries, time series, and deterministic hydrologic product manifests.
  Leverages ras-commander's RasDss for DSS V6/V7 support. Use when processing
  HMS results, extracting peak flows, analyzing hydrographs, computing volumes,
  exporting time series, comparing multiple runs, or linking HMS results to
  HEC-RAS boundary conditions.
  Trigger keywords: DSS file, results, peak flow, hydrograph, time series, volume,
  extract results, HMS output, analyze results, compare runs.
---

# Extracting DSS Results

## When This Skill Is Activated

You are the HMS results extraction specialist. Follow this post-simulation workflow.

## Decision Tree

1. **User wants peak flows** → "Extract Peak Flows"
2. **User wants hydrograph time series** → "Extract Hydrographs"
3. **User wants to compare multiple runs** → "Compare Runs"
4. **User wants a deterministic scenario product** →
   `HmsResultsProducts.export()`
5. **User wants to hand off results to RAS** → Delegate to
   `hms_link_to-ras` skill
6. **DSS operations beyond results** → Delegate to
   `dss-integration-specialist` agent

## Step 1: Locate the DSS File

If the project is initialized:
```python
dss_file = hms.run_df.loc["Run 1", "dss_file"]
```

If not, ask the user for the DSS file path. Verify it exists:
```python
from pathlib import Path
assert Path(dss_file).exists(), f"DSS file not found: {dss_file}"
```

## Extract Peak Flows

```python
from hms_commander import HmsResults

peaks = HmsResults.get_peak_flows(dss_file)
# Columns: Element, Peak Flow (cfs), Time to Peak
```

Display the DataFrame to the user. Validate:
- All peak flows > 0 (negative flows indicate a problem)
- Time to peak is within the simulation window

## Extract Hydrographs

```python
flows = HmsResults.get_outflow_timeseries(dss_file, "Outlet")
# Returns: pandas DataFrame with datetime index
```

For precipitation:
```python
precip = HmsResults.get_precipitation_timeseries(dss_file, "Subbasin1")
```

Validate:
- No NaN values: `assert flows.notna().all().all()`
- No negative flows: `assert flows["Flow"].min() >= 0`

For an HMS-to-RAS handoff, validate the complete required-path inventory rather
than sampling one outlet. For every pathname, record:

- first and last timestamp;
- interval, units, and DSS data type;
- missing/NaN/negative-value counts;
- peak value and peak time; and
- whether the series covers the full RAS window, including the approved
  recession or other extension.

Preserve the exact pathname and element name in the evidence. Do not use peak
flow alone as proof of temporal coverage.

For a reusable product instead of an exploratory read, use the package-owned
contract:

```python
from hms_commander import HmsResultsProducts

manifest = HmsResultsProducts.export(
    dss_file,
    required_pathnames=[
        {
            "mapping_id": "boundary-001",
            "pathname": "//OUTLET/FLOW//5Minute/RUN:SCENARIO-001/",
            "ras_boundary": "Upstream BC",
        }
    ],
    output_directory="products/scenario-001/hydrology",
)
```

The output contains a deterministic portable hydrograph table, exact
pathname/time/value/recession evidence, precipitation-excess inventory, and a
checksum-pinned manifest. Preserve multiple mapping rows when one pathname
intentionally supplies more than one RAS target. Do not treat the manifest's
mechanical checks as the study's hydrologic-handoff acceptance decision.

## Volume Analysis

```python
volumes = HmsResults.get_volume_summary(dss_file)
```

## Compare Runs

```python
comparison = HmsResults.compare_runs(
    ["baseline.dss", "alternative.dss"],
    element="Outlet"
)
```

Display side-by-side peak flows and timing differences.

## DSS Catalog (Advanced)

If the user needs to see all available pathnames:
```python
if HmsDss.is_available():
    catalog = HmsDss.get_catalog(dss_file)
    print(catalog)
```

Note: HmsDss wraps ras-commander's RasDss — requires `ras-commander` and `pyjnius` installed.

## If Something Goes Wrong

- **DSS file not found**: Run hasn't been executed yet — delegate to `hms_execute_runs` skill
- **Empty results**: Check that the run completed successfully (check log file)
- **HmsDss not available**: RasDss/pyjnius not installed — use HmsResults (text-based) instead
- **Element not found**: Check element name against `hms.basin_df` or `get_subbasins()`

## Primary Sources

- `hms_commander/HmsDss.py` — DSS operations (wraps RasDss)
- `hms_commander/HmsResults.py` — Results extraction and analysis
- `hms_commander/HmsResultsProducts.py` — deterministic product and
  qualification contract
- `.claude/rules/hec-hms/dss-operations.md` — DSS patterns and pathname format

## Implementing Agent

For advanced DSS operations, delegate to:
`.claude/agents/dss-integration-specialist.md`

## Delegation Points

- **Need to run simulation first** → `hms_execute_runs` skill
- **Hand off results to RAS** → `hms_link_to-ras` skill
- **Modify model and re-run** → `hms_parse_basin-models` skill
