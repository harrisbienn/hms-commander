"""Numerical method and refusal checks; real DSS reproduction is separate."""

import copy

import numpy as np
import pandas as pd
import pytest

from hms_commander import HmsBoundaryTransformer


@pytest.fixture
def curve():
    # Qout = storage / 2; hand-computable routing, not a basin calibration.
    return {
        "schema_version": 1,
        "method": "explicit-storage-polynomial-v1",
        "breakpoints": [0.0, 100.0],
        "coefficients": [[0.0], [0.0], [0.5], [0.0]],
        "storage_min": 0.0,
        "storage_max": 100.0,
        "initial_storage": 0.0,
        "initial_outflow": "curve-at-minimum-storage",
        "time_step_hours": 1.0,
        "storage_bounds_policy": "clip-and-report",
    }


def frame(values, freq="h", start="2017-01-01"):
    result = pd.DataFrame(
        {"value": values}, index=pd.date_range(start, periods=len(values), freq=freq)
    )
    result.attrs.update(units="CFS", type="INST-VAL")
    return result


def test_explicit_routing_and_input_preservation(curve):
    source = frame([10.0, 10.0, 10.0, 10.0])
    saved = source.copy(deep=True)
    result = HmsBoundaryTransformer.transform(source, curve)
    np.testing.assert_array_equal(result["value"], [0.0, 5.0, 7.5, 8.75])
    np.testing.assert_array_equal(result["storage"], [0.0, 10.0, 15.0, 17.5])
    assert result.attrs["clipping_count"] == 0
    assert result.attrs["engineering_accepted"] is False
    pd.testing.assert_frame_equal(source, saved)


def test_hourly_means_and_terminal_sample(curve):
    result = HmsBoundaryTransformer.transform(
        frame([2.0, 6.0, 10.0, 14.0, 20.0], "30min"), curve
    )
    # Hourly inputs 4, 12, 20. Last sample is retained as its own terminal bin.
    np.testing.assert_array_equal(result["value"], [0.0, 2.0, 7.0])
    assert len(result) == 3
    assert result.attrs["terminal_bin_sample_count"] == 1


def test_clip_diagnostics_preserve_the_specified_method(curve):
    result = HmsBoundaryTransformer.transform(frame([200.0, 200.0, 0.0]), curve)
    np.testing.assert_array_equal(result["value"], [0.0, 50.0, 50.0])
    assert result.attrs["upper_clip_count"] == 2
    assert result.attrs["storage_adjustment_sum"] == -250.0
    assert result.attrs["maximum_absolute_storage_adjustment"] == 150.0


def test_initial_storage_and_initial_outflow_are_distinct(curve):
    curve.update(
        breakpoints=[10.0, 100.0],
        storage_min=10.0,
        coefficients=[[0.0], [0.0], [0.5], [5.0]],
    )
    result = HmsBoundaryTransformer.transform(frame([0.0, 0.0, 0.0]), curve)
    np.testing.assert_array_equal(result["storage"], [0.0, 10.0, 10.0])
    np.testing.assert_array_equal(result["value"], [5.0, 5.0, 5.0])
    assert result.attrs["lower_clip_count"] == 2


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1.0, -3.4e38])
def test_reject_invalid_flow(curve, value):
    with pytest.raises(ValueError, match="flow"):
        HmsBoundaryTransformer.transform(frame([0.0, value, 0.0]), curve)


@pytest.mark.parametrize(
    "change",
    [
        {"storage_min": -1.0},
        {"time_step_hours": 0.5},
        {"initial_storage": float("nan")},
        {"coefficients": [[1.0]]},
        {"breakpoints": [0.0, 0.0]},
        {"unrecognized": 1},
        {"storage_bounds_policy": "extrapolate"},
        {"schema_version": True},
    ],
)
def test_reject_invalid_curve(curve, change):
    model = copy.deepcopy(curve)
    model.update(change)
    with pytest.raises(ValueError):
        HmsBoundaryTransformer.transform(frame([1.0, 1.0]), model)


def test_reject_timezone_offset_and_gaps(curve):
    source = frame([1.0, 1.0, 1.0])
    source.index = source.index.tz_localize("UTC")
    with pytest.raises(ValueError, match="naive"):
        HmsBoundaryTransformer.transform(source, curve)
    with pytest.raises(ValueError, match="hourly"):
        HmsBoundaryTransformer.transform(
            frame([1.0, 1.0], start="2017-01-01 00:05"), curve
        )
    source = frame([1.0, 1.0, 1.0, 1.0]).drop(pd.Timestamp("2017-01-01 01:00"))
    with pytest.raises(ValueError, match="interval"):
        HmsBoundaryTransformer.transform(source, curve)


def test_refuse_existing_output_before_native_io(tmp_path):
    output = tmp_path / "existing.dss"
    output.write_bytes(b"preserve")
    with pytest.raises(FileExistsError):
        HmsBoundaryTransformer.materialize(
            "missing.dss",
            "//FLOW/FLOW//1HOUR/RUN/",
            "missing.json",
            output,
            "//OUT/FLOW//1HOUR/RUN/",
            source_sha256="0" * 64,
            curve_sha256="0" * 64,
        )
    assert output.read_bytes() == b"preserve"


@pytest.mark.parametrize(
    "units,kind", [("CMS", "INST-VAL"), ("CFS", "PER-AVER"), ("", "")]
)
def test_reject_incompatible_metadata(curve, units, kind):
    source = frame([1.0, 1.0])
    source.attrs.update(units=units, type=kind)
    with pytest.raises(ValueError, match="CFS / INST-VAL"):
        HmsBoundaryTransformer.transform(source, curve)


def test_reject_negative_curve_output(curve):
    curve["coefficients"][3][0] = -1.0
    with pytest.raises(ValueError, match="negative flow"):
        HmsBoundaryTransformer.transform(frame([1.0, 1.0]), curve)


@pytest.mark.parametrize("interval", ["5MIN", "5Minute"])
def test_resolves_dss_interval_spelling_without_changing_run(interval):
    from hms_commander.HmsBoundaryTransformer import _resolve_source_selector

    catalog = [
        f"//J1/FLOW/{date}/{interval}/RUN:SOURCE/"
        for date in ["01JAN2017", "02JAN2017"]
    ]
    requested = "//J1/FLOW//5Minute/RUN:Source/"
    assert (
        _resolve_source_selector(catalog, requested)
        == f"//J1/FLOW//{interval.upper()}/RUN:SOURCE/"
    )
    for wrong in [
        requested.replace("Source", "Other"),
        requested.replace("5Minute", "15Minute"),
    ]:
        with pytest.raises(ValueError, match="absent"):
            _resolve_source_selector(catalog, wrong)


def test_rejects_two_physical_interval_families():
    from hms_commander.HmsBoundaryTransformer import _resolve_source_selector

    with pytest.raises(ValueError, match="ambiguous"):
        _resolve_source_selector(
            ["//J1/FLOW//5MIN/RUN:SOURCE/", "//J1/FLOW//5Minute/RUN:SOURCE/"],
            "//J1/FLOW//5Minute/RUN:SOURCE/",
        )
