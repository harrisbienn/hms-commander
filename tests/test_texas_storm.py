"""
Unit Tests: TexasStorm Module

Reproduces published values for the Texas dimensionless hyetographs:

1. USGS SIR 2004-5075 Tables 4, 5, 6 (selected rows) against the bundled table
2. TxDOT HDM 2019 Table 4-14 worked example (triangular, 12 hr, 8 in)
3. TxDOT 0-4194-4 Table 6 (triangular, a=0.23/0.35) ordinates
4. L-gamma formula (HDM Eq. 4-28, worked example form)
5. End points, monotonicity, depth conservation
6. Explicit-selection and applicability behavior

Usage:
    pytest tests/test_texas_storm.py -v
"""

import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from hms_commander import TexasStorm, ScsTypeStorm

# --- Published values -----------------------------------------------------
# SIR 2004-5075 Table 4 (printed p. 15): smoothed medians, 0-72 hr
# columns: Q1, Q2, Q3, Q4, all quartiles combined
TABLE4_ROWS = {
    2.5: (8.70, 2.81, 2.51, 3.28, 6.37),
    5.0: (18.81, 5.89, 4.73, 5.16, 13.58),
    25.0: (68.65, 28.36, 18.66, 19.46, 48.54),
    50.0: (84.59, 65.46, 30.15, 34.27, 61.97),
    75.0: (91.53, 91.15, 75.58, 47.52, 81.61),
    97.5: (99.02, 99.65, 98.94, 96.48, 98.21),
}
# SIR Table 5 (p. 16) / Table 6 (p. 17): (10th, 50th, 90th), 0-72 hr
TABLE5_6 = {
    ("1", 2.5): (1.93, 8.70, 26.06),
    ("1", 20.0): (32.04, 60.57, 89.19),
    ("2", 2.5): (0.12, 2.81, 15.86),
    ("2", 50.0): (39.48, 65.46, 89.26),
    ("3", 2.5): (0.45, 2.51, 8.13),
    ("3", 50.0): (8.84, 30.15, 56.44),
    ("4", 2.5): (0.43, 3.28, 11.93),
    ("4", 50.0): (11.73, 34.27, 49.60),
}
# 0-4194-4 Table 6 (p. 15): triangular a=0.23 (0-24 hr) and a=0.35 (24-72 hr);
# F (percent) -> (percent depth 0-24, percent depth 24-72)
T4194_TABLE6 = {
    5: (1.09, 0.71),
    10: (4.35, 2.86),
    15: (9.78, 6.43),
    20: (17.4, 11.4),
    25: (27.0, 17.9),
    30: (36.4, 25.7),
    35: (45.1, 35.0),
    40: (53.3, 44.6),
    45: (60.7, 53.5),
    50: (67.5, 61.5),
    55: (73.7, 68.9),
    60: (79.2, 75.4),
    65: (84.1, 81.2),
    70: (88.3, 86.2),
    75: (91.9, 90.4),
    80: (94.8, 93.9),
    85: (97.1, 96.5),
    90: (98.7, 98.5),
    95: (99.7, 99.6),
    100: (100.0, 100.0),
}
# HDM 2019 Table 4-14 (p. 4-75, 4-76): 12 hr, 8 in, a=0.02197.
# t (hr) -> (depth in)
HDM_4_14_DEPTH = {
    0.13: 0.04,
    0.26: 0.17,
    0.50: 0.49,
    0.75: 0.81,
    1.0: 1.13,
    2.0: 2.32,
    3.0: 3.40,
    4.0: 4.36,
    5.0: 5.22,
    6.0: 5.96,
    7.0: 6.58,
    8.0: 7.09,
    9.0: 7.49,
    10.0: 7.77,
    11.0: 7.94,
    12.0: 8.00,
}
# intensity (in/hr) at integer hours: change over the preceding hour
HDM_4_14_INTENSITY = {
    2.0: 1.19,
    3.0: 1.08,
    4.0: 0.97,
    5.0: 0.85,
    6.0: 0.74,
    7.0: 0.62,
    8.0: 0.51,
    9.0: 0.40,
    10.0: 0.28,
    11.0: 0.17,
    12.0: 0.06,
}

