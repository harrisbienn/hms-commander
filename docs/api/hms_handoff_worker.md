# HmsHandoffWorker

Versioned process boundary for authenticated multi-source HMS-to-RAS DSS
materialization.

::: hms_commander.HmsHandoffWorker
    options:
      show_root_heading: true
      show_source: true
      members_order: source

## Portable flow materialization

`HmsFlowBoundaryWorker` accepts a separate portable-series contract. It leaves
the hydrologic-handoff 1.0 request and result unchanged.

::: hms_commander.HmsFlowBoundaryWorker
    options:
      show_root_heading: true
      show_source: true
      members_order: source
