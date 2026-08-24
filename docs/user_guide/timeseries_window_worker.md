# Time-Series Window Worker

`HmsTimeSeriesWorker` creates a new DSS file from one or more complete regular
time-series windows. It translates every timestamp by one constant wall-clock
offset and preserves the source ordinates, cadence, units, DSS type, and
relative phase between series.

This operation is intended for explicitly approved qualification workflows. A
translated hydrograph is a counterfactual boundary condition; it is not an
observation, historical reconstruction, or forecast.

## Safety contract

The worker:

- authenticates every source DSS file before and after reading it;
- requires exact inclusive source coverage at the declared interval;
- rejects duplicate, missing, sentinel, and non-finite values;
- rejects unit, DSS type, or interval mismatches;
- rejects ambiguous, nonexistent, or offset-changing local-time windows;
- applies one common timestamp offset without changing ordinates;
- performs all Java/HEC-DSS reads, writes, and readback in a dedicated child
  process, then re-authenticates the sources after that process exits;
- writes and verifies a temporary DSS before atomically publishing the
  requested output;
- refuses an existing output unless an existing successful result proves an
  identical completed request; and
- records source, output, timestamp, and ordinate hashes in an immutable JSON
  result.

It does not interpolate, extrapolate, repeat, scale, synthesize recession, or
mutate a source file.

## Request

Create a JSON request like the following. Paths may be absolute because the
request is a local worker input; do not commit machine-specific requests.

```json
{
  "schema": "hms-commander/timeseries-window-request/1.0",
  "operation_id": "qualification-static-boundaries-96h",
  "qualification": {
    "status": "qualification_only",
    "forecast_eligible": false,
    "basis_issue": "https://example.invalid/issues/62"
  },
  "source_window": {
    "start": "2023-04-01T13:00:00",
    "time_zone": "America/Chicago"
  },
  "target_window": {
    "start": "2019-09-18T13:00:00",
    "end": "2019-09-22T13:00:00",
    "time_zone": "America/Chicago"
  },
  "output_dss": "C:/runs/example/inputs/static-boundaries.dss",
  "sentinel_threshold": -1e20,
  "series": [
    {
      "series_id": "upstream-hourly-flow",
      "source_dss": "C:/models/source/input-hourly.dss",
      "source_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
      "source_pathname": "/BASIN/UPSTREAM/FLOW//1HOUR/MODEL/",
      "output_pathname": "/BASIN/UPSTREAM/FLOW//1HOUR/MODEL/",
      "interval_minutes": 60,
      "units": "CFS",
      "data_type": "INST-VAL"
    }
  ]
}
```

Source and target timestamps are timezone-naive model wall times. The named
IANA timezone makes their interpretation explicit. Both windows must use the
same timezone and must not cross a daylight-saving offset transition.

## Run

```powershell
hms-timeseries-worker --request request.json --result result.json
```

The command returns `0` for a new success or a verified identical reuse, `2`
for an invalid request, `3` for an identity or existing-artifact conflict, and
`4` for a DSS translation or verification failure.

## Evidence interpretation

The result's `ordinate_sha256` proves that each output series contains the same
ordered IEEE-754 double values as its selected source window. Separate source
and target timestamp hashes prove exact coverage on each time axis. The result
also calls out whether the final ordinate is the maximum; an ending peak is an
engineering warning that may require a longer source window or a documented
recession disposition before use.

## Validation

The deterministic worker tests do not require Java or installed HEC software:

```powershell
python -m pytest tests/test_timeseries_worker.py
```

To additionally exercise a real DSS write/read round trip in a prepared DSS
environment:

```powershell
$env:HMS_COMMANDER_RUN_DSS_INTEGRATION = "1"
python -m pytest tests/test_timeseries_worker.py -m requires_java
```