# Columns of the published smoothed tables that are not monotone in the source
# (text layer and OCR agree). (supplement, quartile, duration class, percentile)
KNOWN_NONMONOTONE = {
    (4, "1", "24-72", 75),
    (4, "3", "0-6", 10),
    (5, "1", "24-72", 90),
    (5, "3", "12-24", 10),
    (5, "3", "12-24", 25),
    (5, "3", "12-24", 30),
    (5, "3", "12-24", 40),
    (5, "3", "12-24", 50),
}


def _depth_at(hyeto, hour):
    return float(np.interp(hour, hyeto["hour"], hyeto["cumulative_depth"]))


class TestEmpiricalTables:
    def test_data_file_shape(self):
        df = TexasStorm._load_empirical()
        # 2 supplements x 5 quartile groups x 5 duration classes x 39 intervals
        assert len(df) == 2 * 5 * 5 * 39
        assert set(df["supplement"]) == {4, 5}

    @pytest.mark.parametrize("row", sorted(TABLE4_ROWS))
    def test_table4_medians(self, row):
        for q, expected in zip(("1", "2", "3", "4", "all"), TABLE4_ROWS[row]):
            curve = TexasStorm.get_empirical_curve(q, "0-72", 50, trimmed=True)
            val = curve.loc[curve["duration_pct"] == row, "depth_pct"].iloc[0]
            assert val == pytest.approx(expected, abs=1e-9), (q, row)

    @pytest.mark.parametrize("key", sorted(TABLE5_6))
    def test_tables5_6_percentiles(self, key):
        q, row = key
        for pct, expected in zip((10, 50, 90), TABLE5_6[key]):
            curve = TexasStorm.get_empirical_curve(q, "0-72", pct)
            val = curve.loc[curve["duration_pct"] == row, "depth_pct"].iloc[0]
            assert val == pytest.approx(expected, abs=1e-9), (q, row, pct)

    def test_hdm_table_4_16_typo_not_reproduced(self):
        """HDM Table 4-16 lists 8.70 at 2.5%; the USGS combined value is 6.37."""
        curve = TexasStorm.get_empirical_curve("all", "0-72", 50)
        assert curve["depth_pct"].iloc[0] == pytest.approx(6.37)
        assert curve["depth_pct"].iloc[0] != pytest.approx(8.70)

    def test_selected_duration_class_points(self):
        """Spot values read from Supplement 5 (printed p. 101) and 4 (p. 76)."""
        c5 = TexasStorm.get_empirical_curve(1, "0-6", 50, trimmed=True)
        assert c5["depth_pct"].iloc[0] == 5.98
        assert c5["depth_pct"].iloc[-1] == 99.54
        c4 = TexasStorm.get_empirical_curve(1, "0-6", 50, trimmed=False)
        assert c4["depth_pct"].iloc[0] == 6.07
        assert c4["depth_pct"].iloc[-1] == 99.50

    def test_missing_data_raises(self):
        # SIR reports '--' for 10th and 90th percentiles, fourth quartile, 0-6 hr
        for pct in (10, 90):
            with pytest.raises(ValueError, match="no data"):
                TexasStorm.get_empirical_curve(4, "0-6", pct)

    @pytest.mark.parametrize("value", [50.0, np.float64(50), np.int64(50)])
    def test_integral_numeric_percentile_selector_is_normalized(self, value):
        curve = TexasStorm.get_empirical_curve(1, "0-6", value)
        expected = TexasStorm.get_empirical_curve(1, "0-6", 50)
        pd.testing.assert_frame_equal(curve, expected)

    @pytest.mark.parametrize("value", [1.0, np.float64(1), np.int64(1)])
    def test_integral_numeric_quartile_selector_is_normalized(self, value):
        curve = TexasStorm.get_empirical_curve(value, "0-6", 50)
        expected = TexasStorm.get_empirical_curve(1, "0-6", 50)
        pd.testing.assert_frame_equal(curve, expected)

    def test_generate_normalizes_integral_numeric_selectors(self):
        hyeto = TexasStorm.generate_hyetograph(
            5.0,
            10,
            "empirical",
            quartile=np.float64(1),
            percentile=np.float64(50),
            duration_class="6-12",
        )
        assert hyeto.attrs["provenance"]["quartile"] == "1"
        assert hyeto.attrs["provenance"]["percentile"] == 50

    @pytest.mark.parametrize(
        "quartile, percentile", [(1.5, 50), (1, 50.5), (5, 50), (1, 55)]
    )
    def test_nonintegral_or_unknown_empirical_selector_raises(
        self, quartile, percentile
    ):
        with pytest.raises(ValueError):
            TexasStorm.get_empirical_curve(quartile, "0-6", percentile)

    @pytest.mark.parametrize("trimmed", [True, False, np.bool_(True), np.bool_(False)])
    def test_boolean_trimmed_selector_is_accepted(self, trimmed):
        curve = TexasStorm.get_empirical_curve(1, "0-6", trimmed=trimmed)
        assert not curve.empty

    @pytest.mark.parametrize("trimmed", [None, np.nan, "False", 1, np.int64(1)])
    def test_nonboolean_trimmed_selector_raises(self, trimmed):
        with pytest.raises(ValueError, match="trimmed"):
            TexasStorm.get_empirical_curve(1, "0-6", trimmed=trimmed)

    def test_published_columns_nonmonotone_only_where_known(self):
        df = TexasStorm._load_empirical()
        found = set()
        for (s, q, d), g in df.groupby(["supplement", "quartile", "duration_class"]):
            for p in TexasStorm.EMPIRICAL_PERCENTILES:
                v = g[f"p{p}"].dropna().to_numpy()
                if len(v) and np.any(np.diff(v) < 0):
                    found.add((s, q, d, p))
        assert found == KNOWN_NONMONOTONE


