"""
Tests for HmsArfTexas - Asquith (1999) USGS WRIR 99-4267 Texas 1-day ARF.

Published worked values reproduced here:
- WRIR 99-4267 Table 8 (p. 55): linear Austin watershed, ARF = 0.76
  (9.892 / 13), and 13-mi2 circular comparison, ARF = 0.83.
- WRIR 99-4267 p. 25 example: 12.57-mi2 (R = 2 mi) Austin watershed,
  ARF = 0.83, 8.3 in -> 6.9 in, 9.5 in -> 7.9 in.
- TxDOT HDM (09/2019) Sec. 13 p. 4-64/65: Dallas, 50.3 mi2 (R = 4 mi),
  ARF = 0.85, 9.55 in x 0.85 = 8.12 in.
"""

import logging
import math
import os
import re
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from hms_commander import HmsArfTexas
from hms_commander.HmsArfTexas import (
    _TABLE7,
    _DALLAS_24_27_CORRECTED,
    _DALLAS_24_27_PRINTED,
    MAX_AREA_MI2,
    STUDY_AREAS,
)

T = dict(recurrence_interval_yr=100)


# ---------------------------------------------------------------------------
# Published worked values
# ---------------------------------------------------------------------------


def test_table8_linear_austin_watershed():
    dist = [6, 5, 4, 3, 2, 1, 0, 1, 2, 3, 4, 5, 6]
    arf = HmsArfTexas.noncircular_arf("austin", dist, [1.0] * 13, **T)
    assert round(arf, 2) == 0.76
    assert arf == pytest.approx(9.892 / 13, abs=5e-4)


def test_table8_cell_values():
    printed = {6: 0.675, 5: 0.692, 4: 0.712, 3: 0.736, 2: 0.771, 1: 0.860, 0: 1.0}
    for r, s2 in printed.items():
        assert HmsArfTexas.depth_distance("austin", r) == pytest.approx(s2, abs=6e-4)
    cells = [6, 5, 4, 3, 2, 1, 0, 1, 2, 3, 4, 5, 6]
    assert sum(printed[r] for r in cells) == pytest.approx(9.892)


def test_table8_circular_comparison_13mi2():
    arf = HmsArfTexas.circular_arf("austin", 13.0, **T)
    assert HmsArfTexas.radius_from_area(13.0) == pytest.approx(2.03, abs=5e-3)
    assert round(arf, 2) == 0.83


def test_report_circular_example_austin_r2():
    # p. 25: integral evaluates to 0.5 * (0.453 + 1.216) = 0.8345 -> 0.83
    area = 12.57
    arf = HmsArfTexas.circular_arf("austin", area, **T)
    assert arf == pytest.approx(0.5 * (0.453 + 1.216), abs=1e-3)
    assert round(arf, 2) == 0.83
    assert round(8.3 * 0.83, 1) == 6.9 and round(9.5 * 0.83, 1) == 7.9
    assert HmsArfTexas.scale_depth(8.3, "austin", area, **T) == pytest.approx(
        6.9, abs=0.05
    )
    assert HmsArfTexas.scale_depth(9.5, "austin", area, **T) == pytest.approx(
        7.9, abs=0.05
    )
    assert 6.9 / 12 * area * 640 == pytest.approx(4626, abs=2)  # acre-ft, as printed


def test_hdm_dallas_example():
    # HDM p. 4-64/65: R = 4 mi (50.3 mi2); integral form gives 0.125*(1.84+4.99)
    arf = HmsArfTexas.circular_arf("dallas", 50.3, **T)
    assert arf == pytest.approx(0.125 * (1.84 + 4.99), abs=2e-3)
    assert round(arf, 2) == 0.85
    # R = 4 mi sits on a segment boundary; the equation printed in the HDM
    # (2 <= r <= 4) and the next one (4 <= r <= 6) both give 0.85
    r = 4.0
    hdm_eq = 0.9670 - 0.0290 * r + 0.0440 / r**2
    assert hdm_eq == pytest.approx(0.854, abs=5e-4)
    assert HmsArfTexas.circular_arf("dallas", radius_mi=4.0, **T) == pytest.approx(
        hdm_eq, abs=1e-3
    )
    assert 9.55 * round(arf, 2) == pytest.approx(8.12, abs=5e-3)


