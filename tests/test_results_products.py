"""Tests for deterministic HMS hydrologic product contracts."""

import hashlib
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

from hms_commander import DssCore, HmsResultsProducts

FLOW_PATH = "//J1/FLOW//5Minute/RUN:TEST/"
LEFT_PATH = "//J1/FLOW//5Minute/HANDOFF:LEFT/"
RIGHT_PATH = "//J1/FLOW//5Minute/HANDOFF:RIGHT/"
STATIC_PATH = "/STATIC/UPSTREAM/FLOW//5Minute/APPROVED/"


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


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _install_handoff_dss_fakes(monkeypatch, source_frames):
    written = {}

    def read_timeseries(dss_file, pathname):
        key = (Path(dss_file).resolve(), pathname)
        frame = written.get(key)
        if frame is None:
            frame = source_frames.get(key)
        if frame is None:
            raise ValueError(f"No fake DSS record for {key}")
        return frame.copy()

    def write_timeseries(
        dss_file,
        pathname,
        times,
        values,
        *,
        units,
        data_type,
        interval_minutes,
        create_if_missing=True,
    ):
        del create_if_missing
        destination = Path(dss_file).resolve()
        frame = pd.DataFrame({"value": values}, index=pd.DatetimeIndex(times))
        frame.attrs.update(
            {
                "pathname": pathname,
                "units": units,
                "type": data_type,
                "interval": interval_minutes,
            }
        )
        written[(destination, pathname)] = frame
        records = [
            f"{record_pathname}:"
            + ",".join(format(value, ".17g") for value in record["value"])
            for (record_file, record_pathname), record in sorted(
                written.items(), key=lambda item: item[0][1].casefold()
            )
            if record_file == destination
        ]
        destination.write_text("\n".join(records) + "\n", encoding="utf-8")

    def get_catalog(dss_file):
        source = Path(dss_file).resolve()
        return [pathname for (path, pathname) in written if path == source]

    monkeypatch.setattr(DssCore, "read_timeseries", staticmethod(read_timeseries))
    monkeypatch.setattr(DssCore, "write_timeseries", staticmethod(write_timeseries))
    monkeypatch.setattr(DssCore, "get_catalog", staticmethod(get_catalog))

    def write_in_process(mappings, output, *, start, end):
        HmsResultsProducts._write_handoff_in_process(
            mappings,
            output,
            start=start,
            end=end,
        )

    monkeypatch.setattr(
        HmsResultsProducts,
        "_write_handoff_subprocess",
        staticmethod(write_in_process),
    )

    def export_in_process(
        source,
        required_pathnames,
        output,
        *,
        sentinel_threshold,
        maximum_final_to_peak_ratio,
        minimum_post_peak_hours,
    ):
        HmsResultsProducts.export(
            source,
            required_pathnames,
            output,
            sentinel_threshold=sentinel_threshold,
            maximum_final_to_peak_ratio=maximum_final_to_peak_ratio,
            minimum_post_peak_hours=minimum_post_peak_hours,
        )

    monkeypatch.setattr(
        HmsResultsProducts,
        "_export_handoff_subprocess",
        staticmethod(export_in_process),
    )
    return written


def _handoff_mapping(
    source: Path,
    *,
    mapping_id: str,
    source_pathname: str,
    output_pathname: str,
    multiplier: float = 1.0,
    conversion: str = "identity",
):
    return {
        "mapping_id": mapping_id,
        "source_asset_id": f"source-{mapping_id}",
        "source_dss": str(source),
        "source_sha256": _sha256(source),
        "source_pathname": source_pathname,
        "output_pathname": output_pathname,
        "source_units": "CFS",
        "target_units": "CFS",
        "value_type": "INST-VAL",
        "interval_minutes": 5,
        "conversion": conversion,
        "multiplier": multiplier,
        "offset": 0.0,
    }


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
        assert first["assets"][key]["sha256"] == second["assets"][key]["sha256"]
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


