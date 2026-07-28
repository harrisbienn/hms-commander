"""Tests for deterministic HMS hydrologic product contracts."""

import json

import pandas as pd
import pytest

from hms_commander import DssCore, HmsResultsProducts


FLOW_PATH = "//J1/FLOW//5Minute/RUN:TEST/"


def _frame(
    *,
    values=(1.0, 3.0, 2.0),
    times=None,
) -> pd.DataFrame:
    if times is None:
        times = pd.date_range("2019-09-18T13:00:00", periods=3, freq="5min")
    result = pd.DataFrame({"value": values}, index=pd.DatetimeIndex(times))
    result.attrs.update(
        {
            "pathname": FLOW_PATH,
            "units": "CFS",
            "type": "INST-VAL",
            "interval": 5,
        }
    )
    return result


def _install_dss_fakes(monkeypatch, frame):
    monkeypatch.setattr(
        DssCore,
        "get_catalog",
        staticmethod(
            lambda _path: [
                "//S1/PRECIP-EXCESS/18Sep2019/5Minute/RUN:TEST/",
                "//S1/PRECIP-EXCESS/19Sep2019/5Minute/RUN:TEST/",
                FLOW_PATH,
            ]
        ),
    )
    monkeypatch.setattr(
        DssCore,
        "read_timeseries",
        staticmethod(lambda _path, _pathname: frame.copy()),
    )


def test_export_is_deterministic_and_preserves_duplicate_path_mappings(
    monkeypatch,
    tmp_path,
):
    _install_dss_fakes(monkeypatch, _frame())
    source = tmp_path / "results.dss"
    source.write_bytes(b"test-dss")
    mappings = [
        {"mapping_id": "m002", "pathname": FLOW_PATH, "bc_line": "B"},
        {"mapping_id": "m001", "pathname": FLOW_PATH, "bc_line": "A"},
    ]

    first = HmsResultsProducts.export(source, mappings, tmp_path / "first")
    second = HmsResultsProducts.export(source, mappings, tmp_path / "second")

    assert first == second
    assert first["schema"] == HmsResultsProducts.SCHEMA
    assert list(first["assets"]) == [
        "hydrologic-hydrographs",
        "hydrologic-qualification",
    ]
    for key in first["assets"]:
        assert (
            first["assets"][key]["sha256"]
            == second["assets"][key]["sha256"]
        )
    table = pd.read_csv(tmp_path / "first" / "hydrologic-hydrographs.csv")
    assert len(table) == 6
    assert table["mapping_id"].unique().tolist() == ["m001", "m002"]

    qualification = json.loads(
        (tmp_path / "first" / "hydrologic-qualification.json").read_text()
    )
    assert qualification["required_pathname_count"] == 2
    assert qualification["unique_pathname_count"] == 1
    assert qualification["all_required_pathnames_valid"] is True
    assert qualification["all_required_pathnames_enter_recession"] is True
    assert qualification["precipitation_excess"] == {
        "qualified": True,
        "pathname_count": 2,
        "pathnames": [
            "//S1/PRECIP-EXCESS/18Sep2019/5Minute/RUN:TEST/",
            "//S1/PRECIP-EXCESS/19Sep2019/5Minute/RUN:TEST/",
        ],
        "elements": ["S1"],
        "intervals": ["5Minute"],
        "runs": ["TEST"],
        "date_blocks": ["18Sep2019", "19Sep2019"],
    }


def test_export_reports_missing_sentinel_and_negative_values(
    monkeypatch,
    tmp_path,
):
    _install_dss_fakes(
        monkeypatch,
        _frame(values=(float("nan"), -3.0e38, -2.0)),
    )
    source = tmp_path / "results.dss"
    source.write_bytes(b"test-dss")

    manifest = HmsResultsProducts.export(
        source,
        [{"mapping_id": "m001", "pathname": FLOW_PATH}],
        tmp_path / "products",
    )
    qualification = json.loads(
        (tmp_path / "products" / "hydrologic-qualification.json").read_text()
    )
    summary = qualification["pathnames"][0]

    assert manifest["status"]["all_required_pathnames_valid"] is False
    assert summary["missing_count"] == 1
    assert summary["sentinel_count"] == 1
    assert summary["missing_or_sentinel_count"] == 2
    assert summary["negative_count"] == 1
    assert summary["qualified"] is False


def test_export_rejects_duplicate_mapping_ids(monkeypatch, tmp_path):
    _install_dss_fakes(monkeypatch, _frame())
    source = tmp_path / "results.dss"
    source.write_bytes(b"test-dss")

    with pytest.raises(ValueError, match="mapping_id values must be unique"):
        HmsResultsProducts.export(
            source,
            [
                {"mapping_id": "same", "pathname": FLOW_PATH},
                {"mapping_id": "SAME", "pathname": FLOW_PATH},
            ],
            tmp_path / "products",
        )


def test_export_rejects_non_increasing_time_axis(monkeypatch, tmp_path):
    times = pd.to_datetime(
        [
            "2019-09-18T13:00:00",
            "2019-09-18T13:05:00",
            "2019-09-18T13:05:00",
        ]
    )
    _install_dss_fakes(monkeypatch, _frame(times=times))
    source = tmp_path / "results.dss"
    source.write_bytes(b"test-dss")

    with pytest.raises(ValueError, match="not strictly increasing"):
        HmsResultsProducts.export(
            source,
            [{"mapping_id": "m001", "pathname": FLOW_PATH}],
            tmp_path / "products",
        )
