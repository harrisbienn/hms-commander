# HmsForcingCoverage

Audit the geometric support of a precipitation valid-data mask over the active
HMS computation-cell polygons. This reads no DSS internals and changes no model
or forcing data. Install `hms-commander[gis]` for the geometry dependencies.

```python
from hms_commander import HmsBasin, HmsForcingCoverage, HmsSqlite

cells = HmsSqlite.get_discretization(sqlite_path)
names = HmsBasin.get_subbasins(basin_path)["name"].tolist()
audit = HmsForcingCoverage.audit_grid_mask(
    cells,
    selected_subbasins=names,
    grid_definition=grid_definition,
    valid_mask=valid_mask,
)
```

Supply a boolean mask from native grid readback (`numpy.isfinite(grid["data"])`
for a `RasDss.read_grid()` result). Finite zero precipitation is valid data;
missing values are not zero. Authenticate source files, selectors and time
coverage separately. Apply the audit to every distinct mask across the selected
records; identical grid dimensions do not prove identical spatial support.

The grid definition contains `definition_id`, a projected CRS with meter axes,
`shape` as `[rows, columns]`, the lower-left outer-edge `origin`,
`cell_size_meters`, and `row_order = "south_to_north"`. An explicit different
row order is rejected rather than silently flipped. Input polygons are reprojected
to that CRS without modifying the caller's GeoDataFrame.

The `hms-commander/forcing-mask-coverage/1.0` result records:

- selected and inactive cells/subbasins, with missing selected discretizations
  listed explicitly;
- per-subbasin and aggregate computation-cell polygon area, covered area,
  inside-grid no-data area, and outside-grid area, all in square meters;
- counts of cells intersecting no-data or extending outside the grid;
- normalized grid, selected geometry and row-major boolean mask hashes, plus a
  deterministic hash of the complete unsigned audit.

Areas sum the selected computation-cell polygons. They are not the basin's
parameter areas or a union across subbasins. A polygon touching a missing grid
cell only along an edge contributes zero missing area. Historical SQLite
discretizations are excluded by the explicit active-subbasin selection and
reported separately. A selected subbasin without cells remains an incomplete
audit finding; it is never silently treated as fully covered.

This is raw geometric evidence. It does not emulate the HMS sampling/interpolation
kernel, evaluate precipitation amounts or temporal completeness, assign an
acceptance tolerance, or authorize hydraulic/library/forecast use. Interpret
nonzero areas alongside model-specific engine diagnostics and study policy.
Tiny numerical slivers remain visible rather than being hidden by a built-in
engineering tolerance. Review method choices and engineering criteria against
the HEC-HMS documentation and the governing study requirements.

::: hms_commander.HmsForcingCoverage
