# DSS Operations

Read and write DSS files for time-series data and simulation results.

## Overview

The `HmsDss` class provides methods for working with HEC-DSS (Data Storage System) files, which HMS uses for time-series input data and simulation results.

!!! note "Install DSS dependencies"
    The DSS extra installs the Java bridge and the RAS companion used by the
    isolated grid worker. It does not provision or approve native executable files.
    ```bash
    pip install hms-commander[dss]
    ```

## Runtime ownership

The retained HMS and RAS DSS paths have separate configuration and trust boundaries:

| Route | Native owner | Selected policy |
| --- | --- | --- |
| `HmsDss`, `HmsDssGrid`, time-series and hydrologic result workers | Local `hms_commander.dss.DssCore` | `HMS_DSS_TRUST_MANIFEST`, `HMS_DSS_ALLOW_DOWNLOADS` |
| `HmsGridWriterWorker`, launched by `HmsSpatialTransfer` or `HmsSubbasinTransfer` | `ras_commander.RasDss` | `RAS_DSS_CACHE_DIR`, `RAS_DSS_TRUST_MANIFEST`, `RAS_DSS_ALLOW_DOWNLOADS` |
| `HmsSpatialTransfer.read_fingerprint_cube` | `ras_commander.RasDss`, in the caller process | The same RAS policy settings |
| External-input grid preflight | `ras_commander.RasDss`, in one isolated child for the grid batch | The same RAS policy settings |

The grid-writer route writes and verifies transfer grids in an isolated child process.
Both grid launchers use the owned-process boundary, a 600-second timeout, bounded
diagnostics and an explicit environment. They refuse a timed-out or unreconciled
result. The fingerprint reader instead runs in the caller process; it does not
acquire those child-process controls merely by using the same native owner.
Both the reader and writer require the companion capability
`ras-commander/dss-runtime-trust/1.0.0` before native I/O through HMS's shared
`_dss_runtime` admission helper. An older companion that ignores these settings
is refused. Request/result schemas remain version 1.0.

`HMS_DSS_*` settings do not configure or authorize the RAS runtime. The environment
boundary forwards only the listed settings, without adding a download opt-in or
copying a missing RAS value from HMS. Relative trust-manifest and RAS cache paths
are resolved against the parent process's current working directory before the
isolated worker changes directory. This preserves the selected runtime identity
across the launch. Ambient `CLASSPATH`, `JAVA_TOOL_OPTIONS`,
`PYTHONPATH` and arbitrary `RAS_DSS_*` names are excluded. The outer application
must also admit the selected settings; an HMS child cannot recover a value that
the outer launcher stripped.

Use a fresh process when changing native runtimes. In particular, RAS's trust
policy refuses an already-running JVM it did not configure. Matching JAR names
or package versions do not establish compatible loaded origins or shared approval.

External-input preflight keeps the two owners in separate processes. With
`HmsPrj.preflight_external_inputs(read_records=True)`, gage catalogs and records
use local `HmsDss` in the caller. Declared grid references form one batch in a
fresh isolated child; RAS owns both its catalog and grid reads. The child requires
the same RAS capability before native calls. An HMS JVM in the caller therefore
cannot supply the grid child's runtime. Inventory-only mode starts no child and
leaves inputs unverified.

The private grid-preflight protocol binds results to the exact request and
reference indexes. It uses the existing owned-process boundary, explicit DSS
environment, bounded diagnostics and a 600-second batch timeout. The parent
rejects missing, mismatched or incomplete results, nonzero exits, timeouts and
unreconciled ownership. Each affected grid remains failed; gage outcomes and
individual grid-read errors stay in the public report.

Requests are limited to 1 MiB and results to 8 MiB. Each attempt retains
`request.json`, `result.json` when available, and process diagnostics under a
new `hms-grid-preflight-*` directory in the platform temporary directory. Failure
messages identify that directory; successful attempts are logged at DEBUG.
There is no automatic deletion: inspect process ownership before deliberately
retiring retained attempts. Portable fixtures prove routing and refusal, while
actual native compatibility/readback remains under
[HMS issue 28](https://github.com/harrisbienn/hms-commander/issues/28) and
[FloodForecast integration F11](https://github.com/harrisbienn/floodforecast/issues/326).

## Local HMS native runtime

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

## RAS grid runtime

Select a companion implementing the required trust policy before using the
fingerprint reader or isolated grid worker. Configure `RAS_DSS_TRUST_MANIFEST`
as an independently reviewed manifest outside its writable cache.
`RAS_DSS_CACHE_DIR` optionally selects that cache; otherwise the RAS default is
`~/.ras-commander/dss/`.
Provision it separately using the RAS-owned verified acquisition API and leave
`RAS_DSS_ALLOW_DOWNLOADS` unset or `0` during sealed operation. Only an explicit
`1` enables its acquisition policy; it does not supply missing approval evidence.

The RAS format-version-1 manifest records the platform and approved SHA-256/size
identities in `artifacts` and `downloads`. Its selected filenames and versions
come from the RAS owner's `HecMonolithDownloader.COMMON_JARS` and `NATIVE_LIBS`.
Do not reuse an HMS manifest by assumption or derive approval from cache contents.
RAS validates the cache and exact classpath before loading native libraries.
This guide supplies no approved production manifest or binary digests.

HMS's shared admission helper consumes the policy identity from the companion's
private provisioning helper as an explicit integration boundary. A changed
identity requires a reviewed HMS adapter update, not silent acceptance. The
synthetic worker tests substitute this boundary and native I/O; they do not
qualify the companion currently pinned in CI.
Merge the owning RAS policy change before updating accepted companion pins and
performing actual native qualification. The installed package and its selected
runtime must both be inspected; a source checkout alone is not installed evidence.

## Release and qualification evidence

Keep these identities separate when adopting either DSS route:

| Surface | Identity to retain | What establishes acceptance |
| --- | --- | --- |
| Python library | Distribution version, wheel hash and source revision; identify editable installs explicitly | Installed imports, package resources and applicable contract tests |
| Worker protocol | Exact request/result schema and owning package revision | Positive/negative contract fixtures plus the selected integration checks |
| Native runtime | Policy identity, protected manifest digest, platform and approved artifact digests | Sealed-cache verification and separately scoped real DSS write/readback with loaded-origin evidence |
| Agent/plugin bundle | Bundle version/hash and source revision | Bundle and framework checks; this does not qualify Python or native execution |
| Documentation | Source revision and build environment | Strict documentation build; this does not establish engine or model acceptance |

Portable fixtures establish refusal, environment propagation and process ownership.
They do not establish valid production artifacts, engineering results or a real
grid/time-series round trip. The remaining native qualification tracked in
[HMS issue 28](https://github.com/harrisbienn/hms-commander/issues/28) and the
[RAS provisioning issue](https://github.com/harrisbienn/ras-commander/issues/38)
remains open until its exact retained evidence is accepted.

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