# ---------------------------------------------------------------------------
# Transcription QA
# ---------------------------------------------------------------------------


def _numeric_arf(city, r, n=20001):
    rs = np.linspace(0, r, n)
    s = np.array([HmsArfTexas.depth_distance(city, x) for x in rs])
    return (
        2 * np.trapezoid(rs * s, rs) / r**2
        if hasattr(np, "trapezoid")
        else 2 * np.trapz(rs * s, rs) / r**2
    )


@pytest.mark.parametrize("city", STUDY_AREAS)
def test_closed_form_matches_integral_of_s2(city):
    # ARF2 in Table 7 is the closed-form integral of S2 (eq. 15); published
    # coefficients are rounded, so allow a few thousandths. The printed Dallas
    # 24-27 intercept does not satisfy this; use the correction here.
    for r in (0.5, 1.5, 2.5, 4, 7, 10, 15, 22, 25, 30, 40, 50):
        arf = HmsArfTexas.circular_arf(
            city, radius_mi=r, **T, dallas_intercept_correction=True
        )
        assert arf == pytest.approx(_numeric_arf(city, r), abs=3e-3), (city, r)


@pytest.mark.parametrize("city", STUDY_AREAS)
def test_segments_are_nearly_continuous(city):
    for seg in _TABLE7[city][1:]:
        r = seg[0]
        below = HmsArfTexas.circular_arf(
            city, radius_mi=r - 1e-9, **T, dallas_intercept_correction=True
        )
        at = HmsArfTexas.circular_arf(
            city, radius_mi=r, **T, dallas_intercept_correction=True
        )
        assert abs(below - at) < 2.5e-3, (city, r)  # rounding of published constants


def test_dallas_24_27_default_is_printed_value():
    # Table 7 prints 0.6800 in the ARF column but 0.6880 in the S2 column.
    # The default reproduces the source; the correction is opt-in.
    r = 24.0
    default = HmsArfTexas.circular_arf("dallas", radius_mi=r, **T)
    corrected = HmsArfTexas.circular_arf(
        "dallas", radius_mi=r, **T, dallas_intercept_correction=True
    )
    below = HmsArfTexas.circular_arf("dallas", radius_mi=r - 1e-9, **T)
    assert default == pytest.approx(0.6800 - 0.0058 * r + 17.9533 / r**2)
    assert corrected - default == pytest.approx(
        _DALLAS_24_27_CORRECTED - _DALLAS_24_27_PRINTED
    )
    assert abs(corrected - below) < 5e-4  # corrected is continuous
    assert abs(default - below) > 7e-3  # printed value is not
    assert abs(default - _numeric_arf("dallas", r)) > 7e-3
    # other segments and cities unaffected by the flag
    for city, rr in (("dallas", 10), ("dallas", 30), ("austin", 25), ("houston", 25)):
        assert HmsArfTexas.circular_arf(
            city, radius_mi=rr, **T, dallas_intercept_correction=True
        ) == HmsArfTexas.circular_arf(city, radius_mi=rr, **T)


def test_dallas_correction_forwarded_by_scale_helpers():
    kw = dict(radius_mi=25.0, **T)
    printed = HmsArfTexas.circular_arf("dallas", **kw)
    fixed = HmsArfTexas.circular_arf("dallas", **kw, dallas_intercept_correction=True)
    assert fixed != printed
    assert HmsArfTexas.scale_depth(10.0, "dallas", **kw) == pytest.approx(
        10.0 * printed
    )
    assert HmsArfTexas.scale_depth(
        10.0, "dallas", **kw, dallas_intercept_correction=True
    ) == pytest.approx(10.0 * fixed)
    inc = [1.0, 2.0, 3.0]
    assert HmsArfTexas.scale_hyetograph(inc, "dallas", **kw) == pytest.approx(
        np.array(inc) * printed
    )
    assert HmsArfTexas.scale_hyetograph(
        inc, "dallas", **kw, dallas_intercept_correction=True
    ) == pytest.approx(np.array(inc) * fixed)
    s = pd.Series(inc)
    assert HmsArfTexas.scale_hyetograph(
        s, "dallas", **kw, dallas_intercept_correction=True
    ).sum() == pytest.approx(6.0 * fixed)