class TestTriangular:
    @pytest.mark.parametrize("pct", sorted(T4194_TABLE6))
    def test_0_4194_4_table6(self, pct):
        F = pct / 100.0
        p_short = TexasStorm.triangular_cumulative(F, 0.23, 0.77) * 100
        p_long = TexasStorm.triangular_cumulative(F, 0.35, 0.65) * 100
        e_short, e_long = T4194_TABLE6[pct]
        # table is printed to 3 significant figures
        assert p_short == pytest.approx(e_short, abs=0.06)
        assert p_long == pytest.approx(e_long, abs=0.06)

    def test_hdm_table_4_14_depths(self):
        a, b = 0.02197, 0.97803
        for t, expected in HDM_4_14_DEPTH.items():
            d = 8.0 * float(TexasStorm.triangular_cumulative(t / 12.0, a, b))
            assert d == pytest.approx(expected, abs=0.006), t

    def test_hdm_table_4_14_generated(self):
        hyeto = TexasStorm.generate_hyetograph(
            8.0, 12, "triangular", param_set="nws_hourly", time_interval_min=60
        )
        assert len(hyeto) == 13
        cum = hyeto.set_index("hour")["cumulative_depth"]
        inc = hyeto.set_index("hour")["incremental_depth"]
        for t, expected in HDM_4_14_DEPTH.items():
            if t == int(t):
                assert cum.loc[float(t)] == pytest.approx(expected, abs=0.006), t
        for t, expected in HDM_4_14_INTENSITY.items():
            assert inc.loc[t] == pytest.approx(expected, abs=0.012), t

    def test_peak_location(self):
        # cumulative at F = a equals a (continuity at the peak)
        for pset in TexasStorm.TRIANGULAR_PARAMETER_SETS.values():
            for _, _, a, b in pset["classes"].values():
                assert a + b == pytest.approx(1.0)
                assert TexasStorm.triangular_cumulative(a, a, b) == pytest.approx(a)

    def test_param_set_required(self):
        with pytest.raises(ValueError, match="param_set"):
            TexasStorm.generate_hyetograph(8.0, 10, "triangular")

    def test_param_sets_differ_and_are_exposed(self):
        h1 = TexasStorm.generate_hyetograph(
            8.0, 10, "triangular", param_set="nws_hourly"
        )
        h2 = TexasStorm.generate_hyetograph(
            8.0, 10, "triangular", param_set="usgs_runoff"
        )
        assert h1.attrs["provenance"]["a"] == 0.02197
        assert h2.attrs["provenance"]["a"] == 0.23
        assert not np.allclose(h1["cumulative_depth"], h2["cumulative_depth"])


