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
- writes the DSS in an isolated child process so native handles close before
  authentication;
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
