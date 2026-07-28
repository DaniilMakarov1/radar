from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "radar.sqlite"
FUNDING_MINIMUM_ACTIONABLE_NOTIONAL = 500.0
SCHEMA_PATH = PROJECT_ROOT / "warehouse" / "schema.sql"
DEFAULT_REPORT_PATH = PROJECT_ROOT / "reports" / "latest.md"
DEFAULT_REPORT_HTML_PATH = PROJECT_ROOT / "reports" / "latest.html"
DUNE_API_BASE_URL = "https://api.dune.com/api/v1"


API_ENV_VARS = (
    "DUNE_API_KEY",
    "DEXSCREENER_API_KEY",
    "ETHERSCAN_API_KEY",
    "BASESCAN_API_KEY",
    "BSCSCAN_API_KEY",
    "MORALIS_API_KEY",
    "HYPERSYNC_API_TOKEN",
    "GOPLUS_API_KEY",
    "SOLSCAN_API_KEY",
    "HELIUS_API_KEY",
    "BIRDEYE_API_KEY",
    "NEYNAR_API_KEY",
    "GITHUB_TOKEN",
    "X_BEARER_TOKEN",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "FUNDING_TELEGRAM_BOT_TOKEN",
    "FUNDING_TELEGRAM_CHAT_ID",
    "FUNDING_SHADOW_TELEGRAM_BOT_TOKEN",
    "FUNDING_SHADOW_TELEGRAM_CHAT_ID",
)


def load_env_file(path: Path | None = None) -> dict[str, str]:
    env_path = path or PROJECT_ROOT / ".env"
    loaded: dict[str, str] = {}
    if not env_path.exists():
        return loaded

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
            loaded[key] = value
    return loaded


def api_key_status() -> dict[str, bool]:
    load_env_file()
    return {key: bool(os.environ.get(key)) for key in API_ENV_VARS}


def env_flag(name: str, default: bool = False) -> bool:
    load_env_file()
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def x_social_gate_enabled() -> bool:
    return env_flag("X_SOCIAL_GATE_ENABLED", default=False)


@dataclass(frozen=True)
class ChainConfig:
    chain_id: str
    name: str
    ecosystem: str
    explorer_name: str
    explorer_url: str
    api_env_var: str | None
    live_priority: int
    research_enabled: bool = True


CHAINS: tuple[ChainConfig, ...] = (
    ChainConfig(
        chain_id="base",
        name="Base",
        ecosystem="evm",
        explorer_name="BaseScan",
        explorer_url="https://basescan.org",
        api_env_var="BASESCAN_API_KEY",
        live_priority=1,
    ),
    ChainConfig(
        chain_id="solana",
        name="Solana",
        ecosystem="svm",
        explorer_name="Solscan",
        explorer_url="https://solscan.io",
        api_env_var="SOLSCAN_API_KEY",
        live_priority=2,
    ),
    ChainConfig(
        chain_id="bsc",
        name="BNB Chain",
        ecosystem="evm",
        explorer_name="BscScan",
        explorer_url="https://bscscan.com",
        api_env_var="BSCSCAN_API_KEY",
        live_priority=3,
    ),
    ChainConfig(
        chain_id="ethereum",
        name="Ethereum",
        ecosystem="evm",
        explorer_name="Etherscan",
        explorer_url="https://etherscan.io",
        api_env_var="ETHERSCAN_API_KEY",
        live_priority=4,
    ),
)
