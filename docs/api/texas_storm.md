# TexasStorm

Texas dimensionless hyetographs: empirical percentile curves, the triangular
model, and the L-gamma model.

## Usage

```python
from hms_commander import TexasStorm

# Empirical: 2nd-quartile, median curve, 24-hr class '12-24', 8 in
hyeto = TexasStorm.generate_hyetograph(
    total_depth_inches=8.0, duration_hours=24, method="empirical",
    quartile=2, percentile=50, duration_class="12-24", time_interval_min=30,
)

# Triangular: the parameter set must be named (the sources publish two)
hyeto = TexasStorm.generate_hyetograph(
    8.0, 12, "triangular", param_set="nws_hourly", time_interval_min=60,
)

# L-gamma
hyeto = TexasStorm.generate_hyetograph(15.0, 24, "lgamma", duration_class="12-24")
```

The result has the same columns as `ScsTypeStorm` and `Atlas14Storm`
(`hour`, `incremental_depth`, `cumulative_depth`, with a t=0 row). The source
and selections are in `hyeto.attrs["provenance"]`.

## HDM recommendation and duration-class selection

The TxDOT HDM (2019, p. 4-80) recommends the 50th-percentile combined (first-
through fourth-quartile) curve for the 0-72 hr group, tabulated in HDM
Table 4-16, for any storm duration. To request it:

```python
hyeto = TexasStorm.generate_hyetograph(
    total_depth_inches=8.0, duration_hours=24, method="empirical",
    quartile="all", duration_class="0-72", percentile=50,
)
```

The `0-72` class is never selected automatically. When `duration_class` is
omitted, the empirical method picks the duration-specific class (0-6, 6-12,
12-24, 24-72 hr) that contains the duration, as offered in SIR 2004-5075; the
HDM does not use these duration-specific classes. `quartile` must always be
given.

## Sources and data origin

| Method | Source | Data origin |
|--------|--------|-------------|
| empirical | Williams-Sether and others (2004), USGS SIR 2004-5075, Supplements 4 and 5 (SIR Table 4 is the median of Supplement 5, 0-72 hr) | Runoff-producing storms on small watersheds in Texas, 1959-86 |
| triangular, `usgs_runoff` | Asquith and others (2005), TxDOT 0-4194-4, Tables 4-5; Eq. 1-2 (HDM Eq. 4-26, 4-27) | The same small-watershed runoff-producing storm database |
| triangular, `nws_hourly` | 0-4194-4 Tables 7-9; HDM 2019 Table 4-13 (the only triangular set the HDM publishes) | National Weather Service hourly point gages (Texas, Oklahoma, New Mexico), storms of at least 1 inch |
| lgamma | TxDOT 0-4194-4; HDM 2019 Eq. 4-28, Table 4-15 | See 0-4194-4 |

The tabulated empirical values are in `hms_commander/data/texas_empirical_hyetographs.csv`.
`scripts/texas_hyetograph_transcription.py` compares two independent readings
of the report (PDF text layer and OCR of the page images) and writes the file.

## Selections the caller must make

- **Triangular parameter set.** Two sets are published and they differ:
  `usgs_runoff` (0-4194-4 Tables 4-5: a = 0.23 for 0-24 hr, 0.35 for 24-72 hr)
  and `nws_hourly` (0-4194-4 Tables 7-9 and HDM Table 4-13: a = 0.02197,
  0.28936, 0.38959 for 5-12, 13-24, 25-72 hr). No default is applied.
- **Duration class.** Classes are 0-6, 6-12, 12-24, 24-72 hr (empirical);
  0-12, 12-24, 24-72 hr (L-gamma); and the triangular classes above. A
  duration on an overlapping class boundary needs an explicit
  `duration_class`. The `nws_hourly` classes do not overlap, so durations of
  exactly 12 or 24 hr select automatically (the 5-12 or 13-24 hr class,
  respectively). HDM says to use the class giving the more severe runoff for
  exactly 12 or 24 hr; that choice is left to the caller where classes overlap.
- **Quartile** (empirical): 1, 2, 3, 4, or `"all"`.
- **Non-monotone published columns** (empirical): see below.

## Supported durations

- All methods: more than 0 and at most 72 hr; the duration must be a whole
  multiple of `time_interval_min`.
- `nws_hourly`: 5-12, 13-24 and 25-72 hr (HDM Table 4-13, p. 4-74). Durations
  between the classes (12-13 hr and 24-25 hr) are not tabulated and are
  rejected; an explicit `duration_class` does not change this. Durations under
  5 hr are also rejected. A 6 hr storm uses the 5-12 hr class, which is how
  0-4194-4 Example 1 applies the model (it calls it the 0-12 hour model).
  The 5 hr lower bound reflects the hourly-data sample in 0-4194-4.

## Applicability

- The sources state a limit of less than about 160 mi2 for the empirical
  curves (SIR 2004-5075; HDM p. 4-77). Pass `drainage_area_sqmi` to get a
  `UserWarning` above the limit; the warning is issued for `method="empirical"`
  only. Other methods are not checked against an area limit.
- `total_depth_inches` and `duration_hours` must be finite and positive, and
  `drainage_area_sqmi`, if given, must be finite and positive. NaN and
  infinity are rejected with `ValueError`.
- Storms of at least 1 inch of rainfall (warning below), durations up to 72 hr.
- The source reports no data for the 10th and 90th percentiles of
  fourth-quartile 0-6 hr storms; requesting them raises `ValueError`.

## Empirical curve handling

- **End points.** The curves are tabulated at the centers of 2.5-percent
  intervals (2.5 to 97.5). Generation adds (0, 0) and (100, 100) and
  interpolates linearly. The SIR does not tabulate these end points; HDM
  Table 4-16 lists them.
- **2.5-percent value of the combined median.** HDM Table 4-16 prints 8.70
  for the 50th percentile at 2.5 percent of duration. SIR 2004-5075 Table 4
  and Supplement 5 give 6.37 (8.70 is the first-quartile value in SIR
  Table 4). This module uses the SIR value, 6.37.
- **Non-monotone published columns.** Eight tabulated columns decrease
  somewhere with time (Supplement 4: quartile 1 24-72 hr 75th, quartile 3
  0-6 hr 10th; Supplement 5: quartile 1 24-72 hr 90th, quartile 3 12-24 hr
  10th, 25th, 30th, 40th, 50th percentiles). All median curves other than
  quartile 3 12-24 hr are monotone, and the combined 0-72 hr curve is
  unaffected. The `nonmonotone` argument controls the behavior:
    - `"raise"` (default): `ValueError` naming the supplement table page and
      the opt-in option. Example: Supplement 5, p. 113, quartile 3, 12-24 hr,
      40th percentile.
    - `"running_max"`: the running maximum is applied. The number of adjusted
      ordinates (`monotone_points_adjusted`) and the largest adjustment in
      percent points (`max_monotone_adjustment_pct`; 1.09 for the case above,
      11.76 raised to 12.85 at 37.5 percent of duration) are recorded in
      `hyeto.attrs["provenance"]`. The sources do not prescribe this repair.

    The bundled CSV and `get_empirical_curve` return the published values
    unchanged.

```python
hyeto = TexasStorm.generate_hyetograph(
    5.0, 18, "empirical", quartile=3, duration_class="12-24", percentile=40,
    nonmonotone="running_max",
)
```

::: hms_commander.TexasStorm
    options:
      show_source: true
      heading_level: 2
      show_root_heading: true
      show_root_toc_entry: false
      members_order: source
