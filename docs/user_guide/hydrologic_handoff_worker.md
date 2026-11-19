# Hydrologic Handoff Worker

`HmsHandoffWorker` is the process boundary for constructing the single DSS that
a RAS scenario consumes. It combines checksum-pinned HMS results and approved
provider DSS records, materializes declared linear transformations, qualifies
the result, and publishes authenticated evidence.

The request is an invocation-local projection from the owning scenario
specification. It may contain absolute machine paths and should not be committed
or treated as a second source of scenario truth.

## Safety contract

The worker:

- strictly validates all request fields and exact DSS metadata;
- authenticates each source DSS and detects changes during materialization;
- requires exact inclusive model-window coverage and regular intervals;
- rejects ambiguous output-path collisions and invalid transformations;
- writes and qualifies the DSS in separate isolated child processes so native
  handles close before authentication and atomic publication;
- publishes a consolidated DSS, portable provenance, and qualified product
  manifest atomically; and
- permits reuse only when the request, all sources, and all published assets
  still match their recorded identities.

## Request and execution

The request uses
`hms-commander/hydrologic-handoff-request/1.0`. Its `mappings` array has the
same fields documented for
[`HmsResultsProducts.materialize_handoff`](results_analysis.md#materialize-the-ras-handoff-dss).
It additionally supplies an operation ID, output directory, model window, and
mechanical qualification settings.

```json
{
  "schema": "hms-commander/hydrologic-handoff-request/1.0",
  "operation_id": "scenario-001-hydrologic-handoff",
  "model_window": {
    "start": "2019-09-18T13:00:00",
    "end": "2019-09-19T13:00:00"
  },
  "output_directory": "C:/runs/scenario-001/hydrologic-handoff",
  "qualification": {
    "sentinel_threshold": -1e30,
    "maximum_final_to_peak_ratio": 1.0,
    "minimum_post_peak_hours": 0.0
  },
  "mappings": [
    {
      "mapping_id": "upstream-001",
      "source_asset_id": "hms-scenario-output",
      "source_dss": "C:/runs/scenario-001/hms/results.dss",
      "source_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
      "source_pathname": "//OUTLET/FLOW//5Minute/RUN:SCENARIO-001/",
      "output_pathname": "//UPSTREAM/FLOW//5Minute/HANDOFF:SCENARIO-001/",
      "source_units": "CFS",
      "target_units": "CFS",
      "value_type": "INST-VAL",
      "interval_minutes": 5,
      "conversion": "identity",
      "multiplier": 1.0,
      "offset": 0.0
    }
  ]
}
```

```powershell
hms-handoff-worker --request request.json --result result.json
```

The command returns `0` for a new success or verified identical reuse, `2` for
an invalid request, `3` for an existing-artifact or identity conflict, and `4`
for materialization or qualification failure. A successful result uses
`hms-commander/hydrologic-handoff-result/1.0` and records the request hash,
three output identities, boundary-pathname index, and mechanical qualification
status.

Mechanical success does not grant engineering approval. FloodForecast remains
responsible for applying the versioned study policy and recording assessment,
promotion, and model-approval decisions.

## Portable flow boundary materialization

`HmsFlowBoundaryWorker` converts one authenticated portable JSON series into
one new DSS file. Its source, request, and result contracts are respectively
`hms-commander/portable-flow-series/1.0`,
`hms-commander/flow-boundary-request/1.0`, and
`hms-commander/flow-boundary-result/1.0`. The existing hydrologic-handoff 1.0
contracts remain unchanged. Installed JSON schemas are under
`hms_commander/contracts/`.

This worker supplies storage and readback checks. The caller selects the
provider, initialization, reach, model boundary, and contribution. The worker
copies one series without adding it to another inflow. It records the supplied
mapping, target-boundary, and contribution IDs, but cannot determine whether a
separate model inflow already represents that contribution. The integrated
specification must resolve that question before admitting the output.

### Source and request

The source JSON contains only `schema`, `selector`, `units`, `data_type`,
`interval_minutes`, `timestamps`, and `values`. `selector` identifies the exact
selected record, including its provider initialization where applicable. A
portable fixture is:

```json
{
  "schema": "hms-commander/portable-flow-series/1.0",
  "selector": "fixture:nwm-short-range:2026-11-18T00:00:00Z:reach-101",
  "units": "CMS",
  "data_type": "INST-VAL",
  "interval_minutes": 60,
  "timestamps": [
    "2026-11-18T00:00:00Z",
    "2026-11-18T01:00:00Z",
    "2026-11-18T02:00:00Z"
  ],
  "values": [0, 17.5, 9.25]
}
```

The request binds the source file's exact SHA-256, rather than a second array
of values. Relative source/output paths resolve against the request file.
The complete portable request fixture is
`tests/fixtures/flow_boundary/request-v1.0.json`; its neighboring source fixture
contains the corresponding bytes. Copy both files into a new run-owned directory
before using them. The fixture values are synthetic and do not represent a
qualified forecast or native DSS fixture.

| Request field | Required meaning |
| --- | --- |
| `schema`, `operation_id` | Versioned request identity and caller operation ID |
| `source.path`, `source.sha256`, `source.selector` | Exact preparation JSON bytes and selector |
| `model_window.start`, `.end` | Inclusive physical timestamps, with explicit UTC offsets and whole minutes |
| `model_window.time_zone` | IANA zone used to express physical samples on the DSS model clock |
| `interval_minutes` | Positive regular interval dividing the complete window |
| `units` | Native DSS spelling: `CMS` or `CFS` |
| `unit_policy` | `identity`, or the explicit spelling alias `m3/s-to-CMS-alias` |
| `output_dss`, `output_pathname` | New destination and exact six-part FLOW pathname, with empty D-part and matching interval E-part |
| `routing` | `method: "identity"`, `mapping_id`, `target_boundary_id`, and `contribution_id` |
| `alignment_policy`, `missing_value_policy`, `conversion` | Respectively `exact`, `reject`, and `identity` |
| `sentinel_threshold` | Finite negative rejection threshold; every ordinate must be greater |

Only instantaneous, finite values are accepted. Every sample, including both
endpoints, must appear exactly once in increasing order. The worker rejects
missing, duplicate, reversed, off-lattice, and incompatible records. It does
not synthesize an initial sample when a forecast starts at its first lead hour.
It also rejects ambiguous/nonexistent model-clock times and windows crossing a
UTC-offset transition. Sources are limited to 16 MiB and 100,000 samples;
requests and results are limited to 64 KiB.

The `m3/s-to-CMS-alias` policy accepts source label `m3/s` and writes native
label `CMS` without changing any numerical ordinate. Other aliases, numerical
unit conversion, interpolation, time shifting, scaling, and routing methods
are unsupported by this contract. Apply any approved transformation through its
own owner API and preserve its evidence before preparing this input.

### Execution, evidence, and recovery

After provisioning a trusted DSS runtime, invoke the worker with:

```powershell
hms-flow-boundary-worker --request request-v1.0.json --result result.json
```

Java/DSS work runs in an owned child with a 300-second timeout. The child uses
`DssCore.write_timeseries`, catalog inspection, and the existing time-series
readback checks. The parent authenticates the source again after child exit,
then hashes the released output. Atomic exclusive links publish the output and
terminal result; the destination filesystem must support hard links. Concurrent
writers cannot replace each other's published bytes. A process interruption
between these two publications can leave an output without an accepted result.
That output is refused on retry; inspect it and select a new run destination.

The command returns `0` for success or verified identical reuse, `2` for invalid
input, `3` for identity/publication conflicts, and `4` for materialization
failure. A failed native attempt retains its `.flow-boundary-*` directory,
including owned-process diagnostics and partial files. Its failure receipt
names `diagnostics_directory` when that destination is available. Conflicting
writers do not publish failure receipts over another writer's result. Do not
retry an attempt whose retained supervision evidence reports uncertain process
cleanup until the operator establishes that the process has ended.

Successful evidence binds the preparation hash, selector, model/physical
clocks, mapping/routing, source/native unit labels, output pathname and SHA-256,
sample count, and ordinate/timestamp hashes. `HmsFlowBoundaryWorker.verify()`
reauthenticates the request, preparation source, result bindings, and complete
DSS bytes without starting Java. An identical retry preserves the result bytes.
Changed sources or requests require new output and result destinations.
`forecast_eligible` remains `false`.

### Integration identity and qualification limits

`preparation_source_sha256` identifies the portable JSON consumed by this worker.
It is **not** FloodForecast direct-cycle `source_data_sha256`, which identifies
the original StormHub source series. An upstream hash merely copied into an
adapter record would not establish numerical lineage.

FloodForecast integration increment F10 must authenticate both original and
preparation bytes, recompute the exact selected reach/sample/window projection,
preserve the original initialization identity, and check the declared unit-label
policy. Only then can its separate integration admission bind the original
`source_data_sha256`, this package's authenticated evidence, and the native
`output_sha256`. Current provider-unit rules remain unchanged. This worker alone
does not make the current direct-cycle contract executable.

Portable tests use real numeric arrays with an injected native I/O boundary.
They establish contract behavior, including refusal and race handling; they do
not establish native DSS compatibility or operating readiness. Native
qualification is a separate opt-in test on the approved Windows runtime:

```powershell
$env:HMS_FLOW_NATIVE_QUALIFICATION = "1"
python -I -m pytest tests/test_flow_boundary_worker.py -m requires_java `
  --basetemp C:/Temp/f07-native-qualification-unique
```

Use a fresh, absent temporary directory and the deliberately installed candidate
package with its trusted DSS runtime. Retain that directory's request, source,
DSS, and result together with the tested package/runtime identities. No native
qualification is claimed by the portable test suite. Native qualification and
F10 integration admission remain outstanding gates.
