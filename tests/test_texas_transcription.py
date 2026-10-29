"""
Transcription checks for the bundled Texas empirical hyetograph table.

These tests need the source PDFs and PyMuPDF, so they are skipped otherwise.
Set the paths with environment variables:

    TEXAS_SIR_PDF   sir2004-5075.pdf   (USGS SIR 2004-5075)
    TEXAS_HDM_PDF   hh-2019-hdm.pdf    (TxDOT HDM, September 2019; optional)

The OCR (image) reading is run by scripts/texas_hyetograph_transcription.py;
it is too slow for the test suite. Here the bundled CSV is compared with a
fresh text-layer reading of the report, Table 4 is compared with Supplement 5,
and HDM Table 4-16 is compared with the USGS values.
"""

import os
import re
from pathlib import Path

import numpy as np
import pytest
from hms_commander import TexasStorm
from scripts import texas_hyetograph_transcription as tt

pymupdf = pytest.importorskip("pymupdf")

SIR = os.environ.get("TEXAS_SIR_PDF")
HDM = os.environ.get("TEXAS_HDM_PDF")

needs_sir = pytest.mark.skipif(
    not SIR or not Path(SIR).exists(), reason="TEXAS_SIR_PDF not set"
)
needs_hdm = pytest.mark.skipif(
    not HDM or not Path(HDM).exists(), reason="TEXAS_HDM_PDF not set"
)


@pytest.fixture(scope="module")
def sir_text_layer():
    return tt.read_text_layer(pymupdf.open(SIR))


@needs_sir
def test_csv_matches_text_layer(sir_text_layer):
    blocks, _ = sir_text_layer
    df = TexasStorm._load_empirical()
    for (supp, q, dur), (_, arr) in blocks.items():
        g = df[
            (df["supplement"] == supp)
            & (df["quartile"] == q)
            & (df["duration_class"] == dur)
        ]
        assert len(g) == 39
        got = g[[f"p{p}" for p in tt.PERCENTILES]].to_numpy(dtype=float)
        assert np.array_equal(np.isnan(got), np.isnan(arr))
        assert np.allclose(np.nan_to_num(got), np.nan_to_num(arr), atol=1e-9, rtol=0)


@needs_sir
def test_table4_equals_supplement5_median(sir_text_layer):
    blocks, tables = sir_text_layer
    t4 = tables[4]
    for j, q in enumerate(("1", "2", "3", "4", "all")):
        assert np.array_equal(t4[:, j], blocks[(5, q, "0-72")][1][:, 5])


@needs_sir
def test_tables5_6_equal_supplement5(sir_text_layer):
    blocks, tables = sir_text_layer
    for tno, qs in ((5, ("1", "2")), (6, ("3", "4"))):
        for k, q in enumerate(qs):
            for m, pidx in enumerate((0, 5, 10)):  # 10th, 50th, 90th
                assert np.array_equal(
                    tables[tno][:, 3 * k + m], blocks[(5, q, "0-72")][1][:, pidx]
                )


@needs_sir
@needs_hdm
def test_hdm_table_4_16_vs_usgs(sir_text_layer):
    """HDM Table 4-16 equals the USGS combined 0-72 hr values except the 2.5% median."""
    blocks, _ = sir_text_layer
    toks = []
    for pg in (142, 143, 144):
        for ln in pymupdf.open(HDM)[pg].get_text().splitlines():
            if re.match(r"^\d+\.\d+$", ln.strip()):
                toks.append(float(ln))
    arr = np.array(toks[:123]).reshape(41, 3)
    assert arr[0].tolist() == [0.0, 0.0, 0.0] and arr[-1].tolist() == [100.0] * 3
    usgs = blocks[(5, "all", "0-72")][1]
    diffs = [
        (arr[i + 1, 0], arr[i + 1, 1], usgs[i, 5])
        for i in range(39)
        if arr[i + 1, 1] != usgs[i, 5]
    ] + [
        (arr[i + 1, 0], arr[i + 1, 2], usgs[i, 10])
        for i in range(39)
        if arr[i + 1, 2] != usgs[i, 10]
    ]
    assert diffs == [(2.5, 8.70, 6.37)]
