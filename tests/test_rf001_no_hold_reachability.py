from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _term(*parts: str) -> str:
    return "".join(parts)


PRODUCTION_FILES = [
    "smart_money_radar/cli.py",
    "smart_money_radar/funding/trader.py",
    "smart_money_radar/funding/shadow_monitor.py",
    "smart_money_radar/funding/strategy_synchronized_funding.py",
    "smart_money_radar/paper_bot/cycle_manager.py",
    "smart_money_radar/paper_bot/runtime_v2.py",
    "scripts/check_verified_paper_launch_gate.py",
    ".github/workflows/ci.yml",
]


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def test_rf001_forbidden_continuation_symbols_are_not_in_production_paths() -> None:
    forbidden = [
        _term("HOLDING", "_NEXT", "_CYCLE"),
        _term("Hold", "History", "Reliability"),
        _term("evaluate", "_hold", "_history", "_reliability"),
        _term("next", "_cycle", "_schedule", "_decision"),
        _term("next", "_cycle", "_observation", "_decision"),
        _term("hold", "_economics"),
        _term("exit", "_after", "_second", "_settlement"),
        _term("exit", "_after", "_third", "_settlement"),
        _term("max", "_strategy", "_hold", "_seconds"),
    ]

    offenders: list[str] = []
    for path in PRODUCTION_FILES:
        text = _read(path)
        offenders.extend(f"{path}: {term}" for term in forbidden if term in text)

    assert offenders == []


def test_rf001_removed_hold_configuration_is_not_exposed() -> None:
    forbidden = [
        _term("hold", "_enabled"),
        _term("max", "_settlements", "_per", "_position"),
        _term("max", "_position", "_age", "_seconds"),
        _term("post", "_settlement", "_hold", "_decision", "_seconds"),
        _term("post", "_settlement", "_schedule", "_probe", "_seconds"),
        _term("max", "_gap", "_between", "_settlements", "_seconds"),
        _term("min", "_next", "_settlement", "_wait", "_seconds"),
        _term("max", "_next", "_settlement", "_wait", "_seconds"),
    ]
    config_files = [
        "smart_money_radar/cli.py",
        "smart_money_radar/funding/trader.py",
        "smart_money_radar/funding/profiles.py",
        "smart_money_radar/funding/shadow_monitor.py",
        "scripts/check_verified_paper_launch_gate.py",
    ]

    offenders: list[str] = []
    for path in config_files:
        text = _read(path)
        offenders.extend(f"{path}: {term}" for term in forbidden if term in text)

    assert offenders == []


def test_rf001_status_surfaces_do_not_offer_hold_actions() -> None:
    forbidden = [
        _term("Paper", " Bot ", "HOLD"),
        _term("hold", "_message"),
        _term("HOLDING", "_NEXT", "_CYCLE"),
        _term("next", "_cycle", "_hold", "_or", "_close", "_decision"),
    ]
    surface_files = [
        "smart_money_radar/dashboard.py",
        "smart_money_radar/paper_bot/telegram.py",
        "smart_money_radar/web/index.html",
    ]

    offenders: list[str] = []
    for path in surface_files:
        text = _read(path)
        offenders.extend(f"{path}: {term}" for term in forbidden if term in text)

    assert offenders == []