def test_extrapolated_nonphysical_arf_raises():
    # Houston R = 200 mi continues the last segment to a negative ARF.
    with pytest.raises(ValueError, match=r"outside \(0, 1\]"):
        HmsArfTexas.circular_arf("houston", radius_mi=200, extrapolate=True, **T)
    with pytest.raises(ValueError):
        HmsArfTexas.scale_depth(10.0, "houston", radius_mi=200, extrapolate=True, **T)
    with pytest.raises(ValueError):
        HmsArfTexas.scale_hyetograph(
            [1.0, 2.0], "houston", radius_mi=200, extrapolate=True, **T
        )
    # a modest extrapolation that stays physical is still allowed
    assert (
        0
        < HmsArfTexas.circular_arf("houston", radius_mi=55, extrapolate=True, **T)
        <= 1
    )


@pytest.mark.parametrize("city", STUDY_AREAS)
@pytest.mark.parametrize("corrected", [False, True])
def test_arf_in_unit_interval_over_published_domain(city, corrected):
    for r in np.linspace(0.0, 50.0, 501):
        arf = HmsArfTexas.circular_arf(
            city, radius_mi=float(r), **T, dallas_intercept_correction=corrected
        )
        assert 0 < arf <= 1, (city, r)


_PDF = Path(
    os.environ.get(
        "WRIR_99_4267_PDF",
        r"H:\26-014 CWE\08 Upper Guadalupe 12100201\01 Research\sources\wri99-4267.pdf",
    )
)


@pytest.mark.skipif(not _PDF.exists(), reason="WRIR 99-4267 PDF not available")
def test_coefficients_match_pdf_text_layer():
    pymupdf = pytest.importorskip("pymupdf")
    text = pymupdf.open(_PDF)[58].get_text()  # Table 7, report p. 54
    assert "Table 7" in text
    text = re.sub(r"[\ufffd\u2212\u2013]", "-", text)
    parsed, city = {}, None
    for ln in (x.strip() for x in text.split("\n")):
        if ln in ("Austin", "Dallas", "Houston"):
            city = ln.lower()
            parsed.setdefault(city, {"S": [], "A": [], "L": []})
        if not city:
            continue
        if m := re.match(r"S2\(r\) = ([\d.]+) . ([\d.]+)\(r\)$", ln):
            parsed[city]["S"].append((float(m[1]), float(m[2])))
        elif m := re.match(
            r"ARF2\(r\) = ([\d.]+) . ([\d.]+)\(r\)\s*(?:\+ \(([\d.]+) / r2\))?$", ln
        ):
            parsed[city]["A"].append((float(m[1]), float(m[2]), float(m[3] or 0)))
        elif m := re.match(r"([\d.]+) < r < ([\d.]+)$", ln):
            parsed[city]["L"].append((float(m[1]), float(m[2])))
    for c, segs in _TABLE7.items():
        p = parsed[c]
        assert len(p["S"]) == len(p["A"]) == len(p["L"]) == len(segs)
        for seg, s, a, lim in zip(segs, p["S"], p["A"], p["L"]):
            assert ((seg[0], seg[1]), (seg[2], seg[3]), (seg[4], seg[5], seg[6])) == (
                lim,
                s,
                a,
            )


