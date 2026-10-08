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
  "gage_inputs": [
    {
      "gage_name": "MVK_Ouachita",
      "dss": "C:\\runs\\inputs\\qualification-gages.dss",
      "sha256": "4444444444444444444444444444444444444444444444444444444444444444",
      "pathname": "//OUJ_OUACHITAATFELSENTHAL/FLOW//1HOUR/QUALIFICATION/"
    }
  ],
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
The forcing DSS, each optional `gage_inputs` DSS, and the source `.hms` project
file are checksum-verified before the workspace is created. Gage inputs are
copied below the isolated workspace and only the named cloned `.gage` entries
are rewired. Unknown or duplicate gages, ambiguous project gage files, checksum
drift, and colliding input basenames fail closed; the canonical model is never
edited.

## Delivered excess publication

Request `hms-commander/scenario-worker-request/1.2` adds explicit
`spatial_transfer.method = "hms-subbasin-centroid-delivered-v1"`. Use the same
subbasin transfer fields and an authenticated centroid/full-cell allocation map
(map 2.0 or 3.0). The worker dispatches to
`HmsSubbasinTransfer.apply_delivered_transfer_map_to_dss()`; the map's allocation
method stays `hms-subbasin-centroid-full-cell-v1` while the product method
identifies the delivered publication convention. A different map method fails.

The result envelope remains 1.0 and authenticates product/audit 3.0, including
the actual published start/end and origin. For five-minute excess over model
00:00–01:00, coverage is 00:05–01:05. Consumers must retain that offset; successful
publication does not prove coverage of the first five model minutes. Existing
requests 1.0/1.1 reject this method. Request 1.2 also supports older explicit
methods with their unchanged publication behavior. No engineering acceptance
or forecast eligibility is assigned.

## Preserving the source run identity

Request `hms-commander/scenario-worker-request/1.3` requires
`source_model.run_name_policy = "preserve-source"`. The selected source run
keeps its name inside the isolated project clone. Its prepared meteorologic
model, control, grid, output DSS, and log remain scenario-specific. Preparation
clears historical execution timestamps without changing basin parameters or
the canonical source project.

Use this version when result selectors and downstream transformations name the
delivered run. Every required result pathname must select the exact
`RUN:<source_model.run>` F-part (case-insensitive); a different or wildcard run
is rejected before preparation. All previously supported spatial-transfer
methods remain available with their explicit publication conventions.

The result records `preparation.run_identity` with `policy`, `source_run`, and
`prepared_run`. Consumers should verify these against their requested run and
`execution.run_name`. Separate workspaces and output files provide scenario
isolation even when the DSS F-parts are identical.

Older requests 1.0/1.1/1.2 are unchanged and reject the new policy field. Direct
API callers opt in with
`HmsScenario.prepare_workspace(..., run_name_policy="preserve-source")`;
the default `"scenario"` policy continues creating a scenario-named run.

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

## External gage bindings

`gage_inputs` stages authenticated DSS assets in the scenario clone and binds
each named gage as `External DSS`. For a manual-entry gage, preparation removes
historical start/end limits and retains the variant structure required by HMS 4.9, preserving
gage-level units, type, description, and location metadata. Ambiguous multiple
variants are rejected. The source project remains unchanged.

For synthetic clocks, supply gage series that cover the complete model window,
including both endpoints for instantaneous flow. Rebinding does not shift or
invent DSS values. `HmsGage.bind_external_dss()` exposes the same operation for
package preparation; `update_gage()` remains a reference-only edit.
