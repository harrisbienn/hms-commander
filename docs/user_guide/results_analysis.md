# Results Analysis

Extract peak flows, volumes, hydrograph statistics, and compare multiple runs.

## Overview

The `HmsResults` class provides methods for analyzing HEC-HMS simulation results stored in DSS files.

## Quick Examples

### Get Peak Flows

```python
from hms_commander import HmsResults

# Extract peak flow summary
peaks = HmsResults.get_peak_flows("results.dss")
print(peaks)
```

### Get Volume Summary

```python
# Get volumes in acre-feet
volumes = HmsResults.get_volume_summary("results.dss")
print(volumes)
```

### Get Hydrograph Time Series

```python
# Extract outflow hydrograph
hydrograph = HmsResults.get_outflow_timeseries(
    dss_file="results.dss",
    element="Outlet"
)
print(hydrograph)  # pandas DataFrame
```

### Compare Multiple Runs

```python
# Side-by-side comparison
comparison = HmsResults.compare_runs(
    dss_files=["baseline.dss", "calibrated.dss"],
    element="Outlet"
)
print(comparison)
```

### Hydrograph Statistics

```python
# Get detailed statistics
stats = HmsResults.get_hydrograph_statistics(
    dss_file="results.dss",
    element="Outlet"
)
print(f"Peak: {stats['peak_flow']} CFS")
print(f"Time to peak: {stats['time_to_peak']}")
print(f"Total volume: {stats['volume']} ac-ft")
```

## Precipitation Analysis

```python
# Get precipitation summary
precip = HmsResults.get_precipitation_summary("results.dss")
print(precip)

# Extract precipitation time series
precip_ts = HmsResults.get_precipitation_timeseries(
    dss_file="results.dss",
    element="Subbasin1"
)
```

## Export Results

```python
# Export all results to CSV files
HmsResults.export_results_to_csv(
    dss_file="results.dss",
    output_folder="results_csv"
)
# Creates: peaks.csv, volumes.csv, hydrographs/*.csv
```

## Export a Qualified HMS-to-RAS Product Package

Use `HmsResultsProducts` when another system needs a stable, auditable
hydrologic handoff instead of an exploratory collection of CSV files. Supply
the exact required DSS pathnames and a unique mapping identifier for every RAS
target. The same pathname may appear more than once when one HMS element
intentionally supplies multiple boundaries.

```python
from hms_commander import HmsResultsProducts

mappings = [
    {
        "mapping_id": "upstream-001",
        "pathname": "//OUTLET/FLOW//5Minute/RUN:SCENARIO-001/",
        "ras_boundary": "Upstream BC",
    }
]

manifest = HmsResultsProducts.export(
    "scenario-output.dss",
    mappings,
    "products/scenario-001/hydrology",
)
```

### Materialize the RAS handoff DSS

Use `materialize_handoff` when the RAS plan needs one DSS assembled from HMS
results and approved provider inputs. Each mapping pins its source file by
SHA-256, declares the exact source and output pathnames, and supplies the units,
data type, interval, and permitted identity or linear transformation. The
method validates the full model window, writes each distinct output pathname
once, and then applies the same mechanical qualification as `export`. DSS
materialization runs in an isolated child process so native handles are closed
before the parent authenticates the consolidated file.

```python
handoff = HmsResultsProducts.materialize_handoff(
    [
        {
            "mapping_id": "split-left",
            "source_asset_id": "hms-scenario-output",
            "source_dss": "run/scenario-output.dss",
            "source_sha256": "<64-character lowercase SHA-256>",
            "source_pathname": "//OUTLET/FLOW//5Minute/RUN:SCENARIO-001/",
            "output_pathname": "//UPSTREAM/FLOW//5Minute/HANDOFF:LEFT/",
            "source_units": "CFS",
            "target_units": "CFS",
            "value_type": "INST-VAL",
            "interval_minutes": 5,
            "conversion": "linear",
            "multiplier": 0.5,
            "offset": 0.0,
        }
    ],
    "products/scenario-001/hydrologic-handoff",
    model_start="2019-09-18T13:00:00",
    model_end="2019-09-19T13:00:00",
)
```

The destination directory must not already exist. Publication is atomic: an
invalid checksum, duplicate output with a different transformation, metadata
mismatch, incomplete time window, or failed qualification leaves no package at
the requested destination. Successful output contains:

- `hydrologic-handoff.dss`, the consolidated RAS input;
- `hydrologic-handoff-provenance.json`, a portable record of source hashes and
  transformations; and
- `products/hydrologic-products.json`, whose source hash authenticates the
  consolidated DSS for downstream consumption.

The returned `boundary_pathnames` index maps each stable mapping ID to its
materialized pathname. Multiple mapping IDs may intentionally share an output
only when their source and transformation definitions are identical. This
allows several RAS boundary selectors to consume one DSS record without writing
or transforming it more than once.

## Audit gridded excess transfer

Use `HmsSpatialTransfer` when gridded HMS incremental excess must be sampled
onto another model's regular grid. The caller supplies portable source and
target grid definitions plus the source precipitation fingerprint cube. The
API reads the HMS result HDF and basin SQLite, resolves result columns to
computation cells, and reports raw support, nearest-fill distance, area, and
volume-effect metrics.

