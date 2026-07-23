"""Prediction-market research, arbitrage scanning, and paper execution."""

__all__ = ["run_prediction_scan"]


def run_prediction_scan(*args, **kwargs):
    from smart_money_radar.prediction.service import run_prediction_scan as _run

    return _run(*args, **kwargs)