# ---------------------------------------------------------------------------
# Behaviour: monotonicity, boundaries, scope
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("city", STUDY_AREAS)
def test_arf_decreases_with_area(city):
    areas = np.geomspace(0.01, MAX_AREA_MI2, 400)
    arf = np.array(
        [
            HmsArfTexas.circular_arf(city, a, **T, dallas_intercept_correction=True)
            for a in areas
        ]
    )
    assert np.all((arf > 0) & (arf <= 1))
    # Published segments are fit independently and rounded, so a step up of
    # up to ~2.5e-3 can occur at a breakpoint; the trend is otherwise strict.
    assert np.all(np.diff(arf) < 2.5e-3)
    assert arf[0] > 0.99 and arf[-1] < arf[0] - 0.4
    coarse = arf[::25]
    assert np.all(np.diff(coarse) < 0)


@pytest.mark.parametrize("city", STUDY_AREAS)
def test_boundaries(city):
    assert HmsArfTexas.circular_arf(city, 0.0, **T) == 1.0
    assert HmsArfTexas.depth_distance(city, 0) == 1.0
    top = HmsArfTexas.circular_arf(city, radius_mi=50, **T)
    assert 0.3 < top < 0.6
    assert HmsArfTexas.circular_arf(city, MAX_AREA_MI2, **T) == pytest.approx(
        top, abs=1e-9
    )
    with pytest.raises(ValueError, match="50"):
        HmsArfTexas.circular_arf(city, radius_mi=50.01, **T)
    with pytest.raises(ValueError):
        HmsArfTexas.circular_arf(city, MAX_AREA_MI2 * 1.001, **T)
    with pytest.raises(ValueError):
        HmsArfTexas.circular_arf(city, radius_mi=-1, **T)
    with pytest.raises(ValueError):
        HmsArfTexas.circular_arf(city, float("nan"), **T)


def test_recurrence_interval_range():
    assert HmsArfTexas.circular_arf(
        "austin", 100, recurrence_interval_yr=2
    ) == HmsArfTexas.circular_arf("austin", 100, recurrence_interval_yr=500)
    with pytest.raises(ValueError, match="2-yr"):
        HmsArfTexas.circular_arf("austin", 100, recurrence_interval_yr=1.99)
    with pytest.raises(ValueError):
        HmsArfTexas.circular_arf("austin", 100, recurrence_interval_yr=0)
    with pytest.raises(TypeError):  # required, no silent default
        HmsArfTexas.circular_arf("austin", 100)


def test_duration_and_study_area_scope():
    with pytest.raises(ValueError, match="1-day"):
        HmsArfTexas.circular_arf("austin", 100, duration_hr=6, **T)
    assert HmsArfTexas.circular_arf("austin", 100, duration_hr=24, **T) > 0
    for bad in ("san antonio", "", "Kerrville"):
        with pytest.raises(ValueError, match="study area"):
            HmsArfTexas.circular_arf(bad, 100, **T)
        with pytest.raises(ValueError, match="study area"):  # not bypassable
            HmsArfTexas.circular_arf(bad, 100, extrapolate=True, **T)
    assert HmsArfTexas.circular_arf(" Dallas ", 100, **T) == HmsArfTexas.circular_arf(
        "dallas", 100, **T
    )


def test_extrapolate_opt_in_warns(caplog):
    with caplog.at_level(logging.WARNING):
        caplog.clear()
        v = HmsArfTexas.circular_arf("houston", radius_mi=60, extrapolate=True, **T)
        assert 0 < v < HmsArfTexas.circular_arf("houston", radius_mi=50, **T)
        assert any(
            "Extrapolating" in r.message and "50" in r.message for r in caplog.records
        )
        caplog.clear()
        HmsArfTexas.circular_arf("houston", 100, **T)
        assert not caplog.records


def test_extrapolate_never_relaxes_duration_or_recurrence():
    with pytest.raises(ValueError, match="1-day"):
        HmsArfTexas.circular_arf(
            "austin", 100, duration_hr=6, recurrence_interval_yr=100, extrapolate=True
        )
    with pytest.raises(ValueError, match="minimum"):
        HmsArfTexas.circular_arf(
            "austin", 100, duration_hr=24, recurrence_interval_yr=1, extrapolate=True
        )
    with pytest.raises(ValueError, match="1-day"):
        HmsArfTexas.scale_depth(
            10.0,
            "austin",
            100,
            duration_hr=6,
            recurrence_interval_yr=100,
            extrapolate=True,
        )
    with pytest.raises(ValueError, match="minimum"):
        HmsArfTexas.noncircular_arf(
            "austin", [1.0], [1.0], recurrence_interval_yr=1, extrapolate=True
        )


