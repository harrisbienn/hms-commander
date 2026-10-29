"""
Transcribe and cross-verify the USGS SIR 2004-5075 empirical hyetograph tables.

Source: Williams-Sether, T., Asquith, W.H., Thompson, D.B., Cleveland, T.G.,
and Fang, X., 2004, Empirical, dimensionless, cumulative-rainfall hyetographs
developed from 1959-86 storm data for selected small watersheds in Texas:
U.S. Geological Survey Scientific Investigations Report 2004-5075.

Tables transcribed (PDF page index is 0-based; printed page in parentheses):
    Table 4  (printed p. 15)     smoothed 50th percentile, 0-72 hr, by quartile
    Table 5  (printed p. 16)     10th/50th/90th, quartiles 1 and 2, 0-72 hr
    Table 6  (printed p. 17)     10th/50th/90th, quartiles 3 and 4, 0-72 hr
    Supplement 4 (printed pp. 76-100)   untrimmed + smoothed percentiles
    Supplement 5 (printed pp. 101-125)  trimmed + smoothed percentiles

Two independent readings are made of every table:
    A. the PDF text layer (PyMuPDF get_text)
    B. the rendered page image (300 dpi PNG-equivalent) read with RapidOCR

The script compares A and B cell-for-cell and exits non-zero on any
disagreement, printing page/block/row/column and both readings so the page
image can be inspected. Run on the full report (22,113 cells: Tables 4-6 and
Supplements 4-5) there were no disagreements. OCR boxes that swallowed
neighbouring page text (29 cells) are salvaged only when exactly one 'dd.dd'
number is present (or no number, for a '--' cell).

The reading also exposes properties of the published data, listed by the
structural checks: a few smoothed columns are not monotone and a few rows
have percentiles out of order. Both readings agree on them and the page image
confirms them (for example Supplement 5, third quartile, 12-24 hr, 10th
percentile at 15.0-25.0: 1.70, 1.60, 1.51, 1.46, 1.43).

Usage (needs ``pip install pymupdf rapidocr-onnxruntime``):
    python scripts/texas_hyetograph_transcription.py --pdf sir2004-5075.pdf ^
        --cache .scratch/ocr_cache.json            # verify only
    python scripts/texas_hyetograph_transcription.py --pdf ... --write-csv

``--write-csv`` writes ``hms_commander/data/texas_empirical_hyetographs.csv``
from the text-layer reading, but only if the two readings agree.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import numpy as np

PERCENTILES = [10, 20, 25, 30, 40, 50, 60, 70, 75, 80, 90]
INTERVALS = [2.5 * k for k in range(1, 40)]  # 2.5 ... 97.5
DURATIONS = ["0-6", "6-12", "12-24", "24-72", "0-72"]
QUARTILES = ["1", "2", "3", "4", "all"]
SUPP_PAGES = {4: range(80, 105), 5: range(105, 130)}  # 0-based PDF page index
TABLE_PAGES = {4: (20, 5), 5: (21, 6), 6: (22, 6)}  # page, data columns

_HDR_Q = re.compile(
    r"(First|Second|Third|Fourth)-quartile storms; storm duration (\d+) to (\d+) hours"
)
_HDR_ALL = re.compile(
    r"First- through fourth-quartile storms combined; storm duration "
    r"(\d+) to (\d+) hours"
)
_NUM = re.compile(r"^\d*\.?\d+$")
_QMAP = {"First": "1", "Second": "2", "Third": "3", "Fourth": "4"}


def _to_val(tok: str):
    """Parse one table cell; '--' (no data) -> nan; raises ValueError otherwise."""
    tok = tok.strip().replace(",", ".")
    if tok and set(tok) <= set("-_–—−"):
        return float("nan")
    if not _NUM.match(tok):
        raise ValueError(f"unparsable cell {tok!r}")
    return float(tok)


def _block_key(text: str):
    text = re.sub(r"\bO\b", "0", text)  # OCR reads the digit 0 as the letter O
    m = _HDR_ALL.search(text)
    if m:
        return ("all", f"{m.group(1)}-{m.group(2)}")
    m = _HDR_Q.search(text)
    if m:
        return (_QMAP[m.group(1)], f"{m.group(2)}-{m.group(3)}")
    return None


# --------------------------------------------------------------------------
# Reading A: text layer
# --------------------------------------------------------------------------
def read_text_layer(doc):
    """Return (supp_blocks, tables) from the PDF text layer."""
    blocks = {}
    for supp, pages in SUPP_PAGES.items():
        for pg in pages:
            lines = [ln.strip() for ln in doc[pg].get_text().splitlines()]
            idx = next(i for i, ln in enumerate(lines) if _block_key(ln))
            key = _block_key(lines[idx])
            toks = []
            for ln in lines[idx + 1 :]:  # noqa: E203
                if ln.startswith("Supplement"):
                    break
                toks.append(ln)
            assert len(toks) == 39 * 12, (pg, len(toks))
            arr = np.array([_to_val(t) for t in toks]).reshape(39, 12)
            assert np.allclose(arr[:, 0], INTERVALS), (supp, pg, "interval column")
            blocks[(supp,) + key] = (pg, arr[:, 1:])
    tables = {}
    for tno, (pg, ncols) in TABLE_PAGES.items():
        lines = [ln.strip() for ln in doc[pg].get_text().splitlines()]
        start = next(i for i, ln in enumerate(lines) if ln == "2.5")
        toks = lines[start:]
        assert len(toks) == 39 * (ncols + 1), (tno, len(toks))
        arr = np.array([_to_val(t) for t in toks]).reshape(39, ncols + 1)
        assert np.allclose(arr[:, 0], INTERVALS), (tno, "interval column")
        tables[tno] = arr[:, 1:]
    return blocks, tables


# --------------------------------------------------------------------------
# Reading B: image + OCR
# --------------------------------------------------------------------------
def _ocr_page(ocr, doc, pg, dpi=300):
    import pymupdf  # noqa: F401

    pm = doc[pg].get_pixmap(dpi=dpi)
    img = np.frombuffer(pm.samples, dtype=np.uint8).reshape(pm.height, pm.width, pm.n)
    res, _ = ocr(img)
    out = []
    for box, txt, conf in res or []:
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        out.append([float(np.mean(xs)), float(np.mean(ys)), txt, float(conf)])
    return out


def _grid_from_ocr(items, ncols, y_min, header_x=None):
    """Rebuild an (39, ncols) grid of strings from OCR boxes below y_min."""
    cells = [it for it in items if it[1] > y_min]
    # Row clustering on y.
    cells.sort(key=lambda c: c[1])
    rows, cur = [], [cells[0]]
    for c in cells[1:]:
        if c[1] - np.mean([k[1] for k in cur]) < 12:
            cur.append(c)
        else:
            rows.append(cur)
            cur = [c]
    rows.append(cur)
    rows = [r for r in rows if len(r) >= 2 or _is_interval(r[0][2])]
    if header_x is not None:
        # Column edges from the printed column headings (robust to columns
        # that contain only '--').
        hx = np.sort(header_x)
        edges = [(hx[i] + hx[i + 1]) / 2 for i in range(len(hx) - 1)]
    else:
        # Column centres: split sorted x centres at the (ncols-1) largest gaps.
        xs = np.sort([c[0] for r in rows for c in r])
        gaps = np.diff(xs)
        cut = np.sort(np.argsort(gaps)[-(ncols - 1) :])  # noqa: E203
        edges = [(xs[i] + xs[i + 1]) / 2 for i in cut]
    grid = {}
    for r in rows:
        first = min(r, key=lambda c: c[0])
        if not _is_interval(first[2]):
            continue
        iv = float(first[2])
        row = [None] * ncols
        for c in r:
            j = int(np.searchsorted(edges, c[0]))
            if row[j] is not None:
                row[j] += " " + c[2]  # collision -> will fail parsing, reported
            else:
                row[j] = c[2]
        grid[iv] = row
    return grid


def _is_interval(txt):
    try:
        v = float(txt)
    except ValueError:
        return False
    return abs(v * 2 - round(v * 2)) < 1e-9 and 2.5 <= v <= 97.5 and (v / 2.5) % 1 == 0


def read_ocr(doc, cache_path: Path | None):
    cache = {}
    if cache_path and cache_path.exists():
        cache = json.loads(cache_path.read_text())
    ocr = None
    pages = [pg for pg, _ in TABLE_PAGES.values()]
    for rng in SUPP_PAGES.values():
        pages += list(rng)
    for pg in pages:
        if str(pg) in cache:
            continue
        if ocr is None:
            from rapidocr_onnxruntime import RapidOCR

            ocr = RapidOCR()
        cache[str(pg)] = _ocr_page(ocr, doc, pg)
        if cache_path:
            cache_path.write_text(json.dumps(cache))
        print(f"  OCR page {pg} done", flush=True)

    blocks, tables, bad = {}, {}, []
    salvaged = []

    def to_arr(grid, ncols, where):
        arr = np.full((39, ncols - 1), np.nan)
        for i, iv in enumerate(INTERVALS):
            row = grid.get(iv)
            if row is None:
                bad.append((where, iv, "row missing in OCR"))
                arr[i, :] = np.nan
                continue
            for j in range(1, ncols):
                if row[j] is None:
                    # No OCR box in this cell: read as empty. Counts as agreement
                    # only where the text layer is also '--'; otherwise it is
                    # reported as a disagreement by compare().
                    continue
                try:
                    arr[i, j - 1] = _to_val(row[j])
                except ValueError as e:
                    # A box can swallow neighbouring page text (running head or
                    # footer). Accept it only if exactly one 'dd.dd' number is in it.
                    nums = re.findall(r"(?<![\d.])\d{1,3}\.\d{2}(?![\d])", row[j])
                    if len(nums) == 1:
                        arr[i, j - 1] = float(nums[0])
                        salvaged.append((where, iv, j, row[j]))
                    elif not re.search(r"\d\.\d", row[j]):
                        # no decimal number (a '--' cell merged with page text): empty
                        salvaged.append((where, iv, j, row[j]))
                    else:
                        bad.append((where, iv, f"col {j}: {e}"))
        return arr

    for supp, rng in SUPP_PAGES.items():
        for pg in rng:
            items = cache[str(pg)]
            title = next((it for it in items if _block_key(it[2])), None)
            key = _block_key(title[2])
            hdr = [
                it
                for it in items
                if re.fullmatch(r"\d0th|25th|75th", it[2].replace("O", "0"))
                and abs(it[1] - title[1]) < 120
                and it[1] < title[1]
            ]
            interval_hdr = [it for it in items if it[2] == "Interval"]
            assert len(hdr) == 11 and interval_hdr, (pg, len(hdr))
            header_x = [interval_hdr[0][0]] + [it[0] for it in hdr]
            grid = _grid_from_ocr(items, 12, title[1], header_x)
            blocks[(supp,) + key] = (pg, to_arr(grid, 12, (supp,) + key))
    for tno, (pg, ncols) in TABLE_PAGES.items():
        items = cache[str(pg)]
        hdr = [it for it in items if it[2].startswith("Table")][0]
        # data starts below the column-header block; first interval cell "2.5"
        y0 = min(it[1] for it in items if it[2] == "2.5") - 12
        grid = _grid_from_ocr(items, ncols + 1, y0)
        tables[tno] = to_arr(grid, ncols + 1, ("Table", tno))
    print(f"OCR cells salvaged from merged text boxes: {len(salvaged)}")
    return blocks, tables, bad


# --------------------------------------------------------------------------
def compare(a_blocks, a_tables, b_blocks, b_tables, ocr_bad):
    n_cells = n_diff = 0
    diffs = []
    assert set(a_blocks) == set(b_blocks), "block key sets differ"
    for key in a_blocks:
        pa, A = a_blocks[key]
        pb, B = b_blocks[key]
        assert pa == pb, (key, pa, pb)
        for i, j in np.ndindex(A.shape):
            n_cells += 1
            same = (np.isnan(A[i, j]) and np.isnan(B[i, j])) or A[i, j] == B[i, j]
            if not same:
                n_diff += 1
                diffs.append((key, INTERVALS[i], PERCENTILES[j], A[i, j], B[i, j]))
    for t in a_tables:
        A, B = a_tables[t], b_tables[t]
        for i, j in np.ndindex(A.shape):
            n_cells += 1
            if A[i, j] != B[i, j] and not (np.isnan(A[i, j]) and np.isnan(B[i, j])):
                n_diff += 1
                diffs.append((("Table", t), INTERVALS[i], j, A[i, j], B[i, j]))
    return n_cells, n_diff, diffs


def structural_checks(a_blocks):
    """Independent sanity checks on the text-layer reading."""
    issues = []
    for key, (pg, A) in a_blocks.items():
        for j in range(A.shape[1]):
            col = A[:, j]
            v = col[~np.isnan(col)]
            if len(v) and (np.any(np.diff(v) < 0) or v.max() > 100 or v.min() < 0):
                issues.append(
                    (key, PERCENTILES[j], "cumulative column not monotone/in [0,100]")
                )
        # percentile ordering across columns at a given interval
        for i in range(A.shape[0]):
            r = A[i, :]
            r = r[~np.isnan(r)]
            if np.any(np.diff(r) < -1e-9):
                issues.append((key, INTERVALS[i], "percentile order violated"))
    return issues


def write_csv(a_blocks, out: Path, pdf_name: str):
    header = [
        "# Empirical dimensionless cumulative-rainfall hyetographs, Texas.",
        (
            "# Source: Williams-Sether, T., Asquith, W.H., Thompson, D.B., Cleveland, "
            "T.G., and Fang, X.,"
        ),
        (
            "#   2004, USGS Scientific Investigations Report 2004-5075 "
            "(TxDOT Project 0-4194-3)."
        ),
        "# Supplement 4 = untrimmed and smoothed percentiles (printed pp. 76-100);",
        "# Supplement 5 = trimmed and smoothed percentiles (printed pp. 101-125).",
        (
            "# Values are cumulative rainfall as percent of storm total at the center "
            "of each 2.5-percent"
        ),
        (
            "#   storm-duration interval (2.5 ... 97.5). Empty cell = '--' "
            "(no data) in the source."
        ),
        (
            "# quartile: 1-4 = first-fourth quartile storms (quartile of "
            "storm duration holding the"
        ),
        "#   most rain); all = first- through fourth-quartile storms combined.",
        "# duration_class: storm duration in hours (0-6, 6-12, 12-24, 24-72, 0-72).",
        (
            "# Transcribed twice (PDF text layer and OCR of page images) and "
            "compared cell by cell by"
        ),
        (
            "#   scripts/texas_hyetograph_transcription.py (no disagreements in 22,113 "
            "cells)."
        ),
        (
            "# No corrections were applied. Report Table 4 (0-72 hr medians) "
            "equals the Supplement 5"
        ),
        (
            "#   0-72 hr 50th-percentile columns; Tables 5-6 equal its "
            "10th/50th/90th columns."
        ),
        (
            "# Some smoothed columns are not monotone in the source "
            "(see tests/test_texas_storm.py)."
        ),
    ]
    cols = ["supplement", "quartile", "duration_class", "interval_pct"] + [
        f"p{p}" for p in PERCENTILES
    ]
    lines = header + [",".join(cols)]
    for supp in (4, 5):
        for q in QUARTILES:
            for dur in DURATIONS:
                _, A = a_blocks[(supp, q, dur)]
                for i, iv in enumerate(INTERVALS):
                    cells = ["" if np.isnan(x) else f"{x:.2f}" for x in A[i]]
                    lines.append(",".join([str(supp), q, dur, f"{iv:g}"] + cells))
    out.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--cache", default=None, help="JSON cache for OCR results")
    ap.add_argument("--write-csv", action="store_true")
    args = ap.parse_args(argv)

    import pymupdf

    doc = pymupdf.open(args.pdf)
    a_blocks, a_tables = read_text_layer(doc)
    print(
        f"Reading A (text layer): {len(a_blocks)} supplement blocks, "
        f"{len(a_tables)} tables"
    )
    b_blocks, b_tables, ocr_bad = read_ocr(
        doc, Path(args.cache) if args.cache else None
    )
    print(f"Reading B (OCR): {len(b_blocks)} supplement blocks, {len(b_tables)} tables")

    n_cells, n_diff, diffs = compare(a_blocks, a_tables, b_blocks, b_tables, ocr_bad)
    print(f"Cells compared: {n_cells}; disagreements: {n_diff}")
    for d in diffs:
        print("  DIFF", d)
    for b in ocr_bad:
        print("  OCR-unparsed", b)
    issues = structural_checks(a_blocks)
    print(f"Structural issues (text layer): {len(issues)}")
    for s in issues[:50]:
        print("  ", s)

    if args.write_csv:
        if n_diff or ocr_bad:
            print("Refusing to write CSV: readings disagree (resolve first).")
            return 1
        out = (
            Path(__file__).resolve().parent.parent
            / "hms_commander"
            / "data"
            / ("texas_empirical_hyetographs.csv")
        )
        write_csv(a_blocks, out, Path(args.pdf).name)
        print("wrote", out)
    return 0 if not (n_diff or ocr_bad) else 2


if __name__ == "__main__":
    sys.exit(main())
