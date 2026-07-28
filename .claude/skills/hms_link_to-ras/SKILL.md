---
name: hms_link_to-ras
shared_corpus: true
harness_scope: shared
source_owner: gpt-cmdr
security_review: internal
description: |
  Links HEC-HMS watershed models to HEC-RAS river models by extracting HMS DSS
  results and preparing an auditable hydrologic handoff. Covers exact flow
  pathname inventory, time and value qualification, outlet-to-boundary
  crosswalks, and isolated ras-commander scenario preparation. Use for HMS to
  RAS workflows, boundary conditions, upstream hydrographs, watershed-to-river
  integration, or RAS import qualification.
  Trigger keywords: HMS to RAS, link HMS RAS, boundary condition, upstream BC,
  watershed to river, integrated model, export HMS, spatial matching,
  hydrograph import.
---

# Linking HMS to HEC-RAS

## Scope

This skill owns the HMS side of the handoff. It proves that the hydrologic
outputs are complete and describes their intended hydraulic targets.
ras-commander owns RAS workspace preparation, boundary editing, execution, and
hydraulic result review.

## Handoff Workflow

### 1. Prove HMS Execution

Use `hms_execute_runs` when the run has not already been qualified. Require the
exact HMS completion marker, reject abort/error markers, and verify that the
output DSS is non-empty and catalog-readable.

```python
from hms_commander import init_hms_project, hms

init_hms_project("watershed")
dss_file = hms.run_df.loc["Design_Storm", "dss_file"]
```

Execution completion is not yet a qualified RAS handoff.

### 2. Inventory Exact Flow Pathnames

Enumerate the complete required-path set. Do not sample one outlet or infer DSS
paths from element names.

```python
from hms_commander import HmsDss, HmsResults

catalog = HmsDss.get_catalog(dss_file)
flows = HmsResults.get_outflow_timeseries(dss_file, "Watershed_Outlet")
```

For every required pathname, preserve:

- exact six-part DSS pathname;
- HMS element identity;
- first and last timestamp;
- interval, units, and DSS data type;
- missing, NaN, and unexpected negative-value counts;
- peak flow and peak time; and
- the approved recession or other extension used to cover the RAS window.

### 3. Crosswalk to Active RAS Geometry

For every flow pathname, specify exactly one intended RAS selector:

- 1D: river, reach, and station; or
- storage/2D: area name and, when applicable, BC line.

Document the outlet coordinates and CRS when spatial review is needed:

```python
from hms_commander import HmsGeo

HmsGeo.export_all_geojson("project.basin", "geojson_output", "project.geo")
```

A matching `.u##` boundary block is not proof of a valid mapping. The selector
must exist in the geometry used by the active RAS plan. Record inherited,
inactive, or missing targets as unresolved and keep them out of the successful
mapping count.

### 4. Prepare the Handoff Record

The record should contain:

- source and derivative DSS paths plus provenance or checksum;
- required pathname inventory and validation results;
- HMS and RAS simulation windows;
- extension/recession policy;
- exact RAS selectors and geometry-match status;
- approved non-HMS source-gage inputs, if any; and
- unresolved mappings and their disposition.

### 5. Continue in ras-commander

Use the owning RAS APIs and skills:

- `RasScenario` for isolated project, plan, and unsteady-flow clones;
- `RasUnsteady` for exact DSS links;
- `hecras_compute_plans` for execution;
- `hecras_parse_compute-messages` for completion and warning evidence; and
- `hecras_extract_results` for hydraulic results.

The active plan, plan simulation window, DSS time coverage, boundary-to-geometry
crosswalk, and RAS result HDF window must agree before the pipeline is called
fully qualified.

## Qualification States

Report these states independently:

1. HMS execution complete.
2. Hydrologic handoff qualified.
3. RAS execution complete.
4. Hydraulic QA/QC accepted.

An execution can pass while a later state remains conditional.

## Common Failures

- **RAS cannot find the DSS**: Prefer a copied project-relative reference that
  begins with `.\`; otherwise use a verified absolute path.
- **No data read**: Compare the exact pathname, units, interval, and RAS window
  against the DSS catalog and series timestamps.
- **Ignored lateral inflow**: Confirm the referenced storage area, 2D area, BC
  line, or cross section exists in the active geometry.
- **Partial time coverage**: Apply only an approved extension policy and retain
  the unmodified HMS result as provenance.
- **Peak mismatch**: Verify the same pathname and units before investigating
  interpolation or hydraulic behavior.

## Primary Sources

- `hms_commander/HmsScenario.py` - scenario construction and execution evidence
- `hms_commander/HmsResults.py` - flow extraction and statistics
- `hms_commander/HmsDss.py` - DSS catalog and time-series operations
- `hms_commander/HmsGeo.py` - spatial reference exports
- `ras-commander/ras_commander/RasScenario.py` - RAS-side preparation contract

## Related Skills

- `hms_execute_runs`
- `hms_extract_dss-results`
- `hms_parse_basin-models`