`excess_depth_units` must match the incremental-excess dataset's HDF `units`
attribute; the audit rejects a mismatch before calculating volume.

Precipitation fingerprints need not be unique when every indistinguishable
HMS result column has the same excess series, because the transfer is then
permutation-invariant. The audit records those groups. It fails closed when
an ambiguous assignment would change transferred excess.

```python
from hms_commander import HmsSpatialTransfer

audit = HmsSpatialTransfer.audit_excess_to_grid(
    "run/results.h5",
    "run/basin.sqlite",
    source_fingerprint_cube,
    source_grid_definition,
    target_grid_definition,
    excess_depth_units="IN",
    fingerprint_stride=12,
    source_value_multiplier=1.0 / (12.0 * 25.4),
)
HmsSpatialTransfer.write_audit(audit, "products/spatial-transfer-audit.json")
```

For an executable handoff, use `export_excess_to_grid`. It authenticates and
reads the forcing-grid fingerprint family, transfers every HMS incremental-
excess frame, and writes one immutable target-grid DSS plus checksum-pinned
audit and manifest files. The DSS write runs in an isolated child process so
native file locks are released before the parent hashes the product.

```python
manifest = HmsSpatialTransfer.export_excess_to_grid(
    "run/results/RUN_Scenario.h5",
    "run/2021_Existing_Conditions.sqlite",
    "inputs/stormhub-forcing.dss",
    "/SHG/GRID/PRECIPITATION///AORC-TRANSPOSED/",
    source_grid_definition,
    target_grid_definition,
    "products/spatial-transfer/ras-gridded-excess.dss",
    "/SHG/BASIN/PRECIPITATION///EXCESS/",
    model_start=model_start,
    model_end=model_end,
    model_interval_minutes=5,
    source_interval_minutes=60,
    excess_depth_units="IN",
    source_value_multiplier=1.0 / 304.8,
)
```

`HmsScenarioWorker` can perform this export after a successful HMS run when
its versioned request includes `spatial_transfer`. That object pins the basin
SQLite, both grid-definition files, selector, intervals, conversion factor,
and qualification-only disposition. The worker result exposes the product
manifest, DSS, audit, metrics, selector, and record count for downstream
orchestration without reopening the HMS result HDF.

The audit deliberately does not apply engineering thresholds or declare the
transfer acceptable for forecasting. Those decisions belong to the consuming
study's versioned qualification policy.

The output directory must not already exist. It contains:

- `hydrologic-hydrographs.csv`, a deterministic portable table;
- `hydrologic-qualification.json`, with pathname, time, interval, units,
  missing/sentinel/negative counts, peak, and recession evidence; and
- `hydrologic-products.json`, a checksum-pinned product manifest suitable for
  downstream STAC asset assembly.

The operation also inventories precipitation-excess pathnames in the source
DSS. A successful export means the stated mechanical checks completed; it does
not make the hydrologic handoff acceptable by itself. The owning study must
evaluate and record that gate separately.

## Multi-Run Comparison Workflow

```python
# Compare baseline vs. calibrated
runs = {
    "Baseline": "baseline.dss",
    "Calibrated": "calibrated.dss",
    "Atlas14": "atlas14.dss"
}

for name, dss_file in runs.items():
    peaks = HmsResults.get_peak_flows(dss_file)
    print(f"\n{name}:")
    print(peaks)
```

## Typical Analysis Workflow

```python
# 1. Check simulation completed
peaks = HmsResults.get_peak_flows("results.dss")
if peaks.empty:
    print("No results found - check simulation")
else:
    # 2. Extract key metrics
    outlet_peak = peaks.loc[peaks['element'] == 'Outlet', 'peak_flow'].values[0]

    # 3. Get detailed hydrograph
    hydrograph = HmsResults.get_outflow_timeseries("results.dss", "Outlet")

    # 4. Plot (using matplotlib)
    import matplotlib.pyplot as plt
    hydrograph.plot(x='datetime', y='flow')
    plt.title(f'Outlet Hydrograph - Peak: {outlet_peak:.1f} CFS')
    plt.show()
```

## Key Operations

- **Peak flows** - `get_peak_flows()` - Summary table
- **Volumes** - `get_volume_summary()` - Total volumes
- **Time series** - `get_outflow_timeseries()`, `get_precipitation_timeseries()`
- **Statistics** - `get_hydrograph_statistics()` - Comprehensive metrics
- **Comparison** - `compare_runs()` - Multi-run analysis
- **Export** - `export_results_to_csv()` - CSV output
- **Scenario handoff** - `HmsResultsProducts.export()` - Deterministic,
  qualified HMS-to-RAS products
- **Consolidated RAS input** - `HmsResultsProducts.materialize_handoff()` -
  Authenticated multi-source DSS materialization

## Related Topics

- [API Reference: HmsResults](../api/hms_results.md) - Complete method documentation
- [API Reference: HmsResultsProducts](../api/hms_results_products.md) -
  Scenario product contract
- [DSS Operations](dss_operations.md) - Working with DSS files
- [Clone Workflows](clone_workflows.md) - QAQC comparison patterns
- [Execution](execution.md) - Running simulations

---

*For complete API documentation, see [HmsResults API Reference](../api/hms_results.md)*