class TestLGamma:
    def test_formula_hdm_example(self):
        # HDM p. 4-77: d = 15 (t/24)^0.783 exp(0.4368 (1 - t/24)), 24 hr
        hyeto = TexasStorm.generate_hyetograph(
            15.0, 24, "lgamma", duration_class="12-24", time_interval_min=60
        )
        t = hyeto["hour"].to_numpy()
        expected = 15.0 * (t / 24) ** 0.783 * np.exp(0.4368 * (1 - t / 24))
        assert np.allclose(hyeto["cumulative_depth"], expected, atol=1e-12)

    def test_parameters_table_4_15(self):
        P = TexasStorm.LGAMMA_PARAMETERS
        assert P["0-12"][2:] == (1.262, 1.227)
        assert P["12-24"][2:] == (0.783, 0.4368)
        assert P["24-72"][2:] == (0.3388, -0.8152)

    @pytest.mark.parametrize("dur", [12, 24])
    def test_boundary_requires_explicit_class(self, dur):
        with pytest.raises(ValueError, match="boundary"):
            TexasStorm.generate_hyetograph(5.0, dur, "lgamma")


class TestInvariants:
    CASES = (
        [
            dict(method="triangular", param_set=s, duration_hours=d)
            for s, d in (
                ("nws_hourly", 8),
                ("nws_hourly", 18),
                ("nws_hourly", 48),
                ("usgs_runoff", 12),
                ("usgs_runoff", 48),
            )
        ]
        + [dict(method="lgamma", duration_hours=d) for d in (6, 18, 48)]
        + [
            dict(method="empirical", quartile=q, duration_hours=d, percentile=p)
            for q in (1, 2, 3, 4, "all")
            for d in (3, 9, 18, 48)
            for p in (25, 50, 75)
        ]
    )

    @pytest.mark.parametrize("kw", CASES)
    def test_endpoints_monotone_conservation(self, kw):
        if kw["method"] == "empirical":
            kw = dict(kw, nonmonotone="running_max")
        hyeto = TexasStorm.generate_hyetograph(7.3, time_interval_min=30, **kw)
        assert hyeto["hour"].iloc[0] == 0.0
        assert hyeto["incremental_depth"].iloc[0] == 0.0
        assert hyeto["cumulative_depth"].iloc[0] == 0.0
        assert hyeto["hour"].iloc[-1] == pytest.approx(kw["duration_hours"])
        assert hyeto["cumulative_depth"].iloc[-1] == pytest.approx(7.3, abs=1e-9)
        assert abs(hyeto["incremental_depth"].sum() - 7.3) < 1e-9
        assert (hyeto["incremental_depth"] >= 0).all()

    def test_all_published_empirical_curves_generate_monotone(self):
        """Every tabulated curve (both supplements) yields non-negative increments."""
        n = 0
        for trimmed in (True, False):
            for q in TexasStorm.EMPIRICAL_QUARTILES:
                for cls in TexasStorm.EMPIRICAL_DURATION_CLASSES:
                    dur = float(cls.split("-")[1])
                    for pct in TexasStorm.EMPIRICAL_PERCENTILES:
                        try:
                            h = TexasStorm.generate_hyetograph(
                                1.0,
                                dur,
                                "empirical",
                                quartile=q,
                                percentile=pct,
                                trimmed=trimmed,
                                duration_class=cls,
                                time_interval_min=int(dur * 60 / 24),
                                nonmonotone="running_max",
                            )
                        except ValueError as e:
                            assert "no data" in str(e)
                            continue
                        n += 1
                        assert (h["incremental_depth"] >= 0).all()
                        assert h["cumulative_depth"].iloc[-1] == pytest.approx(1.0)
        assert (
            n == 2 * 5 * 5 * 11 - 2 * 2
        )  # minus Q4 0-6 hr 10th/90th, both supplements

    def test_empirical_scaling_is_linear_in_depth(self):
        a = TexasStorm.generate_hyetograph(
            2.0, 24, "empirical", quartile=2, duration_class="12-24"
        )
        b = TexasStorm.generate_hyetograph(
            6.0, 24, "empirical", quartile=2, duration_class="12-24"
        )
        assert np.allclose(b["incremental_depth"], 3.0 * a["incremental_depth"])

    def test_empirical_matches_table_at_bin_centers(self):
        """At 2.5-percent bin centers the output equals the published ordinate."""
        # 40 intervals of 2.5 percent of 20 hr -> 30-min steps; bin center 2.5% = 0.5 hr
        h = TexasStorm.generate_hyetograph(
            100.0,
            20,
            "empirical",
            quartile=1,
            duration_class="12-24",
            time_interval_min=30,
        )
        curve = TexasStorm.get_empirical_curve(1, "12-24", 50)
        for pct in (2.5, 25.0, 50.0, 97.5):
            hr = pct / 100 * 20
            assert _depth_at(h, hr) == pytest.approx(
                curve.loc[curve["duration_pct"] == pct, "depth_pct"].iloc[0]
            )

    def test_matches_sibling_output_shape(self):
        sib = ScsTypeStorm.generate_hyetograph(5.0, "II", 60)
        tex = TexasStorm.generate_hyetograph(
            5.0, 24, "empirical", quartile=2, duration_class="12-24"
        )
        assert list(tex.columns) == list(sib.columns)
        assert len(tex) == len(sib)

    def test_provenance(self):
        h = TexasStorm.generate_hyetograph(
            5.0, 24, "empirical", quartile=2, duration_class="12-24"
        )
        prov = h.attrs["provenance"]
        assert prov["quartile"] == "2" and prov["trimmed"] is True
        assert "SIR 2004-5075" in prov["source"]


