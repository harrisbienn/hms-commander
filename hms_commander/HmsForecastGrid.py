"""Materialize authenticated rainfall arrays through the isolated DSS owner."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from .Decorators import log_call
from ._forecast_grid import (
    RESULT_SCHEMA,
    _Inputs,
    _digest,
    _fields,
    _grid_time,
    _load,
    _number,
    _sha256,
    _write_new,
)
from ._process import _isolated_worker_command, dss_environment, run_owned

logger = logging.getLogger(__name__)


def _validate_result(value: dict[str, Any], inputs: _Inputs, *, accepted: bool) -> None:
    fields = {
        "schema",
        "status",
        "request_sha256",
        "source_sha256",
        "domain_sha256",
        "records",
        "record_count",
        "units",
        "data_type",
        "clock",
        "grid",
        "interval_seconds",
        "absolute_value_tolerance_mm",
        "forecast_eligible",
    }
    fields.update({"output", "process_quiescent"} if accepted else {"staged_output"})
    _fields(value, fields, "Materialization result")
    expected = {
        "schema": RESULT_SCHEMA,
        "status": "succeeded" if accepted else "readback_verified",
        "request_sha256": inputs.request_sha256,
        "source_sha256": inputs.request["source"]["sha256"],
        "domain_sha256": inputs.request["expected_domain_sha256"],
        "record_count": len(inputs.frames),
        "units": "MM",
        "data_type": "PER-CUM",
        "clock": "UTC",
        "grid": inputs.source["domain"]["grid"],
        "interval_seconds": inputs.source["interval_seconds"],
        "absolute_value_tolerance_mm": inputs.tolerance,
        "forecast_eligible": False,
    }
    for key, expected_value in expected.items():
        if _digest(value[key]) != _digest(expected_value):
            raise ValueError(f"Materialization result {key} differs from the request")
    records = value["records"]
    if not isinstance(records, list) or len(records) != len(inputs.frames):
        raise ValueError("Materialization evidence must account for every interval")
    selector = inputs.request["pathname"].split("/")
    for index, record in enumerate(records):
        _fields(
            record,
            {
                "pathname",
                "start",
                "end",
                "required_cell_count",
                "outside_nodata_cells",
                "max_absolute_error_mm",
            },
            "Readback record",
        )
        parts = record["pathname"].split("/")
        if len(parts) != 8 or any(
            parts[key] != selector[key] for key in (0, 1, 2, 3, 6, 7)
        ):
            raise ValueError("Materialization record has a different pathname family")
        if (_grid_time(parts[4]), _grid_time(parts[5])) != tuple(
            instant.replace(tzinfo=None) for instant in inputs.times[index : index + 2]
        ):
            raise ValueError("Materialization record has a different pathname interval")
        if [record["start"], record["end"]] != inputs.source["times"][
            index : index + 2
        ]:
            raise ValueError("Materialization record has a different source interval")
        for key, count in (
            ("required_cell_count", int(inputs.mask.sum())),
            ("outside_nodata_cells", int((~inputs.mask).sum())),
        ):
            if type(record[key]) is not int or record[key] != count:
                raise ValueError("Materialization record has different domain coverage")
        error = _number(record["max_absolute_error_mm"], "Readback error")
        if not 0 <= error <= inputs.tolerance:
            raise ValueError("Materialization record exceeds its readback tolerance")
    if accepted:
        if value["process_quiescent"] is not True:
            raise ValueError("Materialization process ownership was not reconciled")
        output = _fields(value["output"], {"path", "sha256", "size_bytes"}, "Output")
        if output["path"] != inputs.request["output_dss"]:
            raise ValueError("Materialization output path differs from the request")
        if (
            not inputs.output.is_file()
            or type(output["size_bytes"]) is not int
            or output["size_bytes"] <= 0
            or inputs.output.stat().st_size != output["size_bytes"]
            or _sha256(inputs.output) != output["sha256"]
        ):
            raise ValueError("Materialized DSS identity does not match")


class HmsForecastGrid:
    """Write full-domain rainfall arrays and retain checked materialization evidence.

    This namespace consumes an upstream preparation descriptor. Source-owner replay
    and observed-prefix assembly must happen before preparing that descriptor;
    neither a descriptor nor successful DSS writing proves forecast skill.
    """

    @staticmethod
    @log_call
    def verify(request_path: str | Path, result_path: str | Path) -> dict[str, Any]:
        """Authenticate a completed product without opening native DSS code.

        Args:
            request_path: Version 2 request JSON with a pinned preparation descriptor.
            result_path: Accepted result JSON produced by ``materialize``.

        Returns:
            Result dictionary after checking source and output bytes, interval and
            grid evidence, full-domain coverage, tolerance, and process quiescence.

        Raises:
            ValueError: An identity or result field differs from the request.
            OSError: A required input or result cannot be read.

        Example:
            >>> evidence = HmsForecastGrid.verify("request.json", "result.json")
        """
        inputs = _load(Path(request_path).resolve())
        value = json.loads(Path(result_path).read_bytes())
        _validate_result(value, inputs, accepted=True)
        return value

    @staticmethod
    @log_call
    def materialize(
        request_path: str | Path,
        result_path: str | Path,
        *,
        timeout: float = 600,
    ) -> dict[str, Any]:
        """Write and verify one complete UTC rainfall window in an owned child.

        The version 2 request binds a generic preparation descriptor, an independently
        selected domain hash, an exact window, a DSS selector, and a caller-selected
        absolute readback tolerance in millimetres. Paths inside requests and source
        descriptors resolve against their containing JSON file. The native worker
        uses the configured RAS DSS trust policy and installed companion package.

        Args:
            request_path: Version 2 grid-writer request JSON. Arrays contain interval
                depths in millimetres, with south-to-north rows and NaN outside the
                explicit required mask. Required cells cannot contain missing data.
            result_path: New result JSON path. An identical accepted retry verifies
                and returns this result; conflicting or partial attempts are refused.
            timeout: Positive child time limit in seconds. Defaults to 600.

        Returns:
            Accepted result dictionary with per-interval readback evidence, pinned
            input identities, output SHA256, and reconciled process ownership.
            ``forecast_eligible`` remains false.

        Raises:
            ValueError: Input, readback evidence, or an existing result is invalid.
            FileExistsError: An output, attempt receipt, or active reservation exists.
            TimeoutError: The native child exceeds its time limit.
            RuntimeError: The child fails or process ownership cannot be reconciled.
            OSError: Required files cannot be read or products cannot be published.

        Example:
            >>> evidence = HmsForecastGrid.materialize("request.json", "result.json")
        """
        limit = _number(timeout, "Worker timeout")
        if limit <= 0:
            raise ValueError("Worker timeout must be positive")
        request_file = Path(request_path).resolve()
        result_file = Path(result_path).resolve()
        inputs = _load(request_file)
        receipt_file = result_file.with_name(result_file.name + ".worker.json")
        reservation = result_file.with_name(result_file.name + ".lock")
        destinations = {result_file, receipt_file, reservation, inputs.output}
        if len(destinations) != 4 or destinations & inputs.protected_paths:
            raise ValueError(
                "Materialization paths must not alias each other or inputs"
            )
        result_file.parent.mkdir(parents=True, exist_ok=True)
        # Atomic reservation makes concurrent identical attempts explicit. It is not
        # a lease: abandoned reservations require inspection rather than theft.
        with reservation.open("x", encoding="utf-8") as stream:
            stream.write(inputs.request_sha256 + "\n")
        release_reservation = True
        try:
            if result_file.exists():
                return HmsForecastGrid.verify(request_file, result_file)
            if inputs.output.exists() or receipt_file.exists():
                raise FileExistsError("Refusing partial or conflicting materialization")
            # A launch/cleanup exception can mean descendants still own native
            # resources. Keep the reservation until quiescence is established.
            release_reservation = False
            completed = run_owned(
                _isolated_worker_command(
                    "grid",
                    "--request",
                    str(request_file),
                    "--result",
                    str(receipt_file),
                ),
                timeout=limit,
                cwd=request_file.parent,
                output_dir=result_file.parent,
                env=dss_environment(),
            )
            release_reservation = completed.quiescent
            if completed.timed_out:
                raise TimeoutError(f"Forecast grid worker exceeded {limit} seconds")
            if not completed.quiescent:
                raise RuntimeError(
                    "Forecast grid worker process ownership is not reconciled"
                )
            if completed.returncode != 0 or not receipt_file.is_file():
                raise RuntimeError(
                    f"Forecast grid worker failed; inspect {receipt_file}; "
                    f"exit={completed.returncode}"
                )
            receipt = json.loads(receipt_file.read_bytes())
            current = _load(request_file)
            if current.request_sha256 != inputs.request_sha256:
                raise ValueError("Request changed during worker execution")
            _validate_result(receipt, inputs, accepted=False)
            staged = Path(receipt.pop("staged_output")).resolve()
            if (
                staged.parent.parent != inputs.output.parent
                or not staged.parent.name.startswith(".forecast-grid-")
                or staged.name != "grid.dss"
                or staged in inputs.protected_paths
                or not staged.is_file()
                or staged.stat().st_size <= 0
            ):
                raise ValueError(
                    "Worker staged DSS is outside its owned output location"
                )
            # Hash and publish only after the owning child and descendants exit.
            identity = {
                "path": inputs.request["output_dss"],
                "sha256": _sha256(staged),
                "size_bytes": staged.stat().st_size,
            }
            os.link(staged, inputs.output)
            receipt.update(status="succeeded", output=identity, process_quiescent=True)
            _validate_result(receipt, inputs, accepted=True)
            _write_new(result_file, receipt)
            try:
                staged.unlink()
                staged.parent.rmdir()
            except OSError:
                logger.warning(
                    "Accepted forecast grid; staging evidence remains at %s",
                    staged.parent,
                    exc_info=True,
                )
            return receipt
        finally:
            if release_reservation:
                try:
                    reservation.unlink()
                except OSError:
                    logger.warning(
                        "Forecast grid reservation remains at %s; inspect ownership before retry",
                        reservation,
                        exc_info=True,
                    )
            else:
                logger.error(
                    "Forecast grid ownership is unknown; reservation retained at %s",
                    reservation,
                )
