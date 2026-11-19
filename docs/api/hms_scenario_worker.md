# HmsScenarioWorker

Versioned process boundary for isolated scenario preparation, execution, and
hydrologic product export.

Request 1.4 adds provisional static initialization with result 1.1. The
no-compute `HmsForecastInitialization` API creates and reauthenticates source
receipts; the scenario worker checks the prepared configuration before compute
and rechecks retained inputs on successful reuse. See the
[initialization contract](../user_guide/scenario_worker.md#provisional-forecast-initialization-request-14)
for clock declarations, identity scope and scientific limitations.

An optional authenticated `spatial_transfer` request causes the worker to
export the run's incremental excess as a qualification-only target-grid DSS.
The request accepts the established `nearest-active-hms-cell` inputs or the
explicit `hms-subbasin-volume-conserving-v1` variant with an authenticated
compiled transfer map and numerical tolerances. The resulting manifest,
audit, DSS identity, and method-specific evidence are included under
`products.spatial_transfer`; consuming orchestrators should use that boundary
instead of reopening HMS HDF or selecting raw excess pathnames.

::: hms_commander.HmsScenarioWorker
    options:
      show_root_heading: true
      show_source: true
      members_order: source

::: hms_commander.HmsForecastInitialization
    options:
      show_root_heading: true
      show_source: true
      members_order: source
