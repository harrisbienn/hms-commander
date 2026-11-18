"""Private grid preflight contracts with substituted native I/O, never Java."""

import copy
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from hms_commander import _grid_preflight as worker


@pytest.fixture
def grid_request(tmp_path):
    source = tmp_path / "input.dss"
    source.write_bytes(b"synthetic source, not a native DSS")
    payload = {
        "schema": worker.REQUEST_SCHEMA,
        "references": [
            {
                "reference_index": index,
                "dss_file": str(source),
                "selector": f"/A/{name}/PRECIP///F/",
            }
            for index, name in enumerate(["ONE", "TWO"])
        ],
    }
    request_path = tmp_path / "request.json"
    request_path.write_bytes(worker._encoded(payload))
    return request_path, tmp_path / "result.json", payload


def _successful_result(payload):
    return {
        "schema": worker.RESULT_SCHEMA,
        "request_sha256": hashlib.sha256(worker._encoded(payload)).hexdigest(),
        "results": [
            {
                "reference_index": reference["reference_index"],
                "matched_pathnames": [
                    reference["selector"].replace("///", "/01JAN2020/02JAN2020/")
                ],
                "records_read": 1,
                "status": "passed",
                "issues": [],
            }
            for reference in payload["references"]
        ],
    }


@pytest.mark.parametrize("second_corrupt", [False, True])
def test_worker_uses_only_ras_catalog_and_reads_each_reference(
    grid_request, monkeypatch, second_corrupt
):
    request_path, result_path, payload = grid_request
    pathnames = [
        outcome["matched_pathnames"][0]
        for outcome in _successful_result(payload)["results"]
    ]
    calls = []
    monkeypatch.setattr(worker, "require_ras_dss_policy", lambda: None)

    def catalog(source):
        calls.append("catalog")
        return pd.DataFrame({"pathname": pathnames})

    def read(source, pathname):
        calls.append(pathname)
        if second_corrupt and pathname == pathnames[1]:
            raise RuntimeError("DataFormatException: synthetic corrupt grid")
        return {"data": [[1.0]]}

    monkeypatch.setitem(
        sys.modules,
        "ras_commander.dss",
        SimpleNamespace(RasDss=SimpleNamespace(get_catalog=catalog, read_grid=read)),
    )
    assert worker.run(request_path, result_path) == 0
    result = json.loads(result_path.read_text())
    assert (
        result["request_sha256"] == hashlib.sha256(worker._encoded(payload)).hexdigest()
    )
    assert calls == ["catalog", *pathnames]
    assert result["results"][0]["status"] == "passed"
    assert result["results"][1]["status"] == ("failed" if second_corrupt else "passed")
    assert result["results"][1]["records_read"] == (0 if second_corrupt else 1)
    if second_corrupt:
        assert "synthetic corrupt grid" in result["results"][1]["issues"][0]
    assert (
        Path(payload["references"][0]["dss_file"]).read_bytes()
        == b"synthetic source, not a native DSS"
    )


