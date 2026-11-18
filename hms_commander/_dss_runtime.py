"""Companion capability admission; native verification stays with its owner."""

RAS_DSS_TRUST_POLICY = "ras-commander/dss-runtime-trust/1.0.0"


def require_ras_dss_policy() -> None:
    """Refuse companions that silently ignore the selected native trust policy."""
    from ras_commander.dss._hec_monolith import HecMonolithDownloader

    if getattr(HecMonolithDownloader, "TRUST_POLICY", None) != RAS_DSS_TRUST_POLICY:
        raise RuntimeError(
            "HMS grid operations require RAS DSS runtime trust policy "
            f"{RAS_DSS_TRUST_POLICY}; install a qualified companion revision "
            "before native execution"
        )
