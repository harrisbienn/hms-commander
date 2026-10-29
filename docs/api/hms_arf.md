# HmsArf

Areal reduction factor helpers for design-storm workflows.

::: hms_commander.HmsArf
    options:
      show_source: true
      heading_level: 2
      show_root_heading: true
      show_root_toc_entry: false
      members_order: source


## Texas 1-day ARF (Asquith 1999)

`HmsArfTexas` implements the circular-watershed and cell-based areal-reduction
factors of USGS WRIR 99-4267 (Table 7, Table 8) for Austin, Dallas and Houston.
It does not edit met files.

**Scope.** 24-hour (1-day) design storm, recurrence interval of 2 years or
greater, circular-equivalent radius 0-50 mi, and the three study areas.
`extrapolate=True` relaxes only the radius/area domain (beyond 50 mi, with a
logged warning). A duration other than 24 hr and a recurrence interval below
2 years always raise `ValueError`, regardless of `extrapolate`; WRIR 99-4267
(p. 25) limits the method to the one-day duration. Non-finite or negative
circular radii/areas and scaling depths raise `ValueError`; cell areas must
also be positive. Invalid numeric inputs (including bools, strings, and values
outside the float64 range) raise `ValueError`. A computed ARF or S2 outside
(0, 1] also raises `ValueError` from `circular_arf`, `noncircular_arf`,
`depth_distance` and therefore
`scale_depth` and `scale_hyetograph`; this can occur only with
`extrapolate=True`. The TxDOT HDM
cautions that applicability diminishes with distance from Austin, Dallas and
Houston and as the design-storm duration departs from 1 day; the module does
not check the watershed location.

**Dallas 24-27 mi row.** By default the module reproduces Table 7 as printed,
including an ARF intercept of 0.6800 for Dallas, 24 <= r <= 27 mi. The S2
intercept in that row is 0.6880, and in the other 29 rows the ARF intercept
equals the S2 intercept; 0.6880 makes the ARF approximately continuous at
r = 24 mi (a residual of about 0.0003 remains, like the other joins). Joins
select the higher-radius segment, so the corrected or printed row applies over
24 <= r < 27 mi.
Pass `dallas_intercept_correction=True` to `circular_arf`, `scale_depth` or
`scale_hyetograph` to use 0.6880 (ARF about 0.008 higher for Dallas areas of
roughly 1,810-2,290 mi2). No erratum has been located; the correction is an
inference from the report's own equations.

**HDM Table 4-12 typos.** The HDM table differs from WRIR Table 7 in two
Houston entries: the 7-11 mi ARF intercept (HDM 0.8708, WRIR 0.8078) and the
15-20 mi ARF slope (HDM 0.00053, WRIR 0.0053). The module follows the WRIR.

```python
from hms_commander import HmsArfTexas
arf = HmsArfTexas.circular_arf("dallas", area_mi2=50.3, recurrence_interval_yr=100)  # 0.85
depth = HmsArfTexas.scale_depth(9.55, "dallas", area_mi2=50.3, recurrence_interval_yr=100)
```

::: hms_commander.HmsArfTexas
    options:
      show_source: false
      heading_level: 3
      show_root_heading: true
      show_root_toc_entry: false
      members_order: source