@pytest.mark.parametrize(
    "fault",
    [
        "schema",
        "unknown",
        "empty",
        "duplicate",
        "bool_index",
        "relative",
        "selector",
        "duplicate_json",
        "too_large",
        "existing_result",
    ],
)
def test_worker_refuses_invalid_requests_before_native_io(
    grid_request, monkeypatch, fault
):
    request_path, result_path, payload = grid_request
    monkeypatch.setattr(
        worker,
        "require_ras_dss_policy",
        lambda: pytest.fail("Native admission attempted"),
    )
    if fault == "schema":
        payload["schema"] = "unknown/2"
    elif fault == "unknown":
        payload["unexpected"] = True
    elif fault == "empty":
        payload["references"] = []
    elif fault == "duplicate":
        payload["references"][1]["reference_index"] = 0
    elif fault == "bool_index":
        payload["references"][0]["reference_index"] = True
    elif fault == "relative":
        payload["references"][0]["dss_file"] = "input.dss"
    elif fault == "selector":
        payload["references"][0]["selector"] = "/incomplete/"
    elif fault == "too_large":
        monkeypatch.setattr(worker, "MAX_REQUEST_BYTES", 8)
    elif fault == "existing_result":
        result_path.write_bytes(b"prior evidence")
    encoded = worker._encoded(payload)
    if fault == "duplicate_json":
        encoded = encoded[:-1] + b',"schema":"duplicate"}'
    request_path.write_bytes(encoded)
    assert worker.run(request_path, result_path) == 2
    if fault == "existing_result":
        assert result_path.read_bytes() == b"prior evidence"
    else:
        assert not result_path.exists()


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "timeout",
        "unreconciled",
        "ownership_exception",
        "nonzero",
        "missing",
        "digest",
        "lost_reference",
        "duplicate_reference",
        "reordered",
        "launch_error",
        "bool_count",
        "incomplete_reads",
        "unexpected_path",
        "malformed_path",
        "empty_failure",
        "oversized",
    ],
)
def test_parent_owns_child_validates_evidence_and_retains_attempts(
    grid_request, tmp_path, monkeypatch, fault
):
    _, _, payload = grid_request
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    caller = tmp_path / "caller"
    caller.mkdir()
    monkeypatch.chdir(caller)
    monkeypatch.setenv("RAS_DSS_TRUST_MANIFEST", "approved.json")
    monkeypatch.setenv("RAS_DSS_CACHE_DIR", "cache")
    monkeypatch.setenv("RAS_DSS_ALLOW_DOWNLOADS", "0")
    monkeypatch.setenv("JAVA_TOOL_OPTIONS", "untrusted")
    monkeypatch.setattr(worker.tempfile, "mkdtemp", lambda **kwargs: str(attempt))
    seen = []

    def run(command, **options):
        seen.append(command)
        assert command[:4] == [
            sys.executable,
            "-I",
            "-m",
            "hms_commander._grid_preflight",
        ]
        assert options["cwd"] == options["output_dir"] == attempt
        assert options["timeout"] == 600
        assert options["env"]["RAS_DSS_TRUST_MANIFEST"] == str(caller / "approved.json")
        assert options["env"]["RAS_DSS_CACHE_DIR"] == str(caller / "cache")
        assert options["env"]["RAS_DSS_ALLOW_DOWNLOADS"] == "0"
        assert "JAVA_TOOL_OPTIONS" not in options["env"]
        actual = json.loads(Path(command[5]).read_text())
        assert actual == payload
        if fault == "ownership_exception":
            raise RuntimeError("owned processes are not quiescent")
        if fault == "launch_error":
            raise OSError("synthetic child launch failed")
        result = _successful_result(actual)
        if fault == "digest":
            result["request_sha256"] = "0" * 64
        elif fault == "lost_reference":
            result["results"].pop()
        elif fault == "duplicate_reference":
            result["results"][1]["reference_index"] = 0
        elif fault == "reordered":
            result["results"].reverse()
        elif fault == "bool_count":
            result["results"][0]["records_read"] = True
        elif fault == "incomplete_reads":
            result["results"][0]["records_read"] = 0
        elif fault == "unexpected_path":
            result["results"][0]["matched_pathnames"] = ["/WRONG/B/C/D/E/F/"]
        elif fault == "malformed_path":
            result["results"][0]["matched_pathnames"] = [
                "XA/ONE/PRECIP/01JAN2020/02JAN2020/FY"
            ]
        elif fault == "empty_failure":
            result["results"][0]["status"] = "failed"
        elif fault == "oversized":
            monkeypatch.setattr(worker, "MAX_RESULT_BYTES", 8)
        if fault != "missing":
            Path(command[7]).write_bytes(worker._encoded(result))
        return SimpleNamespace(
            returncode=2 if fault == "nonzero" else 0,
            timed_out=fault == "timeout",
            quiescent=fault != "unreconciled",
        )

    monkeypatch.setattr(worker, "run_owned", run)
    if fault is None:
        assert (
            worker.read_references(payload["references"])
            == _successful_result(payload)["results"]
        )
    else:
        with pytest.raises(RuntimeError, match="evidence retained"):
            worker.read_references(payload["references"])
    assert len(seen) == 1
    assert attempt.is_dir() and (attempt / "request.json").is_file()


def test_parent_refuses_request_budget_before_creating_attempt(
    grid_request, monkeypatch
):
    _, _, payload = grid_request
    monkeypatch.setattr(worker, "MAX_REQUEST_BYTES", 8)
    monkeypatch.setattr(
        worker.tempfile, "mkdtemp", lambda **kwargs: pytest.fail("Attempt created")
    )
    with pytest.raises(ValueError, match="byte limit"):
        worker.read_references(copy.deepcopy(payload["references"]))
