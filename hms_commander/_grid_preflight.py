"""Private isolated RAS-owned reads for external HMS grid declarations."""

import argparse
import hashlib
import json
import logging
import sys
import tempfile
from pathlib import Path

from ._dss_runtime import require_ras_dss_policy
from ._process import dss_environment, run_owned

logger = logging.getLogger(__name__)

REQUEST_SCHEMA = "hms-commander/external-grid-preflight-request/1.0"
RESULT_SCHEMA = "hms-commander/external-grid-preflight-result/1.0"
MAX_REQUEST_BYTES = 1024 * 1024
MAX_RESULT_BYTES = 8 * 1024 * 1024


def _encoded(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _load(path: Path, limit: int) -> dict:
    with path.open("rb") as stream:
        encoded = stream.read(limit + 1)
    if len(encoded) > limit:
        raise ValueError("Grid preflight JSON exceeds its byte limit")

    def unique_fields(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate grid preflight JSON field")
            result[key] = value
        return result

    payload = json.loads(encoded, object_pairs_hook=unique_fields)
    if not isinstance(payload, dict):
        raise ValueError("Grid preflight JSON must be an object")
    return payload


def _validate_request(payload: dict) -> list[dict]:
    from ._external_dss import _matched_paths

    if set(payload) != {"schema", "references"} or payload["schema"] != REQUEST_SCHEMA:
        raise ValueError("Unsupported grid preflight request")
    references = payload["references"]
    if not isinstance(references, list) or not references:
        raise ValueError("Grid preflight requires at least one reference")
    seen = set()
    for reference in references:
        if not isinstance(reference, dict) or set(reference) != {
            "reference_index",
            "dss_file",
            "selector",
        }:
            raise ValueError("Invalid grid preflight reference fields")
        index = reference["reference_index"]
        if type(index) is not int or index < 0 or index in seen:
            raise ValueError(
                "Grid preflight reference index must be unique and nonnegative"
            )
        seen.add(index)
        source = reference["dss_file"]
        selector = reference["selector"]
        if not isinstance(source, str) or not source or not Path(source).is_absolute():
            raise ValueError("Grid preflight source must be an absolute path")
        if not isinstance(selector, str):
            raise ValueError("Grid preflight selector must be a string")
        _matched_paths(selector, [])
    return references


def _validate_result(payload: dict, request: dict, digest: str) -> list[dict]:
    from ._external_dss import _matched_paths

    if set(payload) != {"schema", "request_sha256", "results"}:
        raise ValueError("Invalid grid preflight result fields")
    if payload["schema"] != RESULT_SCHEMA or payload["request_sha256"] != digest:
        raise ValueError("Grid preflight result does not match its request")
    results = payload["results"]
    references = request["references"]
    if not isinstance(results, list) or len(results) != len(references):
        raise ValueError("Grid preflight result must account for every reference")
    for result, reference in zip(results, references):
        if not isinstance(result, dict) or set(result) != {
            "reference_index",
            "matched_pathnames",
            "records_read",
            "status",
            "issues",
        }:
            raise ValueError("Invalid grid preflight reference result")
        if (
            type(result["reference_index"]) is not int
            or result["reference_index"] != reference["reference_index"]
        ):
            raise ValueError("Grid preflight result reference identity changed")
        matches = result["matched_pathnames"]
        issues = result["issues"]
        count = result["records_read"]
        if not isinstance(matches, list) or not all(
            isinstance(path, str) for path in matches
        ):
            raise ValueError("Grid preflight pathnames must be strings")
        if any(
            not path.startswith("/") or not path.endswith("/") or path.count("/") != 7
            for path in matches
        ):
            raise ValueError("Grid preflight returned an invalid DSS pathname")
        if _matched_paths(reference["selector"], matches) != matches:
            raise ValueError("Grid preflight returned duplicate or unmatched pathnames")
        if type(count) is not int or not 0 <= count <= len(matches):
            raise ValueError("Grid preflight returned an invalid read count")
        if not isinstance(issues, list) or not all(
            isinstance(issue, str) and issue for issue in issues
        ):
            raise ValueError("Grid preflight issues must be nonempty strings")
        if result["status"] == "passed":
            if not matches or count != len(matches) or issues:
                raise ValueError("Grid preflight pass lacks complete read evidence")
        elif result["status"] != "failed" or not issues:
            raise ValueError("Grid preflight failure lacks an issue")
    return results


def read_references(references: list[dict]) -> list[dict]:
    """Read a grid batch in one fresh owned child and retain attempt evidence."""
    request = {"schema": REQUEST_SCHEMA, "references": references}
    _validate_request(request)
    encoded = _encoded(request)
    if len(encoded) > MAX_REQUEST_BYTES:
        raise ValueError("Grid preflight request exceeds its byte limit")
    digest = hashlib.sha256(encoded).hexdigest()
    # Retain even successful attempts. Unknown child quiescence must never race
    # automatic recursive cleanup of files a native process may still hold.
    attempt = Path(tempfile.mkdtemp(prefix="hms-grid-preflight-"))
    request_path = attempt / "request.json"
    result_path = attempt / "result.json"
    request_path.write_bytes(encoded)
    try:
        completed = run_owned(
            [
                sys.executable,
                "-I",
                "-m",
                "hms_commander._grid_preflight",
                "--request",
                str(request_path),
                "--result",
                str(result_path),
            ],
            timeout=600,
            cwd=attempt,
            output_dir=attempt,
            env=dss_environment(),
        )
        if completed.timed_out:
            raise TimeoutError("Grid preflight exceeded 600 seconds")
        if not completed.quiescent:
            raise RuntimeError("Grid preflight process ownership is not reconciled")
        if completed.returncode != 0:
            raise RuntimeError(
                f"Grid preflight worker failed with exit code {completed.returncode}"
            )
        results = _validate_result(
            _load(result_path, MAX_RESULT_BYTES), request, digest
        )
    except Exception as error:
        raise RuntimeError(
            f"{error}; grid preflight evidence retained at {attempt}"
        ) from error
    logger.debug("Grid preflight evidence retained at %s", attempt)
    return results


def _read_references(references: list[dict]) -> list[dict]:
    """Use one RAS owner for both catalog and grid reads inside the child."""
    from ._external_dss import _matched_paths

    results = []
    catalogs = {}
    for reference in references:
        result = {
            "reference_index": reference["reference_index"],
            "matched_pathnames": [],
            "records_read": 0,
            "status": "failed",
            "issues": [],
        }
        results.append(result)
        try:
            require_ras_dss_policy()
            from ras_commander.dss import RasDss

            source = Path(reference["dss_file"])
            if not source.is_file() or source.stat().st_size == 0:
                raise ValueError("External DSS file is missing or empty")
            if source not in catalogs:
                catalogs[source] = RasDss.get_catalog(source)["pathname"].tolist()
            matches = _matched_paths(reference["selector"], catalogs[source])
            result["matched_pathnames"] = matches
            if not matches:
                raise ValueError("External DSS selector matches no cataloged pathname")
            for pathname in matches:
                RasDss.read_grid(source, pathname)
                result["records_read"] += 1
            result["status"] = "passed"
        except Exception as error:
            # Preserve every declared reference, including partial reads. A
            # failure on one grid must not make another disappear from evidence.
            result["issues"].append(f"{type(error).__name__}: {error}")
    return results


def run(request_path: str | Path, result_path: str | Path) -> int:
    """Validate the private request and publish new, request-bound read evidence."""
    try:
        request = _load(Path(request_path), MAX_REQUEST_BYTES)
        references = _validate_request(request)
        result_path = Path(result_path)
        if result_path.exists():
            raise FileExistsError("Refusing to replace grid preflight evidence")
        digest = hashlib.sha256(_encoded(request)).hexdigest()
        payload = {
            "schema": RESULT_SCHEMA,
            "request_sha256": digest,
            "results": _read_references(references),
        }
        _validate_result(payload, request, digest)
        encoded = _encoded(payload)
        if len(encoded) > MAX_RESULT_BYTES:
            raise ValueError("Grid preflight result exceeds its byte limit")
        with result_path.open("xb") as stream:
            stream.write(encoded)
        return 0
    except Exception as error:
        logger.error("Grid preflight worker failed: %s", error)
        return 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True)
    parser.add_argument("--result", required=True)
    args = parser.parse_args()
    return run(args.request, args.result)


if __name__ == "__main__":
    raise SystemExit(main())
