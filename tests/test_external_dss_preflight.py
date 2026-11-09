"""External-input declarations use small HMS text fixtures and offline readers."""

import pandas as pd
import pytest

from hms_commander import HmsGage
from hms_commander.HmsPrj import HmsPrj
from hms_commander._external_dss import preflight
from hms_commander.dss.hms_dss import HmsDss


@pytest.fixture
def inputs(tmp_path):
    (tmp_path / "input.dss").write_bytes(b"synthetic input, no native read")
    gage = tmp_path / "project.gage"
    gage.write_text(
        "Gage: Test\n     Gage Type: Flow\n     Variant: Main\n"
        "     Data Source Type: External DSS\n     DSS File Name: input.dss\n"
        "     DSS Pathname: /A/B/FLOW//1HOUR/F/\n     End Variant: Main\nEnd:\n"
    )
    return tmp_path, gage


def test_legacy_aliases_match_project_and_gage_apis(inputs):
    folder, gage = inputs
    project = HmsPrj()
    project.project_folder = folder
    project._build_gage_dataframe()
    frame = HmsGage.get_gages(gage)
    assert project.gage_df.loc[0, "dss_file"] == frame.loc[0, "dss_file"] == "input.dss"
    assert project.gage_df.loc[0, "dss_pathname"] == frame.loc[0, "dss_pathname"]
    assert project.gage_df.loc[0, "has_dss_reference"]


def test_inventory_only_is_not_a_native_read_pass(inputs, monkeypatch):
    folder, _ = inputs
    monkeypatch.setattr(
        HmsDss, "get_catalog", lambda *a: pytest.fail("Native catalog attempted")
    )
    report = preflight(folder, False)
    assert not report["passed"]
    assert report["references"][0]["status"] == "unverified"


def test_catalog_and_record_reads_required(inputs, monkeypatch):
    folder, _ = inputs
    exact = "/A/B/FLOW/01JAN2020/1HOUR/F/"
    monkeypatch.setattr(HmsDss, "get_catalog", lambda *a: [exact])
    read = []
    monkeypatch.setattr(
        HmsDss,
        "read_timeseries",
        lambda path, selector: read.append(selector) or pd.DataFrame({"value": [1]}),
    )
    report = preflight(folder, True)
    assert report["passed"] and read == [exact]
    assert report["references"][0]["matched_pathnames"] == [exact]


@pytest.mark.parametrize(
    "failure", ["missing_file", "missing_record", "bad_read", "conflict", "variants"]
)
def test_inputs_fail_closed_with_exact_identity(inputs, monkeypatch, failure):
    folder, gage = inputs
    monkeypatch.setattr(
        HmsDss, "get_catalog", lambda *a: ["/A/B/FLOW/01JAN2020/1HOUR/F/"]
    )
    if failure == "missing_file":
        (folder / "input.dss").unlink()
    elif failure == "missing_record":
        monkeypatch.setattr(HmsDss, "get_catalog", lambda *a: [])
    elif failure == "bad_read":

        def read(*args):
            raise RuntimeError("DataFormatException: fixture corrupt record")

        monkeypatch.setattr(HmsDss, "read_timeseries", read)
    elif failure == "conflict":
        gage.write_text(
            gage.read_text().replace(
                "     End Variant:", "     Filename: other.dss\n     End Variant:"
            )
        )
    else:
        gage.write_text(
            gage.read_text().replace(
                "\nEnd:", "\n     Variant: Other\n     End Variant: Other\nEnd:"
            )
        )
    report = preflight(folder, True)
    assert not report["passed"]
    record = report["references"][0]
    assert record["name"] == "Test" and record["source_file"] == str(gage)
    assert record["issues"] and record["status"] == "failed"


@pytest.mark.parametrize("corrupt", [False, True])
def test_grid_preflight_delegates_exact_reads_and_reports_decompression(
    inputs, monkeypatch, corrupt
):
    import sys
    from types import SimpleNamespace

    folder, gage = inputs
    gage.unlink()
    (folder / "project.grid").write_text(
        "Grid: Rain\n     Data Source Type: External DSS\n"
        "     Filename: input.dss\n     Pathname: /A/B/PRECIP///F/\nEnd:\n"
    )
    exact = "/A/B/PRECIP/01JAN2020/02JAN2020/F/"
    monkeypatch.setattr(HmsDss, "get_catalog", lambda *args: [exact])
    reads = []

    def read(path, selector):
        reads.append(selector)
        if corrupt:
            raise RuntimeError("DataFormatException: fixture decompression failed")
        return {"data": [[1.0]]}

    monkeypatch.setitem(
        sys.modules,
        "ras_commander.dss",
        SimpleNamespace(RasDss=SimpleNamespace(read_grid=read)),
    )
    report = preflight(folder, True)
    assert reads == [exact]
    assert report["passed"] is (not corrupt)
    assert report["references"][0]["matched_pathnames"] == [exact]
    if corrupt:
        assert "decompression failed" in report["references"][0]["issues"][0]


@pytest.mark.parametrize("kind", ["Gage", "Grid"])
def test_unterminated_external_block_is_reported_not_omitted(
    tmp_path, monkeypatch, kind
):
    path = tmp_path / ("input." + kind.lower())
    path.write_text(
        kind + ": Broken\n     Data Source Type: External DSS\n"
        "     Filename: input.dss\n     Pathname: /A/B/C/D/E/F/\n"
    )
    monkeypatch.setattr(
        HmsDss, "get_catalog", lambda *args: pytest.fail("Unterminated input read")
    )
    report = preflight(tmp_path, True)
    assert not report["passed"]
    assert report["references"]
    assert all(record["status"] == "failed" for record in report["references"])
    assert any(
        "unterminated" in issue.lower() or "malformed" in issue.lower()
        for record in report["references"]
        for issue in record["issues"]
    )


@pytest.mark.parametrize(
    "key,conflict",
    [("DSS File Name", "conflicting.dss"), ("DSS Pathname", "/OTHER/B/FLOW//1HOUR/F/")],
)
def test_conflicting_repeated_keys_rejected_before_parser_collapse(
    inputs, monkeypatch, key, conflict
):
    folder, gage = inputs
    content = gage.read_text()
    gage.write_text(
        content.replace(
            "     " + key + ":", "     " + key + ": " + conflict + "\n     " + key + ":"
        )
    )
    monkeypatch.setattr(
        HmsDss, "get_catalog", lambda *args: ["/A/B/FLOW/01JAN2020/1HOUR/F/"]
    )
    monkeypatch.setattr(
        HmsDss, "read_timeseries", lambda *args: pd.DataFrame({"value": [1]})
    )
    report = preflight(folder, True)
    assert not report["passed"]
    assert "conflicting repeated" in report["references"][0]["issues"][0].lower()


@pytest.mark.parametrize("malformation", ["unclosed_then_valid", "inline_terminator"])
def test_malformed_block_boundaries_cannot_borrow_a_later_terminator(
    inputs, monkeypatch, malformation
):
    folder, gage = inputs
    content = gage.read_text()
    if malformation == "unclosed_then_valid":
        content = content.removesuffix("End:\n") + content.replace(
            "Gage: Test", "Gage: Second"
        )
    else:
        content = content.replace("End:\n", "     Description: not a terminator End:\n")
    gage.write_text(content)
    monkeypatch.setattr(
        HmsDss, "get_catalog", lambda *args: pytest.fail("Malformed input read")
    )
    report = preflight(folder, True)
    assert not report["passed"]
    assert any(
        "malformed" in issue.lower()
        for record in report["references"]
        for issue in record["issues"]
    )