def test_size_argument_validation():
    with pytest.raises(ValueError, match="exactly one"):
        HmsArfTexas.circular_arf("austin", **T)
    with pytest.raises(ValueError, match="exactly one"):
        HmsArfTexas.circular_arf("austin", 10, radius_mi=2, **T)
    assert HmsArfTexas.circular_arf("austin", math.pi * 4, **T) == pytest.approx(
        HmsArfTexas.circular_arf("austin", radius_mi=2, **T)
    )
    for bad in (-1.0, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            HmsArfTexas.circular_arf("austin", area_mi2=bad, **T)
        with pytest.raises(ValueError):
            HmsArfTexas.radius_from_area(bad)
    for bad in (-1.0, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            HmsArfTexas.circular_arf("austin", radius_mi=bad, **T)


@pytest.mark.parametrize(
    "bad",
    [-(10**400), 10**400, -(10**20)],
    ids=["negative_overflow", "positive_overflow", "negative_large"],
)
def test_huge_integer_radius_and_area_raise_value_error(bad):
    for kwargs in ({"area_mi2": bad}, {"radius_mi": bad}):
        with pytest.raises(ValueError, match="float64|finite"):
            HmsArfTexas.circular_arf("austin", **kwargs, **T)


@pytest.mark.parametrize(
    "bad",
    [-(10**400), 10**400],
    ids=["negative_overflow", "positive_overflow"],
)
def test_huge_integer_depths_raise_value_error(bad):
    for method in (HmsArfTexas.scale_depth, HmsArfTexas.scale_hyetograph):
        with pytest.raises(ValueError, match="float64|finite"):
            method(bad, "austin", radius_mi=1, **T)


@pytest.mark.parametrize("bad", [True, "1"])
@pytest.mark.parametrize(
    "numeric_input",
    [
        lambda bad: HmsArfTexas.radius_from_area(bad),
        lambda bad: HmsArfTexas.depth_distance("austin", bad),
        lambda bad: HmsArfTexas.circular_arf("austin", area_mi2=bad, **T),
        lambda bad: HmsArfTexas.circular_arf("austin", radius_mi=bad, **T),
        lambda bad: HmsArfTexas.noncircular_arf("austin", [bad], [1], **T),
        lambda bad: HmsArfTexas.noncircular_arf("austin", [1], [bad], **T),
        lambda bad: HmsArfTexas.scale_depth(bad, "austin", radius_mi=1, **T),
        lambda bad: HmsArfTexas.circular_arf("austin", 1, recurrence_interval_yr=bad),
        lambda bad: HmsArfTexas.circular_arf("austin", 1, duration_hr=bad, **T),
    ],
    ids=[
        "area_radius",
        "distance",
        "circular_area",
        "circular_radius",
        "cell_distance",
        "cell_area",
        "depth",
        "recurrence",
        "duration",
    ],
)
def test_bool_and_string_numeric_inputs_raise_value_error(numeric_input, bad):
    with pytest.raises(ValueError, match="float64"):
        numeric_input(bad)


def test_noncircular_validation():
    with pytest.raises(ValueError):
        HmsArfTexas.noncircular_arf("austin", [1, 2], [1.0], **T)
    with pytest.raises(ValueError):
        HmsArfTexas.noncircular_arf("austin", [1, 2], [1.0, 0.0], **T)
    with pytest.raises(ValueError):
        HmsArfTexas.noncircular_arf("austin", [1, 51], [1.0, 1.0], **T)
    # a single cell at the centroid has no reduction
    assert HmsArfTexas.noncircular_arf("dallas", [0], [3.0], **T) == 1.0


def test_noncircular_arf_normalizes_large_cell_areas():
    arf = HmsArfTexas.noncircular_arf("houston", [1, 1], [1e308, 1e308], **T)
    assert arf == pytest.approx(
        HmsArfTexas.noncircular_arf("houston", [1, 1], [1, 1], **T)
    )
    assert arf == pytest.approx(0.88)


# ---------------------------------------------------------------------------
# Convenience scaling
# ---------------------------------------------------------------------------


def test_scale_hyetograph_types():
    arf = HmsArfTexas.circular_arf("dallas", 50.3, **T)
    inc = [0.1, 0.5, 2.0, 0.9, 0.2]
    out = HmsArfTexas.scale_hyetograph(inc, "dallas", 50.3, **T)
    assert isinstance(out, np.ndarray)
    assert out == pytest.approx(np.array(inc) * arf)
    assert out.sum() == pytest.approx(sum(inc) * arf)
    s = pd.Series(inc, index=pd.date_range("2020-01-01", periods=5, freq="h"))
    out_s = HmsArfTexas.scale_hyetograph(s, "dallas", 50.3, **T)
    assert isinstance(out_s, pd.Series) and out_s.index.equals(s.index)
    assert out_s.sum() == pytest.approx(s.sum() * arf)
    assert HmsArfTexas.scale_depth(9.55, "dallas", 50.3, **T) == pytest.approx(
        9.55 * arf
    )
    with pytest.raises(ValueError):
        HmsArfTexas.scale_hyetograph(inc, "dallas", 50.3, duration_hr=6, **T)


@pytest.mark.parametrize(
    "depths",
    [
        -1.0,
        float("nan"),
        float("inf"),
        [1.0, -1.0],
        np.array([1.0, np.nan]),
        pd.Series([1.0, np.inf]),
        pd.DataFrame({"depth": [1.0, -1.0]}),
    ],
)
def test_scale_helpers_reject_nonfinite_or_negative_depths(depths):
    for method in (HmsArfTexas.scale_depth, HmsArfTexas.scale_hyetograph):
        with pytest.raises(ValueError, match="finite numbers >= 0"):
            method(depths, "houston", radius_mi=1, **T)


@pytest.mark.parametrize("radius_mi", [1e155, 1e308])
@pytest.mark.parametrize("extrapolate", [False, True])
def test_huge_finite_radii_raise_value_error(radius_mi, extrapolate):
    kwargs = dict(radius_mi=radius_mi, extrapolate=extrapolate, **T)
    for method, depths in (
        (HmsArfTexas.circular_arf, None),
        (HmsArfTexas.scale_depth, 1.0),
        (HmsArfTexas.scale_hyetograph, [1.0]),
    ):
        with pytest.raises(ValueError):
            if depths is None:
                method("houston", **kwargs)
            else:
                method(depths, "houston", **kwargs)


def test_existing_hmsarf_untouched():
    from hms_commander import HmsArf

    assert hasattr(HmsArf, "apply_arf") and not hasattr(HmsArf, "circular_arf")


def test_extrapolated_nonphysical_cell_and_depth_distance_raise():
    with pytest.raises(ValueError):
        HmsArfTexas.noncircular_arf(
            "houston",
            [200, 200],
            [1, 1],
            recurrence_interval_yr=100,
            extrapolate=True,
        )
    with pytest.raises(ValueError):
        HmsArfTexas.noncircular_arf(
            "houston",
            [0, 150],
            [1, 1],
            recurrence_interval_yr=100,
            extrapolate=True,
        )
    with pytest.raises(ValueError):
        HmsArfTexas.depth_distance("houston", 200, extrapolate=True)


@pytest.mark.parametrize("city", STUDY_AREAS)
@pytest.mark.parametrize("r", [0, 1e-200, 1e-12])
def test_radius_zero_limit(city, r):
    assert HmsArfTexas.circular_arf(
        city, radius_mi=r, recurrence_interval_yr=100
    ) == pytest.approx(1.0)
