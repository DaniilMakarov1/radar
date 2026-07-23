"""Funding carry research and paper-execution pipeline."""

from smart_money_radar.funding.models import FundingScanConfig

__all__ = ["FundingScanConfig", "run_funding_scan"]


def __getattr__(name: str):
    if name == "run_funding_scan":
        from smart_money_radar.funding.service import run_funding_scan

        return run_funding_scan
    raise AttributeError(name)
