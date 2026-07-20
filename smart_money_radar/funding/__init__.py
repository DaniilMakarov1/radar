"""Funding carry research and paper-execution pipeline."""

from smart_money_radar.funding.models import FundingScanConfig
from smart_money_radar.funding.service import run_funding_scan

__all__ = ["FundingScanConfig", "run_funding_scan"]