def test_materialize_handoff_combines_and_transforms_authenticated_sources(
    monkeypatch,
    tmp_path,
):
    hms_source = tmp_path / "hms-output.dss"
    static_source = tmp_path / "static-input.dss"
    hms_source.write_bytes(b"hms-source")
    static_source.write_bytes(b"static-source")
    static_frame = _frame(values=(4.0, 5.0, 6.0))
    static_frame.attrs["pathname"] = STATIC_PATH
    source_frames = {
        (hms_source.resolve(), FLOW_PATH): _frame(values=(10.0, 20.0, 30.0)),
        (static_source.resolve(), STATIC_PATH): static_frame,
    }
    written = _install_handoff_dss_fakes(monkeypatch, source_frames)
    mappings = [
        _handoff_mapping(
            hms_source,
            mapping_id="split-left",
            source_pathname=FLOW_PATH,
            output_pathname=LEFT_PATH,
            multiplier=0.5,
            conversion="linear",
        ),
        _handoff_mapping(
            hms_source,
            mapping_id="split-right",
            source_pathname=FLOW_PATH,
            output_pathname=RIGHT_PATH,
            multiplier=0.5,
            conversion="linear",
        ),
        _handoff_mapping(
            static_source,
            mapping_id="static-upstream",
            source_pathname=STATIC_PATH,
            output_pathname=STATIC_PATH,
        ),
        _handoff_mapping(
            static_source,
            mapping_id="static-upstream-alias",
            source_pathname=STATIC_PATH,
            output_pathname=STATIC_PATH,
        ),
    ]

    first = HmsResultsProducts.materialize_handoff(
        mappings,
        tmp_path / "first",
        model_start="2019-09-18T13:00:00",
        model_end="2019-09-18T13:10:00",
    )
    second = HmsResultsProducts.materialize_handoff(
        mappings,
        tmp_path / "second",
        model_start="2019-09-18T13:00:00",
        model_end="2019-09-18T13:10:00",
    )

    written_values = {
        pathname: frame["value"].tolist()
        for (_dss_file, pathname), frame in written.items()
    }
    assert written_values[LEFT_PATH] == [5.0, 10.0, 15.0]
    assert written_values[RIGHT_PATH] == [5.0, 10.0, 15.0]
    assert written_values[STATIC_PATH] == [4.0, 5.0, 6.0]
    assert first["boundary_pathnames"] == {
        "split-left": LEFT_PATH,
        "split-right": RIGHT_PATH,
        "static-upstream": STATIC_PATH,
        "static-upstream-alias": STATIC_PATH,
    }
    assert first["dss"]["sha256"] == second["dss"]["sha256"]
    assert first["product_manifest"]["sha256"] == second["product_manifest"]["sha256"]
    assert (
        first["provenance_manifest"]["sha256"]
        == second["provenance_manifest"]["sha256"]
    )

    provenance = json.loads(Path(first["provenance_manifest"]["path"]).read_text())
    assert provenance["schema"] == "hms-commander/hydrologic-handoff-provenance/1.0"
    assert str(tmp_path) not in json.dumps(provenance)
    assert provenance["mappings"][0]["mapping_id"] == "split-left"
    manifest = json.loads(Path(first["product_manifest"]["path"]).read_text())
    assert manifest["source"]["sha256"] == first["dss"]["sha256"]
    assert "hydrologic-handoff-provenance" in manifest["assets"]


