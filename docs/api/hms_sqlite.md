# HmsSqlite

SQLite geometry and grid database helpers used by HMS geospatial workflows.

`HmsSqlite.export_subbasin_regions(database, basin, output, basin_id="study")`
exports the exact active basin names, their SQLite polygons and their union in
the declared projected metre CRS. The immutable JSON includes source hashes,
selection counts, overlap area and per-polygon topology/area/boundary metrics.
It is a geometry metadata export, not an RFC 7946 longitude/latitude GeoJSON.

Invalid polygons fail by default. Explicit `repair="make_valid"` repairs copies
and records every change; it never edits the source models. Outputs continue
to require engineering review. A downstream consumer must check partition
overlap, source/profile applicability and the recorded repair before approving
use. An unchanged area alone does not establish an acceptable repair.

::: hms_commander.HmsSqlite
    options:
      show_source: true
      heading_level: 2
      show_root_heading: true
      show_root_toc_entry: false
      members_order: source

