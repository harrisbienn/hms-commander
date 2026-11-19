"""No-compute admission evidence for provisional static HMS initialization."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from .Decorators import log_call
from .LoggingConfig import get_logger
from . import _forecast_initialization as initialization

logger = get_logger(__name__)


class HmsForecastInitialization:
    """Verify selected model inputs without executing HMS or opening DSS natively."""

    @staticmethod
    @log_call
    def prepare(request_path: str | Path, evidence_path: str | Path) -> Path:
        """Freeze deterministic source-admission evidence for a request 1.4.

        This validates source inputs only. The scenario worker separately checks
        the prepared clone immediately before execution. No workspace is created.

        Args:
            request_path: Scenario-worker request 1.4 JSON file.
            evidence_path: New receipt destination; identical evidence may be reused.

        Returns:
            Absolute path to the immutable source-admission receipt.

        Raises:
            ValueError: Initialization is unsupported, stale, or inconsistent.
            FileExistsError: The destination contains different evidence.
            OSError: Required input files cannot be read or the receipt written.

        Example:
            >>> receipt = HmsForecastInitialization.prepare("request.json", "receipt.json")
        """
        evidence = HmsForecastInitialization._current(request_path)
        destination = Path(evidence_path).resolve()
        if destination.exists():
            if initialization._canonical(
                initialization._json(destination)
            ) != initialization._canonical(evidence):
                raise FileExistsError(
                    "Initialization receipt already contains different evidence"
                )
            return destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=destination.parent, suffix=".tmp", delete=False
            ) as stream:
                temporary = Path(stream.name)
                stream.write(initialization._canonical(evidence) + b"\n")
                stream.flush()
                os.fsync(stream.fileno())
            # A hard link publishes complete bytes without replacing a concurrent writer.
            os.link(temporary, destination)
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    logger.warning(
                        "Could not remove owned initialization receipt temporary file: %s",
                        temporary,
                    )
        return destination

    @staticmethod
    @log_call
    def verify(request_path: str | Path, evidence_path: str | Path) -> dict[str, Any]:
        """Reauthenticate current source inputs against a retained receipt.

        Args:
            request_path: Scenario-worker request 1.4 JSON file.
            evidence_path: Source receipt previously produced by ``prepare``.

        Returns:
            Verified source-admission evidence, with forecast eligibility false.

        Raises:
            ValueError: Source identities or retained evidence differ.
            OSError: A required file is unavailable.

        Example:
            >>> evidence = HmsForecastInitialization.verify("request.json", "receipt.json")
        """
        current = HmsForecastInitialization._current(request_path)
        retained = initialization._json(Path(evidence_path))
        if initialization._canonical(current) != initialization._canonical(retained):
            raise ValueError(
                "Initialization receipt differs from current request inputs"
            )
        return current

    @staticmethod
    def _current(request_path: str | Path) -> dict[str, Any]:
        from .HmsScenarioWorker import HmsScenarioWorker

        request = HmsScenarioWorker._validate_request(
            initialization._json(Path(request_path))
        )
        if request["schema"] != HmsScenarioWorker.FORECAST_REQUEST_SCHEMA:
            raise ValueError(
                "Forecast initialization requires scenario-worker request 1.4"
            )
        HmsScenarioWorker._verify_input_identities(request)
        return initialization.verify_source(request)