class TestSelectionAndApplicability:
    def test_unknown_method(self):
        with pytest.raises(ValueError, match="Invalid method"):
            TexasStorm.generate_hyetograph(5.0, 24, "scs")

    def test_empirical_requires_quartile(self):
        with pytest.raises(ValueError, match="quartile"):
            TexasStorm.generate_hyetograph(5.0, 10, "empirical")

    @pytest.mark.parametrize("dur", [6, 12, 24])
    def test_empirical_boundary_requires_explicit_class(self, dur):
        with pytest.raises(ValueError, match="boundary"):
            TexasStorm.generate_hyetograph(5.0, dur, "empirical", quartile=1)
        h = TexasStorm.generate_hyetograph(
            5.0,
            dur,
            "empirical",
            quartile=1,
            duration_class={6: "0-6", 12: "6-12", 24: "12-24"}[dur],
        )
        assert h["cumulative_depth"].iloc[-1] == pytest.approx(5.0)

    def test_class_must_contain_duration(self):
        with pytest.raises(ValueError, match="outside class"):
            TexasStorm.generate_hyetograph(
                5.0, 30, "empirical", quartile=1, duration_class="0-6"
            )

    def test_nws_gap_between_classes(self):
        with pytest.raises(ValueError, match="not covered"):
            TexasStorm.generate_hyetograph(
                5.0, 12.5, "triangular", param_set="nws_hourly", time_interval_min=30
            )

    def test_duration_limits_and_interval(self):
        with pytest.raises(ValueError):
            TexasStorm.generate_hyetograph(5.0, 96, "lgamma", duration_class="24-72")
        with pytest.raises(ValueError, match="evenly divide"):
            TexasStorm.generate_hyetograph(5.0, 10, "lgamma", time_interval_min=7)
        with pytest.raises(ValueError):
            TexasStorm.generate_hyetograph(0.0, 10, "lgamma")

    def test_area_limit_warns(self):
        kw = dict(quartile=1, duration_class="6-12")
        with pytest.warns(UserWarning, match="160"):
            TexasStorm.generate_hyetograph(
                5.0, 10, "empirical", drainage_area_sqmi=500.0, **kw
            )
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            TexasStorm.generate_hyetograph(
                5.0, 10, "empirical", drainage_area_sqmi=100.0, **kw
            )

    def test_small_depth_warns(self):
        with pytest.warns(UserWarning, match="1 inch"):
            TexasStorm.generate_hyetograph(0.5, 10, "lgamma")

    def test_nonmonotone_raises_by_default(self):
        # SIR 2004-5075 Supplement 5, p. 113, Q3, 12-24 hr, 40th percentile
        with pytest.raises(ValueError) as exc:
            TexasStorm.generate_hyetograph(
                5.0,
                18,
                "empirical",
                quartile=3,
                duration_class="12-24",
                percentile=40,
                time_interval_min=30,
            )
        msg = str(exc.value)
        assert "Supplement 5" in msg and "p. 113" in msg
        assert "nonmonotone='running_max'" in msg

    def test_nonmonotone_running_max_records_provenance(self):
        h = TexasStorm.generate_hyetograph(
            5.0,
            18,
            "empirical",
            quartile=3,
            duration_class="12-24",
            percentile=40,
            time_interval_min=30,
            nonmonotone="running_max",
        )
        prov = h.attrs["provenance"]
        assert prov["nonmonotone"] == "running_max"
        assert prov["monotone_points_adjusted"] > 0
        assert prov["max_monotone_adjustment_pct"] == pytest.approx(1.09)
        assert (h["incremental_depth"] >= 0).all()
        assert h["cumulative_depth"].iloc[-1] == pytest.approx(5.0)

    def test_monotone_column_unaffected_by_policy(self):
        kw = dict(
            quartile="all", duration_class="0-72", percentile=50, time_interval_min=60
        )
        a = TexasStorm.generate_hyetograph(5.0, 72, "empirical", **kw)
        b = TexasStorm.generate_hyetograph(
            5.0, 72, "empirical", nonmonotone="running_max", **kw
        )
        pd.testing.assert_frame_equal(a, b)
        assert b.attrs["provenance"]["monotone_points_adjusted"] == 0
        assert b.attrs["provenance"]["max_monotone_adjustment_pct"] == 0.0

    def test_invalid_nonmonotone_option(self):
        with pytest.raises(ValueError, match="nonmonotone"):
            TexasStorm.generate_hyetograph(
                5.0,
                24,
                "empirical",
                quartile=2,
                duration_class="12-24",
                nonmonotone="repair",
            )

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1.0, 0.0])
    def test_depth_and_duration_must_be_finite_positive(self, bad):
        with pytest.raises(ValueError, match="total_depth_inches"):
            TexasStorm.generate_hyetograph(bad, 10, "lgamma")
        with pytest.raises(ValueError, match="duration_hours"):
            TexasStorm.generate_hyetograph(5.0, bad, "lgamma")

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), -5.0, 0.0])
    def test_drainage_area_must_be_finite_positive(self, bad):
        with pytest.raises(ValueError, match="drainage_area_sqmi"):
            TexasStorm.generate_hyetograph(5.0, 10, "lgamma", drainage_area_sqmi=bad)

    @pytest.mark.parametrize(
        "name",
        [
            "total_depth_inches",
            "duration_hours",
            "drainage_area_sqmi",
            "time_interval_min",
        ],
    )
    @pytest.mark.parametrize("bad", [True, np.bool_(True)])
    def test_physical_quantities_reject_booleans(self, name, bad):
        kwargs = {
            "total_depth_inches": 5.0,
            "duration_hours": 10,
            "method": "lgamma",
            name: bad,
        }
        with pytest.raises(ValueError, match=name):
            TexasStorm.generate_hyetograph(**kwargs)

    @pytest.mark.parametrize("trimmed", [None, np.nan, "False", 1, np.int64(1)])
    def test_generate_rejects_nonboolean_trimmed_selector(self, trimmed):
        with pytest.raises(ValueError, match="trimmed"):
            TexasStorm.generate_hyetograph(5.0, 10, "lgamma", trimmed=trimmed)

    def test_applicability_warnings_point_to_caller(self):
        cases = (
            (
                dict(total_depth_inches=0.5, duration_hours=10, method="lgamma"),
                "1 inch",
            ),
            (
                dict(
                    total_depth_inches=5.0,
                    duration_hours=10,
                    method="empirical",
                    quartile=1,
                    duration_class="6-12",
                    drainage_area_sqmi=500.0,
                ),
                "160",
            ),
        )
        for kwargs, warning_text in cases:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                TexasStorm.generate_hyetograph(**kwargs)
            assert len(caught) == 1
            assert warning_text in str(caught[0].message)
            assert Path(caught[0].filename).resolve() == Path(__file__).resolve()

    @pytest.mark.parametrize("cls", [None, "5-12", "13-24"])
    def test_nws_gap_unsupported_even_with_explicit_class(self, cls):
        with pytest.raises(ValueError, match="unsupported|outside class"):
            TexasStorm.generate_hyetograph(
                5.0,
                12.5,
                "triangular",
                param_set="nws_hourly",
                duration_class=cls,
                time_interval_min=30,
            )

    def test_nws_gap_message_does_not_offer_override(self):
        with pytest.raises(ValueError) as exc:
            TexasStorm.generate_hyetograph(
                5.0, 24.5, "triangular", param_set="nws_hourly", time_interval_min=30
            )
        assert "does not override" in str(exc.value)

    def test_nws_six_hour_storm_uses_first_class(self):
        # 0-4194-4 Example 1 applies the 0-12 hr model (a = 0.02197) to 6 hr.
        h = TexasStorm.generate_hyetograph(
            10.0, 6, "triangular", param_set="nws_hourly", time_interval_min=60
        )
        assert h.attrs["provenance"]["duration_class"] == "5-12"
        assert h.attrs["provenance"]["a"] == 0.02197

    def test_nws_below_five_hours_rejected(self):
        with pytest.raises(ValueError, match="not covered"):
            TexasStorm.generate_hyetograph(5.0, 3, "triangular", param_set="nws_hourly")

    def test_area_warning_only_for_empirical(self):
        with pytest.warns(UserWarning, match="p. 4-77"):
            TexasStorm.generate_hyetograph(
                5.0,
                10,
                "empirical",
                quartile=1,
                duration_class="6-12",
                drainage_area_sqmi=500.0,
            )
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            TexasStorm.generate_hyetograph(
                5.0,
                24,
                "triangular",
                param_set="usgs_runoff",
                duration_class="0-24",
                drainage_area_sqmi=500.0,
            )
            TexasStorm.generate_hyetograph(5.0, 10, "lgamma", drainage_area_sqmi=500.0)
