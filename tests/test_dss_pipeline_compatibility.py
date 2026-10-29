"""Compatibility between the fork's DSS writer and upstream time/release APIs."""

from datetime import datetime, timedelta
from types import SimpleNamespace
import json
import sys

import pandas as pd
import pytest

from hms_commander import DssCore, HmsResultsProducts


@pytest.mark.parametrize("year", [1899, 2016, 2263, 3000])
@pytest.mark.parametrize("granularity", [1, 60])
@pytest.mark.parametrize("base_days", [0, 40000])
def test_container_dates_preserve_range_granularity_and_base(
    year, granularity, base_days
):
    expected = datetime(year, 1, 2, 0, 5)
    base = datetime(1899, 12, 31) + timedelta(days=base_days)
    count = int((expected - base).total_seconds()) // granularity
    stamps = DssCore._hec_times_to_datetimes([count, count + 1], granularity, base_days)
    assert stamps[0].to_pydatetime() == expected
    assert (stamps[1] - stamps[0]).total_seconds() == granularity
    assert DssCore._hec_time_to_datetime(count, granularity, base_days) == stamps[0]


def test_cleanup_preserves_active_error_and_still_releases(monkeypatch):
    released = []

    def fail_done():
        raise RuntimeError("cleanup failed")

    monkeypatch.setattr(DssCore, "_release_dss_file", released.append)
    with pytest.raises(ValueError, match="original read failure"):
        try:
            raise ValueError("original read failure")
        finally:
            DssCore._done_and_release(SimpleNamespace(done=fail_done), "exact.dss")
    assert released == ["exact.dss"]


@pytest.mark.parametrize("write_fails", [False, True])
def test_writer_releases_file_and_preserves_failure(monkeypatch, tmp_path, write_fails):
    released = []
    original = ValueError("write failed")

    def put(container):
        if write_fails:
            raise original

    def done():
        if write_fails:
            raise OSError("cleanup failed")

    handle = SimpleNamespace(put=put, done=done)
    classes = {
        "hec.heclib.dss.HecDss": SimpleNamespace(open=lambda path: handle),
        "hec.io.TimeSeriesContainer": lambda: SimpleNamespace(
            setStoreAsDoubles=lambda flag: None,
            setTimes=lambda times: 0,
            setValues=lambda values: 0,
        ),
        "hec.heclib.util.HecTimeArray": lambda times: times,
    }
    monkeypatch.setitem(
        sys.modules,
        "jnius",
        SimpleNamespace(
            autoclass=classes.__getitem__,
            cast=lambda name, obj: obj,
        ),
    )
    monkeypatch.setattr(DssCore, "_configure_jvm", lambda: None)
    monkeypatch.setattr(DssCore, "_release_dss_file", released.append)
    destination = tmp_path / "writer.dss"

    def write():
        DssCore.write_timeseries(
            destination,
            "/SYNTH/OUTLET/FLOW//5MIN/TEST/",
            pd.date_range("3000-01-02", periods=3, freq="5min", unit="s"),
            [1.0, 3.0, 2.0],
            units="CFS",
            data_type="INST-VAL",
        )

    if write_fails:
        with pytest.raises(RuntimeError, match="Failed to write time series") as error:
            write()
        assert error.value.__cause__ is original
    else:
        write()
    assert released == [str(destination.resolve())]


@pytest.mark.requires_java
@pytest.mark.parametrize("year", [1899, 2016, 2263, 3000])
@pytest.mark.parametrize("units", ["CFS", "CMS"])
def test_native_writer_window_peak_and_release(tmp_path, year, units):
    """Use only synthetic temporary DSS records; no HMS/RAS solver execution."""
    if not DssCore.is_available():
        pytest.skip("pyjnius is unavailable")
    DssCore._configure_jvm()
    from jnius import autoclass

    destination = tmp_path / "writer.dss"
    pathname = "/SYNTH/OUTLET/FLOW//5MIN/TEST/"
    stamps = pd.date_range(f"{year}-01-02", periods=3, freq="5min", unit="s")
    DssCore.write_timeseries(
        destination,
        pathname,
        stamps,
        [1.0, 3.0, 2.0],
        units=units,
        data_type="INST-VAL",
    )
    renamed = destination.with_suffix(".released")
    destination.rename(renamed)
    renamed.rename(destination)
    frame = DssCore.read_timeseries(
        destination, pathname, str(stamps[1]), str(stamps[2])
    )
    assert frame.index.tolist() == stamps[1:].tolist()
    assert frame["value"].tolist() == [3.0, 2.0]
    assert frame.attrs["interval"] == 5
    assert frame.attrs["units"] == units
    assert DssCore.get_peak_value(destination, pathname)["peak_time"] == stamps[1]
    # Release must leave the caller's JVM usable for subsequent operations.
    assert autoclass("java.lang.System").currentTimeMillis() > 0
    destination.rename(renamed)
    renamed.rename(destination)
    products = tmp_path / "products"
    HmsResultsProducts.export(
        destination,
        [{"mapping_id": "outlet", "pathname": pathname}],
        products,
    )
    summary = json.loads((products / "hydrologic-qualification.json").read_text())
    assert summary["pathnames"][0]["units"] == units
    assert summary["pathnames"][0]["peak_value"] == 3.0
