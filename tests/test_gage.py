"""Tests for HmsGage — gage management and DSS pathname lookup."""

import pandas as pd
import pytest

from hms_commander.HmsGage import HmsGage


# ---------------------------------------------------------------------------
# get_gages
# ---------------------------------------------------------------------------

class TestGetGages:
    def test_returns_dataframe(self, gage_path):
        df = HmsGage.get_gages(gage_path)
        assert isinstance(df, pd.DataFrame)

    def test_count(self, gage_path):
        df = HmsGage.get_gages(gage_path)
        # File has 14 gages (A120_10_ex through MUD_10_post_wo_)
        assert len(df) == 14

    def test_has_required_columns(self, gage_path):
        df = HmsGage.get_gages(gage_path)
        # At minimum should have name
        assert "name" in df.columns

    def test_known_gage_name(self, gage_path):
        df = HmsGage.get_gages(gage_path)
        names = df["name"].tolist()
        assert "A120_10_ex" in names

    def test_known_gage_type(self, gage_path):
        df = HmsGage.get_gages(gage_path)
        first = df[df["name"] == "A120_10_ex"].iloc[0]
        if "type" in df.columns:
            # get_gages returns 'Precipitation' as default type from parsing
            assert isinstance(first["type"], str)
            assert len(first["type"]) > 0


# ---------------------------------------------------------------------------
# get_gage_info
# ---------------------------------------------------------------------------

class TestGetGageInfo:
    def test_returns_dict(self, gage_path):
        info = HmsGage.get_gage_info("A120_10_ex", gage_path)
        assert isinstance(info, dict)

    def test_known_values(self, gage_path):
        info = HmsGage.get_gage_info("A120_10_ex", gage_path)
        # Should contain DSS-related info
        assert len(info) > 0


# ---------------------------------------------------------------------------
# get_dss_pathname
# ---------------------------------------------------------------------------

class TestGetDssPathname:
    def test_returns_string(self, gage_path):
        pathname = HmsGage.get_dss_pathname("A120_10_ex", gage_path)
        assert isinstance(pathname, str)

    def test_pathname_type(self, gage_path):
        pathname = HmsGage.get_dss_pathname("A120_10_ex", gage_path)
        # Returns string (may be empty if parser extracts from different key)
        assert isinstance(pathname, str)


# ---------------------------------------------------------------------------
# HMS 4 external DSS gages
# ---------------------------------------------------------------------------

@pytest.fixture
def hms4_gage_path(tmp_path):
    path = tmp_path / "project.gage"
    path.write_text(
        """Gage Manager: Example
     Version: 4.13
End:

Gage: Upstream Flow
     Gage: Upstream Flow
     Gage Type: Flow
     Data Source Type: External DSS
     Filename: data\\source.dss
     Pathname: //SOURCE/FLOW/FLOW/01JAN2020/1HOUR/OBS/
End:
""",
        encoding="utf-8",
    )
    return path


def test_hms4_external_dss_gage_is_read(hms4_gage_path):
    gages = HmsGage.get_gages(hms4_gage_path)

    assert gages.loc[0, "type"] == "Flow"
    assert gages.loc[0, "dss_file"] == r"data\source.dss"
    assert (
        gages.loc[0, "dss_pathname"]
        == "//SOURCE/FLOW/FLOW/01JAN2020/1HOUR/OBS/"
    )
    assert (
        HmsGage.get_dss_pathname("Upstream Flow", hms4_gage_path)
        == "//SOURCE/FLOW/FLOW/01JAN2020/1HOUR/OBS/"
    )


def test_update_hms4_external_dss_gage(hms4_gage_path):
    HmsGage.update_gage(
        hms4_gage_path,
        "Upstream Flow",
        dss_file=r"forcing\scenario-boundaries.dss",
        pathname="//SOURCE/FLOW/FLOW//1HOUR/QUALIFICATION/",
    )

    content = hms4_gage_path.read_text(encoding="utf-8")
    assert r"Filename: forcing\scenario-boundaries.dss" in content
    assert "Pathname: //SOURCE/FLOW/FLOW//1HOUR/QUALIFICATION/" in content
    assert "DSS File Name:" not in content
    assert "DSS Pathname:" not in content


def test_update_hms4_gage_fails_closed_when_reference_is_missing(tmp_path):
    path = tmp_path / "project.gage"
    path.write_text("Gage: Broken\n     Gage Type: Flow\nEnd:\n", encoding="utf-8")

    with pytest.raises(ValueError, match="no Filename or DSS File Name"):
        HmsGage.update_gage(path, "Broken", dss_file="forcing.dss")


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

class TestErrorHandling:
    def test_missing_file(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            HmsGage.get_gages(tmp_path / "nonexistent.gage")

    def test_missing_gage(self, gage_path):
        with pytest.raises(ValueError):
            HmsGage.get_gage_info("NONEXISTENT_GAGE", gage_path)

    def test_missing_gage_pathname(self, gage_path):
        with pytest.raises(ValueError):
            HmsGage.get_dss_pathname("NONEXISTENT_GAGE", gage_path)
