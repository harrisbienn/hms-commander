"""Tests for the versioned hydrologic handoff worker boundary."""

import hashlib
import json
from pathlib import Path

import pytest

from hms_commander import HmsHandoffWorker, HmsResultsProducts

SOURCE_PATH = "//OUTLET/FLOW//5Minute/RUN:TEST/"
OUTPUT_PATH = "//UPSTREAM/FLOW//5Minute/HANDOFF:TEST/"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _identity(path: Path) -> dict[str, object]:
    return {
        "path": str(path.resolve()),
        "size_bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }


def _request(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    source = tmp_path / "source.dss"
    source.write_bytes(b"source")
    output = tmp_path / "handoff"
    request = {
        "schema": "hms-commander/hydrologic-handoff-request/1.0",
        "operation_id": "scenario-001-hydrologic-handoff",
        "model_window": {
            "start": "2019-09-18T13:00:00",
            "end": "2019-09-18T13:10:00",
        },
        "output_directory": str(output),
        "qualification": {
            "sentinel_threshold": -1.0e30,
            "maximum_final_to_peak_ratio": 1.0,
            "minimum_post_peak_hours": 0.0,
        },
        "mappings": [
            {
                "mapping_id": "boundary-001",
                "source_asset_id": "hms-output",
                "source_dss": str(source),
                "source_sha256": _sha256(source),
                "source_pathname": SOURCE_PATH,
                "output_pathname": OUTPUT_PATH,
                "source_units": "CFS",
                "target_units": "CFS",
                "value_type": "INST-VAL",
                "interval_minutes": 5,
                "conversion": "identity",
                "multiplier": 1.0,
                "offset": 0.0,
            }
        ],
    }
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    return request_path, tmp_path / "result.json", output, source


def _install_materializer(monkeypatch) -> list[dict[str, object]]:
    calls = []

    def materialize(mappings, output_directory, **kwargs):
        calls.append({"mappings": mappings, **kwargs})
        output = Path(output_directory).resolve()
        products = output / "products"
        products.mkdir(parents=True)
        dss = output / "hydrologic-handoff.dss"
        dss.write_bytes(b"consolidated-dss")
        dss_hash = _sha256(dss)
        model_window = {
            "start": kwargs["model_start"],
            "end": kwargs["model_end"],
        }
        status = {"all_required_pathnames_valid": True}
        provenance = output / "hydrologic-handoff-provenance.json"
        provenance.write_text(
            json.dumps(
                {
                    "schema": HmsResultsProducts.HANDOFF_SCHEMA,
                    "output": {"sha256": dss_hash},
                    "model_window": model_window,
                    "mappings": [
                        HmsResultsProducts._portable_handoff_mapping(mapping)
                        for mapping in mappings
                    ],
                }
            ),
            encoding="utf-8",
        )
        product = products / "hydrologic-products.json"
        product.write_text(
            json.dumps(
                {
                    "schema": HmsResultsProducts.SCHEMA,
                    "source": {"sha256": dss_hash},
                    "time": model_window,
                    "status": status,
                }
            ),
            encoding="utf-8",
        )
        return {
            "directory": str(output),
            "dss": _identity(dss),
            "product_manifest": _identity(product),
            "provenance_manifest": _identity(provenance),
            "boundary_pathnames": {"boundary-001": OUTPUT_PATH},
            "status": status,
        }

    monkeypatch.setattr(
        HmsResultsProducts,
        "materialize_handoff",
        staticmethod(materialize),
    )
    return calls


def test_worker_materializes_and_verifies_identical_reuse(monkeypatch, tmp_path):
    request, result, output, _source = _request(tmp_path)
    calls = _install_materializer(monkeypatch)

    assert HmsHandoffWorker.run(request, result) == 0
    assert HmsHandoffWorker.run(request, result) == 0

    payload = json.loads(result.read_text(encoding="utf-8"))
    assert payload["status"] == "succeeded"
    assert payload["directory"] == str(output.resolve())
    assert payload["boundary_pathnames"] == {"boundary-001": OUTPUT_PATH}
    assert payload["assets"]["dss"]["sha256"] == _sha256(
        output / "hydrologic-handoff.dss"
    )
    assert len(calls) == 1


def test_worker_refuses_changed_request_against_existing_result(monkeypatch, tmp_path):
    request, result, _output, _source = _request(tmp_path)
    _install_materializer(monkeypatch)
    assert HmsHandoffWorker.run(request, result) == 0
    original_result = result.read_bytes()
    payload = json.loads(request.read_text(encoding="utf-8"))
    payload["operation_id"] = "changed"
    request.write_text(json.dumps(payload), encoding="utf-8")

    assert HmsHandoffWorker.run(request, result) == 3
    assert result.read_bytes() == original_result


def test_worker_detects_published_asset_tampering(monkeypatch, tmp_path):
    request, result, output, _source = _request(tmp_path)
    _install_materializer(monkeypatch)
    assert HmsHandoffWorker.run(request, result) == 0
    with (output / "hydrologic-handoff.dss").open("ab") as stream:
        stream.write(b"tampered")

    assert HmsHandoffWorker.run(request, result) == 3


def test_worker_rejects_tampered_result_shape(monkeypatch, tmp_path):
    request, result, _output, _source = _request(tmp_path)
    _install_materializer(monkeypatch)
    assert HmsHandoffWorker.run(request, result) == 0
    payload = json.loads(result.read_text(encoding="utf-8"))
    payload["unexpected"] = True
    result.write_text(json.dumps(payload), encoding="utf-8")

    assert HmsHandoffWorker.run(request, result) == 3


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("unknown", "unknown unexpected"),
        ("collision", "output pathname collision"),
        ("timestamp", "ISO timestamp string"),
    ],
)
def test_worker_rejects_invalid_request_before_materialization(
    monkeypatch,
    tmp_path,
    mutation,
    message,
):
    request, result, output, _source = _request(tmp_path)
    payload = json.loads(request.read_text(encoding="utf-8"))
    if mutation == "unknown":
        payload["unexpected"] = True
    elif mutation == "collision":
        payload["mappings"].append(
            {
                **payload["mappings"][0],
                "mapping_id": "boundary-002",
                "multiplier": 0.5,
                "conversion": "linear",
            }
        )
    elif mutation == "timestamp":
        payload["model_window"]["start"] = 1_568_812_800
    request.write_text(json.dumps(payload), encoding="utf-8")
    calls = _install_materializer(monkeypatch)

    assert HmsHandoffWorker.run(request, result) == 2
    failure = json.loads(result.read_text(encoding="utf-8"))
    assert failure["classification"] == "invalid_request"
    assert message in failure["error"]["message"]
    assert not output.exists()
    assert not calls


def test_worker_records_materialization_failure(monkeypatch, tmp_path):
    request, result, output, _source = _request(tmp_path)

    def fail(*_args, **_kwargs):
        raise RuntimeError("DSS child failed")

    monkeypatch.setattr(
        HmsResultsProducts,
        "materialize_handoff",
        staticmethod(fail),
    )
    assert HmsHandoffWorker.run(request, result) == 4
    failure = json.loads(result.read_text(encoding="utf-8"))
    assert failure["classification"] == "materialization_failed"
    assert not output.exists()


def test_packaged_handoff_schemas_match_public_contract_constants():
    contracts = Path(__file__).resolve().parents[1] / "hms_commander" / "contracts"
    request = json.loads(
        (contracts / "hydrologic-handoff-request-v1.0.schema.json").read_text(
            encoding="utf-8"
        )
    )
    result = json.loads(
        (contracts / "hydrologic-handoff-result-v1.0.schema.json").read_text(
            encoding="utf-8"
        )
    )

    assert request["$id"] == HmsHandoffWorker.REQUEST_SCHEMA
    assert result["$id"] == HmsHandoffWorker.RESULT_SCHEMA