def test_handoff_writer_serializes_child_request_and_removes_it(
    monkeypatch,
    tmp_path,
):
    source = tmp_path / "source.dss"
    source.write_bytes(b"source")
    mappings = HmsResultsProducts._normalize_handoff_mappings(
        [
            _handoff_mapping(
                source,
                mapping_id="boundary",
                source_pathname=FLOW_PATH,
                output_pathname=LEFT_PATH,
            )
        ]
    )
    output = tmp_path / "handoff.dss"
    observed = {}

    def run_child(command, **kwargs):
        request_path = Path(command[command.index("--request") + 1])
        observed.update(json.loads(request_path.read_text(encoding="utf-8")))
        output.write_bytes(b"handoff")
        return subprocess.CompletedProcess(command, 0, "", "")

    products_module = importlib.import_module("hms_commander.HmsResultsProducts")
    monkeypatch.setattr(products_module.subprocess, "run", run_child)
    HmsResultsProducts._write_handoff_subprocess(
        mappings,
        output,
        start=pd.Timestamp("2019-09-18T13:00:00"),
        end=pd.Timestamp("2019-09-18T13:10:00"),
    )

    assert observed["mappings"][0]["source_dss"] == str(source.resolve())
    assert observed["output"] == str(output)
    assert not output.with_suffix(".child-request.json").exists()


@pytest.mark.requires_java
@pytest.mark.skipif(
    os.environ.get("HMS_COMMANDER_RUN_DSS_INTEGRATION") != "1",
    reason="set HMS_COMMANDER_RUN_DSS_INTEGRATION=1 for DSS handoff coverage",
)
def test_materialize_handoff_releases_dss_before_atomic_publication(tmp_path):
    source = tmp_path / "source.dss"
    writer = """
import sys
from pathlib import Path

import pandas as pd

from hms_commander import DssCore

DssCore.write_timeseries(
    Path(sys.argv[1]),
    sys.argv[2],
    pd.date_range("2019-09-18T13:00:00", periods=3, freq="5min"),
    [10.0, 20.0, 15.0],
    units="CFS",
    data_type="INST-VAL",
    interval_minutes=5,
)
"""
    completed = subprocess.run(
        [sys.executable, "-c", writer, str(source), FLOW_PATH],
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout

    package = tmp_path / "handoff-package"
    result = HmsResultsProducts.materialize_handoff(
        [
            _handoff_mapping(
                source,
                mapping_id="outlet",
                source_pathname=FLOW_PATH,
                output_pathname=LEFT_PATH,
            )
        ],
        package,
        model_start="2019-09-18T13:00:00",
        model_end="2019-09-18T13:10:00",
    )

    assert package.is_dir()
    assert Path(result["dss"]["path"]).is_file()
    assert result["status"]["all_required_pathnames_valid"] is True
    assert not list(tmp_path.glob(".handoff-package-*"))


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("checksum", "checksum"),
        ("units", "units"),
        ("window", "model window"),
        ("collision", "output pathname"),
        ("timestamp", "ISO timestamp string"),
    ],
)
def test_materialize_handoff_fails_closed_before_publication(
    monkeypatch,
    tmp_path,
    mutation,
    message,
):
    source = tmp_path / "source.dss"
    source.write_bytes(b"source")
    frame = _frame(values=(10.0, 20.0, 30.0))
    if mutation == "units":
        frame.attrs["units"] = "CMS"
    _install_handoff_dss_fakes(monkeypatch, {(source.resolve(), FLOW_PATH): frame})
    mapping = _handoff_mapping(
        source,
        mapping_id="split-left",
        source_pathname=FLOW_PATH,
        output_pathname=LEFT_PATH,
        multiplier=0.5,
        conversion="linear",
    )
    mappings = [mapping]
    model_start = "2019-09-18T13:00:00"
    model_end = "2019-09-18T13:10:00"
    if mutation == "checksum":
        mapping["source_sha256"] = "0" * 64
    elif mutation == "window":
        model_end = "2019-09-18T13:15:00"
    elif mutation == "collision":
        mappings.append(
            {
                **mapping,
                "mapping_id": "other",
                "output_pathname": LEFT_PATH.lower(),
                "multiplier": 0.25,
            }
        )
    elif mutation == "timestamp":
        model_start = 1_568_812_800

    with pytest.raises((RuntimeError, ValueError), match=message):
        HmsResultsProducts.materialize_handoff(
            mappings,
            tmp_path / "products",
            model_start=model_start,
            model_end=model_end,
        )
