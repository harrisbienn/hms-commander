# Scenario Worker

Use `hms-scenario-worker` when an orchestrator must execute one HMS scenario in
the HMS Commander environment without importing HMS Commander into the
orchestrator process. The worker prepares an input-only project clone, executes
the run, validates the exact HMS completion evidence, and exports the
package-owned hydrologic products.

## Command

```powershell
hms-scenario-worker `
  --request C:\runs\attempt-001\hms-request.json `
  --result C:\runs\attempt-001\hms-result.json
```

The result path must be new. If it already contains a successful result for the
same canonical request, the worker verifies the output DSS and product-manifest
checksums and exits successfully without rerunning HMS. A failed attempt keeps
its workspace and result as evidence; a retry should use a new attempt path.

## Request Contract

The packaged JSON Schema is
`hms_commander/contracts/scenario-worker-request-v1.0.schema.json`. A complete
request has this form:

```json
{
  "schema": "hms-commander/scenario-worker-request/1.0",
  "scenario": {
    "scenario_id": "lwi-r3-rank-001",
    "specification_sha256": "1111111111111111111111111111111111111111111111111111111111111111"
  },
  "source_model": {
    "project": "C:\\models\\deloutre-hms",
    "project_file_sha256": "2222222222222222222222222222222222222222222222222222222222222222",
    "run": "Baseline Run",
    "grid": "AORC Grid"
  },
  "forcing": {
    "dss": "C:\\stormhub\\target\\rank-001.dss",
    "sha256": "3333333333333333333333333333333333333333333333333333333333333333",
    "pathname": "/AORC-TRANSPOSED/SHG_1000/PRECIPITATION///INCREMENTAL/"
  },
  "model_window": {
    "start": "2019-09-18T13:00:00",
    "end": "2019-09-19T13:00:00",
    "time_zone": "America/Chicago",
    "interval_minutes": 5
  },
  "workspace": "C:\\runs\\attempt-001\\hms-workspace",
  "products": {
    "directory": "C:\\runs\\attempt-001\\hydrologic-products",
    "required_pathnames": [
      {
        "mapping_id": "upstream-001",
        "pathname": "//OUTLET/FLOW//5Minute/RUN:SCENARIO/",
        "ras_boundary": "Upstream BC"
      }
    ]
  },
  "execution": {
    "timeout_seconds": 3600,
    "hms_executable": "C:\\Program Files\\HEC\\HEC-HMS\\4.13\\hec-hms.cmd",
    "max_memory": "8G"
  }
}
```

`specification_sha256` is the calling orchestrator's result-specification
identity. HMS Commander also hashes the normalized request, including resolved
local paths, and records that request hash in the result. This separates the
portable scientific identity from the exact local worker invocation.

The model timestamps are naive HMS local/model times. `time_zone` records the
interpretation explicitly; the worker does not perform time-zone conversion.
The forcing DSS and source `.hms` project file are checksum-verified before the
workspace is created.

## Result Contract

The worker atomically writes
`hms-commander/scenario-worker-result/1.0`. A successful result includes:

- preparation checks and the GUI-verifiable workspace manifest;
- the HMS execution artifact and exact completion-marker evidence;
- output DSS size and SHA-256 identity;
- hydrologic product-manifest identity and raw qualification facts;
- UTC stage timings, warnings, and the request/specification identities.

Invalid requests, timeouts, aborted runs, missing completion markers, empty
outputs, and product failures return a nonzero exit code. When the result path
is writable and new, the worker still records an identity-bound failure result
with a stable `error.classification` and `retryable` flag.

The worker never evaluates the engineering hydrologic-handoff gate. It exports
mechanical facts through `HmsResultsProducts`; the owning study applies its
versioned policy later.
