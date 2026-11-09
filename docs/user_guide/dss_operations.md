# DSS Operations

Read and write DSS files for time-series data and simulation results.

## Overview

The `HmsDss` class provides methods for working with HEC-DSS (Data Storage System) files, which HMS uses for time-series input data and simulation results.

!!! note "Requires RAS Commander"
    DSS operations require the `ras-commander` package, which provides the `RasDss` interface.
    ```bash
    pip install hms-commander[dss]
    ```

## Verified native runtime

DSS initialization requires `HMS_DSS_TRUST_MANIFEST`, pointing to an independently
reviewed JSON manifest outside the writable `~/.hms-commander/dss/` cache.
The runtime administrator must protect that manifest from worker writes and
protect executable cache directories from other users. Hash checks do not
replace filesystem permissions or prevent a privileged concurrent mutation.

Manifest format version 1 contains:

- `format_version`: `1`;
- `platform`: `Windows`, `Linux`, or `Darwin` for the selected native library;
- `artifacts`: every exact cache-relative `jar/<filename>` and `lib/<filename>`
  accepted at runtime, each with lowercase `sha256` and positive `size_bytes`;
- `downloads`: the same selected versioned JARs plus the platform ZIP archive,
  each with its independently approved digest and byte count.

Use the exact artifact/version inventory in `HecMonolithDownloader.COMMON_JARS`
and `NATIVE_LIBS`. Do not generate approval from whatever happens to be in the
cache, or accept a checksum delivered with untrusted bytes. This change does not
provide approved production digests; provisioning and legitimate native DSS
initialization remain deployment qualification requirements.

Workers default to a sealed cache: missing files stop initialization without
network access. A separately controlled provisioning step may set
`HMS_DSS_ALLOW_DOWNLOADS=1`. Downloads use the package-owned HTTPS sources, refuse
redirects, enforce the approved byte count and a 300-second total transfer
deadline checked between reads, with 5-second connect and 1-second read timeouts.
The transfer loop uses `urllib3.HTTPResponse.read1`; the selected environment
must provide that interface. Network-stack timeout resolution may delay return
until the current bounded read finishes.

New bytes are staged, flushed, hashed and promoted without replacing an existing
file. Native ZIP extraction accepts only approved members and sizes and rejects
links, traversal and case collisions. Corrupt existing files and unexpected
executable artifacts stop initialization; an operator must investigate them.
Retry does not overwrite conflicting cache evidence. Every classpath/library
selection validates the complete runtime, and only selected JARs enter the
classpath. The native archive may remain in the cache for verified retry.

## Quick examples

### Check DSS Availability

```python
from hms_commander import HmsDss

# Verify DSS support is available
if HmsDss.is_available():
    print("DSS operations available")
else:
    print("Install: pip install hms-commander[dss]")
```

### Read Time-Series Data

```python
# Read precipitation time series
data = HmsDss.read_timeseries(
    dss_file="input.dss",
    pathname="/BASIN/GAGE1/PRECIP/01JAN2020/15MIN/OBS/"
)
print(data)  # Returns pandas DataFrame
```

### Get Catalog

```python
# List all paths in DSS file
catalog = HmsDss.get_catalog("results.dss")
for path in catalog:
    print(path)
```

### Extract HMS Results

```python
# Extract all flow results
flows = HmsDss.extract_hms_results(
    dss_file="results.dss",
    result_type="flow"
)

# List available results
flow_paths = HmsDss.list_flow_results("results.dss")
precip_paths = HmsDss.list_precipitation_data("results.dss")
```

### Write Time-Series

```python
import pandas as pd

# Create time series
data = pd.DataFrame({
    'datetime': pd.date_range('2020-01-01', periods=24, freq='H'),
    'value': [1.0, 1.2, 0.8, ...]
})

# Write to DSS
HmsDss.write_timeseries(
    dss_file="output.dss",
    pathname="/BASIN/LOCATION/FLOW/01JAN2020/1HOUR/SIMULATED/",
    data=data,
    units="CFS"
)
```

## DSS Pathname Structure

DSS uses a 6-part pathname:
```
/A-Part/B-Part/C-Part/D-Part/E-Part/F-Part/
```

**Example:** `/BASIN/SUBBASIN1/PRECIP/01JAN2020/15MIN/OBS/`

- **A-Part:** Basin/watershed
- **B-Part:** Location (subbasin, junction, reach)
- **C-Part:** Parameter (PRECIP, FLOW, STAGE)
- **D-Part:** Start date
- **E-Part:** Time interval
- **F-Part:** Version (OBS, SIMULATED)

## Pathname Utilities

```python
# Parse pathname into components
parts = HmsDss.parse_dss_pathname(
    "/BASIN/SUB1/FLOW/01JAN2020/15MIN/SIM/"
)
print(parts)

# Create pathname from components
path = HmsDss.create_dss_pathname(
    basin="MYBASIN",
    element="OUTLET",
    param_type="FLOW",
    interval="1HOUR"
)
```

## Key Operations

- **Read/Write** - `read_timeseries()`, `write_timeseries()`
- **Catalog** - `get_catalog()`, `list_flow_results()`, `list_precipitation_data()`
- **Extract** - `extract_hms_results()`
- **Pathname** - `parse_dss_pathname()`, `create_dss_pathname()`

## File Handles and Locks

Each `HmsDss`, `DssCore` and `HmsDssGrid` call closes the DSS file before it
returns, including when it raises. HEC-HMS or another program can write, rename
or delete the file as soon as the call returns, while the Python process and its
Java VM keep running.

HEC Monolith's `HecDss.done()` does not close the file by itself; it lowers a
shared reference count, and a background timer closes the file a few seconds
later. hms-commander therefore calls `HecDSSFileAccess.close(path, True)` with
the full absolute path it used to open the file. Code that calls the HEC
Monolith Java API directly through pyjnius needs the same call:

```python
from jnius import autoclass

HecDss = autoclass("hec.heclib.dss.HecDss")
HecDSSFileAccess = autoclass("hec.heclib.dss.HecDSSFileAccess")

path = r"C:\Projects\Basin\results.dss"  # full long path, not an 8.3 short name
dss = HecDss.open(path)
try:
    catalog = dss.getCatalogedPathnames()
finally:
    dss.done()
    HecDSSFileAccess.close(path, True)  # same string that was passed to open()
```

Other `HecDss` objects on the same file reopen it on their next access.

## Related Topics

- [API Reference: HmsDss](../api/hms_dss.md) - Complete method documentation
- [Results Analysis](results_analysis.md) - Processing HMS results
- [Time-Series Gages](gages.md) - Linking DSS data to models
- [Data Formats: DSS Integration](../data_formats/dss_integration.md) - DSS file format

---

*For complete API documentation, see [HmsDss API Reference](../api/hms_dss.md)*
