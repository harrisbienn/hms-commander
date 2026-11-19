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

## Provisional forecast initialization (request 1.4)

Request `hms-commander/scenario-worker-request/1.4` adds explicit provisional
static initialization and emits result `hms-commander/scenario-worker-result/1.1`.
Requests 1.0–1.3 and result 1.0 retain their existing behavior. Version 1.4 retains
the source run name and requires an explicitly selected executable file.

The supported method preserves the selected basin model and meteorologic
initialization parameters, with exactly one `Save State Type: None` in the
selected run. Saved/restart states and implicit NWM wetness conversions are not
supported. Static parameters do not establish equivalence to observed catchment
state. The receipt always records `disposition="provisional-static"`,
`observed_state_equivalent=false`, `predecessor=null`, and
`forecast_eligible=false`; no forecast output becomes a future analysis state.
HEC-HMS describes basin and meteorologic initial conditions separately from
optional start states in its [simulation-run documentation](https://www.hec.usace.army.mil/confluence/hmsdocs/hmsum/4.11/hydrologic-simulation/simulation-runs).

Supply two independently selected JSON references under `initialization`:

```json
{
  "initialization": {
    "evidence": {"path": "inputs/initialization-plan.json", "sha256": "<plan SHA-256>"},
    "model_manifest": {"path": "inputs/initialization-model.json", "sha256": "<manifest SHA-256>"}
  }
}
```

These are fragments; replace the illustrative hashes with actual lowercase
SHA-256 values. Request paths resolve from the worker's current directory, as in
the existing worker contract. The packaged schemas define the complete fields:

| Artifact | Schema file | Identity scope |
|---|---|---|
| Request | `scenario-worker-request-v1.4.schema.json` | Independent plan and model-manifest references |
| Plan | `forecast-initialization-v1.0.schema.json` | Physical valid instant, method, model manifest, executable, forcing and optional warm-up |
| Model manifest | `initialization-model-v1.0.schema.json` | Exact selected project, run, basin, meteorologic, control and grid files and component names |
| Receipt | `forecast-initialization-evidence-v1.0.schema.json` | Authenticated source identities and, after preparation, selected clone identities |
| Result | `scenario-worker-result-v1.1.schema.json` | Separate admission and retained post-execution receipts |

The model manifest's file paths are portable paths relative to the source
project folder. Each identity must match the file actually selected by the
request and native text references. This manifest authenticates selected HMS
configuration, **not the entire integrated model package**. The calling
orchestrator must bind it to its independently approved package tree and other
required model assets. Similarly, the executable hash identifies one file;
it does not qualify JVM libraries or the complete installed runtime.

Before running the worker, source admission can be checked without creating a
workspace, starting an engine, or reading DSS through a native library:

```python
from hms_commander import HmsForecastInitialization

receipt = HmsForecastInitialization.prepare("request.json", "source-receipt.json")
evidence = HmsForecastInitialization.verify("request.json", receipt)
```

`prepare` publishes immutable, complete receipt bytes. An identical existing
receipt can be reused; a conflicting destination is refused. Concurrent
publication may also refuse an existing destination; use `verify` to check the
winner. A receipt with `status="source_verified"` attests source admission only.
The worker separately verifies actual prepared run references, static parameters,
control window, interval, and the meteorologic model → grid → staged DSS file
and exact pathname chain immediately before compute. Preparation may change
only the scenario wiring, description, requested control times/interval, and
execution metadata allowed by this contract.

Successful result 1.1 contains `preparation.initialization` with precompute
identities and `preparation.retained_initialization` with verified post-execution
identities. This preserves the inputs originally admitted while allowing known
run execution-date/time updates. Identical-result reuse reauthenticates source
files, plan, manifest, forcing, explicit gage inputs, executable, prepared
configuration and the retained snapshot before checking outputs. Changed
inputs refuse reuse; retained evidence is not overwritten.
The retained snapshot independently verifies current bytes. Basin, meteorologic,
control and grid identities must also equal their precompute identities. The
precompute project/run hashes remain audit records when execution updates their
metadata; the original bytes are not retained by this API, so those earlier
hashes cannot be independently reproduced after such updates. The caller must
retain the external result checksum to authenticate that audit record.

### Physical clock and declared warm-up

Version 1.4 requires whole-minute UTC physical/model timestamps. The plan's
`valid_at` must equal the model start exactly; earlier and later instants are
refused. The required `clock` declaration names `physical_time_zone`,
`model_wall_time_zone`, and `forcing_time_zone` as `UTC`, with `time_shift="none"`.
This is an explicit caller disposition, not native clock verification.

HEC-HMS describes simulation times as an arbitrary local wall clock without
daylight-saving adjustment and provides separate precipitation and grid-output
shifts. See [control specifications (4.9)](https://www.hec.usace.army.mil/confluence/hmsdocs/hmsum/4.9/projects-and-control-specifications/control-specifications)
and [gridded precipitation time shifts (4.11)](https://www.hec.usace.army.mil/confluence/hmsdocs/hmsum/4.11/meteorology-description/precipitation).
This API preserves project/control time-zone labels, including non-UTC labels,
and records them as evidence; it does not infer the computational clock from
those labels or silently rewrite them. Missing shift fields are retained as
absence, not evidence of zero native shift. The first supported text grammar
admits absent precipitation shift controls and absent or exactly zero
`Grid Write Time Shift` for output grids. Other shift/normalization controls
and duplicate clock controls are refused pending explicit support.

Every receipt records `native_clock.native_clock_verified=false`. The later
FloodForecast F10 integration must prove the input records, requested physical
window, actual selected control text, and output timestamps agree in the
designated-engine pilot. If the source needs clock preparation, perform it in
an owner-prepared clone with a new manifest before admission; do not change the
canonical source. No installed-engine or physical-record clock proof is claimed
by these portable tests.

An optional `warm_up` object records UTC `start`, `end`, and `forcing_sha256`.
It must be a nonempty prefix beginning at the model start, ending before the
model end on the computation interval, and referencing the selected forcing.
The receipt retains these exact bounds with
`warm_up_status="declared-not-qualified"`. A whole-file forcing hash proves
neither observed precipitation in that prefix nor adequate spin-up. F10 must
bind owner-verified source/observed seam evidence to the declared prefix;
scientific spin-up and state-equivalence assessment remain separate.

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

The boundary transformer resolves DSS6 `5MIN` and DSS7 `5Minute` catalog
spellings to one exact physical source family, retaining both requested and
resolved selectors in its evidence. It still rejects a different run or
interval and refuses competing physical families.

Older requests 1.0/1.1/1.2 are unchanged and reject the new policy field. Direct
API callers opt in with
`HmsScenario.prepare_workspace(..., run_name_policy="preserve-source")`;
the default `"scenario"` policy continues creating a scenario-named run.

## Native DSS stage isolation

When a request includes spatial transfer, hydrograph export runs in a child
process through `HmsResultsProducts.export_isolated()`. Windows DSS6 readers
can retain a native file lock after closing a DSS handle. Ending the export
child releases that lock before excess export hashes and reads the same file.
The product schema and conditional qualification results are unchanged, and
the parent verifies the source checksum again after export. Temporary request
files are written beside the product destination, never in the source model.

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
