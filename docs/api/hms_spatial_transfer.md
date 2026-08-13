# HmsSpatialTransfer

Deterministic raw metrics for moving gridded HMS incremental excess from
computation cells to a portable target grid.

The API owns HMS result-HDF and basin-SQLite interpretation. A caller may use
RAS Commander to obtain a hydraulic target-grid definition, but qualification
thresholds, reviewer disposition, and forecast promotion remain outside HMS
Commander.

::: hms_commander.HmsSpatialTransfer
    options:
      show_source: false
      heading_level: 2
