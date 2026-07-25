"""Generic funding bot: position management, armed logic, reporting.

``FundingBotBase`` is parameterised entirely by ``FundingBotProfile``.
Create a new bot by defining a profile — no code duplication needed.

Subclasses may override hooks:
    filter_candidates()   — extra filtering after strategy scan
    format_status_extra() — additional status-report sections
    on_position_opened()  — side-effect when a position opens
    on_position_closed()  — side-effect when a position closes
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from smart_money_radar.bots.base import BaseBot
from smart_money_radar.bots.funding_entry import (
    FundingEntryConfig,
    route_entry_decision,
)
from smart_money_radar.bots.funding_profile import FundingBotProfile
from smart_money_radar.bots.strategies.base import Opportunity
from smart_money_radar.bots.strategies.funding_carry import FundingCarryStrategy
from smart_money_radar.bots.strategies.spread_arb import SpreadArbStrategy
from smart_money_radar.bots.telegram import (
    fmt_money,
    fmt_rate,
    fmt_seconds,
    fmt_signed,
    tg,
)
from smart_money_radar.funding.adapters.base import FundingDataError
from smart_money_radar.funding.economics import market_fee_rate
from smart_money_radar.funding.models import FundingScanConfig
from smart_money_radar.funding.normalization import canonical_asset_symbol
from smart_money_radar.notifications import TelegramNotifier
from smart_money_radar.storage import SQLiteStore, utc_now_iso


STRATEGY_LABELS = {
    "funding_carry": "FUNDING",
    "spread_arb": "SPREAD",
}


def strategy_label(strategy: str) -> str:
    return STRATEGY_LABELS.get(strategy, strategy.upper())


# ---------------------------------------------------------------------------
# Venue client registry
# ---------------------------------------------------------------------------

def build_venue_client(venue: str) -> Any:
    """Instantiate a funding adapter by venue name (lazy imports)."""
    if venue == "risex":
        from smart_money_radar.funding.adapters.risex import RiseXFundingClient
        return RiseXFundingClient()
    if venue == "hyperliquid":
        from smart_money_radar.funding.adapters.hyperliquid import HyperliquidFundingClient
        return HyperliquidFundingClient()
    if venue == "dydx":
        from smart_money_radar.funding.adapters.dydx import DydxFundingClient
        return DydxFundingClient()
    if venue == "lighter":
        from smart_money_radar.funding.adapters.lighter import LighterFundingClient
        return LighterFundingClient()
    if venue == "variational":
        from smart_money_radar.funding.adapters.variational import VariationalFundingClient
        return VariationalFundingClient()
    if venue == "binance":
        from smart_money_radar.funding.adapters.binance import BinanceFundingClient
        return BinanceFundingClient()
    if venue == "bybit":
        from smart_money_radar.funding.adapters.bybit import BybitFundingClient
        return BybitFundingClient()
    if venue == "okx":
        from smart_money_radar.funding.adapters.okx import OKXFundingClient
        return OKXFundingClient()
    if venue == "bitget":
        from smart_money_radar.funding.adapters.bitget import BitgetFundingClient
        return BitgetFundingClient()
    if venue == "gate":
        from smart_money_radar.funding.adapters.gate import GateFundingClient
        return GateFundingClient()
    if venue == "kucoin":
        from smart_money_radar.funding.adapters.kucoin import KuCoinFundingClient
        return KuCoinFundingClient()
    if venue == "mexc":
        from smart_money_radar.funding.adapters.mexc import MEXCFundingClient
        return MEXCFundingClient()
    if venue == "kraken":
        from smart_money_radar.funding.adapters.kraken import KrakenFundingClient
        return KrakenFundingClient()
    if venue == "deribit":
        from smart_money_radar.funding.adapters.deribit import DeribitFundingClient
        return DeribitFundingClient()
    if venue == "backpack":
        from smart_money_radar.funding.adapters.backpack import BackpackFundingClient
        return BackpackFundingClient()
    if venue == "aster":
        from smart_money_radar.funding.adapters.aster import AsterFundingClient
        return AsterFundingClient()
    if venue == "paradex":
        from smart_money_radar.funding.adapters.paradex import ParadexFundingClient
        return ParadexFundingClient()
    if venue == "vertex":
        from smart_money_radar.funding.adapters.vertex import VertexFundingClient
        return VertexFundingClient()
    if venue == "drift":
        from smart_money_radar.funding.adapters.drift import DriftFundingClient
        return DriftFundingClient()
    if venue == "aevo":
        from smart_money_radar.funding.adapters.aevo import AevoFundingClient
        return AevoFundingClient()
    if venue == "apex":
        from smart_money_radar.funding.adapters.apex import ApexFundingClient
        return ApexFundingClient()
    if venue == "coinex":
        from smart_money_radar.funding.adapters.coinex import CoinExFundingClient
        return CoinExFundingClient()
    if venue == "htx":
        from smart_money_radar.funding.adapters.htx import HTXFundingClient
        return HTXFundingClient()
    if venue == "woox":
        from smart_money_radar.funding.adapters.woox import WOOXFundingClient
        return WOOXFundingClient()
    if venue == "bitmart":
        from smart_money_radar.funding.adapters.bitmart import BitMartFundingClient
        return BitMartFundingClient()
    if venue == "edgex":
        from smart_money_radar.funding.adapters.edgex import EdgexFundingClient
        return EdgexFundingClient()
    if venue == "ethereal":
        from smart_money_radar.funding.adapters.ethereal import EtherealFundingClient
        return EtherealFundingClient()
    if venue == "extended":
        from smart_money_radar.funding.adapters.extended import ExtendedFundingClient
        return ExtendedFundingClient()
    if venue == "grvt":
        from smart_money_radar.funding.adapters.grvt import GrvtFundingClient
        return GrvtFundingClient()
    if venue == "pacifica":
        from smart_money_radar.funding.adapters.pacifica import PacificaFundingClient
        return PacificaFundingClient()
    if venue == "reya":
        from smart_money_radar.funding.adapters.reya import ReyaFundingClient
        return ReyaFundingClient()
    raise FundingDataError(f"Unknown venue: {venue}")


# ---------------------------------------------------------------------------
# Paper position (venue-agnostic)
# ---------------------------------------------------------------------------

@dataclass
class PaperPosition:
    position_id: int
    strategy: str
    canonical_asset: str
    primary_side: str  # long | short (on primary venue)
    hedge_venue: str
    hedge_side: str
    notional_usd: float
    primary_entry_price: float
    hedge_entry_price: float
    opened_at: str
    target_close_at: str
    primary_funding_rate: float
    hedge_funding_rate: float
    spread_bps: float
    status: str = "open"
    closed_at: str | None = None
    close_reason: str | None = None
    realized_pnl: float = 0.0
    fees_paid: float = 0.0
    funding_earned: float = 0.0
    entry_spread_per_unit: float = 0.0
    target_quantity: float = 0.0
    primary_symbol: str = ""
    hedge_symbol: str = ""
    settlement_at: str | None = None
    armed_reason: str = ""
    open_reason: str = ""


# ---------------------------------------------------------------------------
# FundingBotBase
# ---------------------------------------------------------------------------

class FundingBotBase(BaseBot):
    """Generic funding bot parameterised by ``FundingBotProfile``."""

    def __init__(
        self,
        store: SQLiteStore,
        profile: FundingBotProfile,
        notifier: TelegramNotifier | None = None,
    ) -> None:
        self.profile = profile
        super().__init__(
            iterations=profile.iterations,
            telegram_enabled=profile.telegram_enabled,
            notifier=notifier
            or TelegramNotifier(
                token_env_var=profile.telegram_token_env,
                chat_id_env_var=profile.telegram_chat_id_env,
            ),
        )
        self.store = store

        # Venue clients
        self.clients: dict[str, Any] = {}
        for venue in profile.venues:
            try:
                self.clients[venue] = build_venue_client(venue)
            except FundingDataError:
                pass

        # Balances
        self.venue_balances: dict[str, float] = {
            venue: profile.venue_starting_balance
            for venue in profile.venues
        }

        # Position state
        self.positions: list[PaperPosition] = []
        self.next_position_id = 1
        self.total_fees_paid = 0.0
        self.total_funding_earned = 0.0
        self.total_realized_pnl = 0.0
        self.closed_trades: list[PaperPosition] = []
        self.armed_routes: dict[str, dict[str, Any]] = {}
        self.scan_count = 0
        self.last_status_report_monotonic = 0.0

        # Strategies
        self._funding_strategy: FundingCarryStrategy | None = None
        self._spread_strategy: SpreadArbStrategy | None = None
        self._init_strategies()

    # ------------------------------------------------------------------
    # Config derivation
    # ------------------------------------------------------------------

    def _scan_config(self) -> FundingScanConfig:
        overrides = dict(self.profile.scan_config_overrides)
        return FundingScanConfig(
            target_notional=self.profile.target_notional_per_leg,
            horizon_mode=self.profile.horizon_mode,
            near_miss_full_depth_routes=0,
            history_refresh_hours=0,
            max_live_history_markets=0,
            minimum_net_profit=self.profile.minimum_net_profit,
            **overrides,
        ).validated()

    def _entry_config(self) -> FundingEntryConfig:
        return FundingEntryConfig(
            entry_window_seconds=self.profile.entry_window_seconds,
            entry_min_lead_seconds=self.profile.entry_min_lead_seconds,
            entry_max_lead_seconds=self.profile.entry_max_lead_seconds,
            arm_window_seconds=self.profile.arm_window_seconds,
            settlement_grace_seconds=self.profile.settlement_grace_seconds,
            collateral_reserve_fraction=self.profile.collateral_reserve_fraction,
            min_live_net_profit=self.profile.minimum_net_profit,
        ).validated()

    def _init_strategies(self) -> None:
        scan_config = self._scan_config()
        primary = self.profile.primary_venue

        if self.profile.funding_carry_enabled and primary:
            self._funding_strategy = FundingCarryStrategy(
                target_venue=primary,
                clients=self.clients,
                scan_config=scan_config,
            )
        if self.profile.spread_arb_enabled and primary:
            primary_client = self.clients.get(primary)
            hedge_clients = {
                v: c for v, c in self.clients.items() if v != primary
            }
            if primary_client:
                self._spread_strategy = SpreadArbStrategy(
                    target_venue=primary,
                    target_client=primary_client,
                    hedge_clients=hedge_clients,
                    scan_config=scan_config,
                    max_hold_hours=self.profile.spread_arb_max_hold_hours,
                    convergence_threshold=self.profile.spread_arb_convergence_threshold,
                )

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def total_balance(self) -> float:
        return sum(self.venue_balances.values())

    @property
    def _primary_venue(self) -> str:
        return self.profile.primary_venue or ""

    # ------------------------------------------------------------------
    # BaseBot interface
    # ------------------------------------------------------------------

    def start_message(self) -> str:
        p = self.profile
        venues = ", ".join(
            f"{tg(v)} {fmt_money(p.venue_starting_balance)}"
            for v in p.venues
        )
        return (
            f"<b>{tg(p.name)} Bot STARTED</b>\n\n"
            f"Capital: <b>{fmt_money(p.total_starting_balance)}</b> (paper)\n"
            f"Venues: {venues}\n"
            f"Notional/leg: <b>{fmt_money(p.target_notional_per_leg)}</b>\n"
            f"Strategies: {tg(p.strategy_list)}\n"
            f"Scan: {p.scan_interval_seconds}s | "
            f"Status: every {p.status_report_interval_seconds // 60} min\n"
            f"Engine: funding scanner pipeline (no artificial BPS filters)"
        )

    def stop_message(self) -> str:
        return (
            f"<b>{tg(self.profile.name)} Bot STOPPED</b>\n\n"
            f"Reason: {tg(self.stop_reason or 'completed')}\n"
            f"Scans: {self.scan_count}\n"
            f"Trades: {len(self.closed_trades)}\n"
            f"Realized PnL: <b>{fmt_signed(self.total_realized_pnl)}</b>\n"
            f"Fees: {fmt_money(self.total_fees_paid)}\n"
            f"Funding earned: {fmt_money(self.total_funding_earned)}"
        )

    def crash_message(self, exc: Exception) -> str:
        return (
            f"<b>{tg(self.profile.name)} Bot ERROR</b>\n\n"
            f"<code>{tg(type(exc).__name__)}</code>: {tg(exc)}\n"
            "Продолжаю работу."
        )

    def run_iteration(self) -> dict[str, Any]:
        self.scan_count += 1
        observed_at = utc_now_iso()
        now = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))

        self._check_position_exits(now, observed_at)

        opportunities: list[dict[str, Any]] = []
        if self._funding_strategy is not None:
            for opp in self._funding_strategy.scan(observed_at):
                opportunities.append(asdict(opp))
        if self._spread_strategy is not None:
            for opp in self._spread_strategy.scan(observed_at):
                opportunities.append(asdict(opp))

        opportunities = self.filter_candidates(opportunities)
        self._process_armed(opportunities, observed_at, now)

        elapsed = time.monotonic() - self.last_status_report_monotonic
        if elapsed >= self.profile.status_report_interval_seconds:
            self._send_status_report(observed_at)
            self.last_status_report_monotonic = time.monotonic()

        return {"scan_count": self.scan_count, "opportunities": len(opportunities)}

    def next_sleep_seconds(self, result: dict[str, Any]) -> int:
        return max(5, self.profile.scan_interval_seconds)

    # ------------------------------------------------------------------
    # Hooks for subclasses
    # ------------------------------------------------------------------

    def filter_candidates(
        self, opportunities: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        return opportunities

    def format_status_extra(self) -> list[str]:
        return []

    def on_position_opened(self, pos: PaperPosition) -> None:
        pass

    def on_position_closed(self, pos: PaperPosition) -> None:
        pass

    # ------------------------------------------------------------------
    # Armed / Open logic
    # ------------------------------------------------------------------

    def _process_armed(
        self,
        opportunities: list[dict[str, Any]],
        observed_at: str,
        now: datetime,
    ) -> None:
        seen_keys: set[str] = set()
        for opp in opportunities:
            key = self._opp_key(opp)
            seen_keys.add(key)

            if key in self.armed_routes:
                continue
            if not self._can_open(opp):
                continue

            if opp["strategy"] == "funding_carry":
                route = (opp.get("extra") or {}).get("route")
                if route is not None:
                    decision = route_entry_decision(
                        route,
                        self._paper_accounts(),
                        now,
                        self._entry_config(),
                    )
                    if not decision["armed"] or not decision["eligible"]:
                        continue

            self.armed_routes[key] = opp
            self.notify(self._armed_message(opp))

        disarmed_keys = [k for k in self.armed_routes if k not in seen_keys]
        for key in disarmed_keys:
            opp = self.armed_routes.pop(key)
            self.notify(self._disarmed_message(opp))

        for key in list(self.armed_routes):
            opp = self.armed_routes[key]
            if not self._can_open(opp):
                self.armed_routes.pop(key)
                self.notify(self._disarmed_message(opp, reason="недостаточно баланса"))
                continue

            if opp["strategy"] == "funding_carry":
                route = (opp.get("extra") or {}).get("route")
                if route is not None:
                    decision = route_entry_decision(
                        route,
                        self._paper_accounts(),
                        now,
                        self._entry_config(),
                    )
                    if not decision["eligible"]:
                        continue

            self.armed_routes.pop(key)
            self._open_position(opp, observed_at, now)

    def _paper_accounts(self) -> dict[str, dict[str, Any]]:
        accounts: dict[str, dict[str, Any]] = {}
        primary = self._primary_venue
        for venue, balance in self.venue_balances.items():
            open_on_venue = sum(
                p.notional_usd
                for p in self.positions
                if p.status == "open" and venue in (primary, p.hedge_venue)
            )
            accounts[venue] = {
                "available_balance": max(0.0, balance - open_on_venue * 0.1),
            }
        return accounts

    def _opp_key(self, opp: dict[str, Any]) -> str:
        return f"{opp['strategy']}|{opp['canonical_asset']}|{opp['hedge_venue']}"

    def _can_open(self, opp: dict[str, Any]) -> bool:
        primary = self._primary_venue
        hedge_venue = opp["hedge_venue"]
        notional = float(
            opp.get("notional") or self.profile.target_notional_per_leg
        )
        fees = float(opp.get("total_cost", 0))
        if self.venue_balances.get(primary, 0) < fees + notional * 0.1:
            return False
        if self.venue_balances.get(hedge_venue, 0) < fees + notional * 0.1:
            return False
        return True

    # ------------------------------------------------------------------
    # Position management
    # ------------------------------------------------------------------

    def _open_position(
        self,
        opp: dict[str, Any],
        observed_at: str,
        now: datetime,
    ) -> None:
        notional = min(
            float(opp.get("notional") or self.profile.target_notional_per_leg),
            self.profile.target_notional_per_leg,
        )
        if notional < 50:
            return

        fees = float(opp.get("total_cost", 0))
        hedge_venue = opp["hedge_venue"]
        primary = self._primary_venue

        settlement_at = opp.get("settlement_at")
        if settlement_at:
            target_close = datetime.fromisoformat(
                str(settlement_at).replace("Z", "+00:00")
            )
            if target_close.tzinfo is None:
                target_close = target_close.replace(tzinfo=UTC)
        else:
            hold_hours = (
                self.profile.spread_arb_max_hold_hours
                if opp["strategy"] == "spread_arb"
                else 1.0
            )
            target_close = now + timedelta(hours=hold_hours)

        if opp["strategy"] == "funding_carry":
            open_reason = (
                f"live net {fmt_signed(opp.get('net_profit'))} ≥ 0 после всех расходов; "
                f"funding spread {float(opp.get('spread_bps', 0)):.2f} bps/h; "
                f"settlement через {fmt_seconds((target_close - now).total_seconds())}"
            )
        else:
            open_reason = (
                f"executable spread {float(opp.get('spread_bps', 0)):.2f} bps; "
                f"net {fmt_signed(opp.get('net_profit'))} после fees+slippage+basis; "
                f"gross {fmt_money(opp.get('gross_profit'))}"
            )

        extra = opp.get("extra") or {}
        position = PaperPosition(
            position_id=self.next_position_id,
            strategy=opp["strategy"],
            canonical_asset=opp["canonical_asset"],
            primary_side=opp.get("primary_side", "long"),
            hedge_venue=hedge_venue,
            hedge_side=opp["hedge_side"],
            notional_usd=notional,
            primary_entry_price=float(opp.get("primary_price", 0)),
            hedge_entry_price=float(opp.get("hedge_price", 0)),
            opened_at=observed_at,
            target_close_at=target_close.isoformat(),
            primary_funding_rate=float(opp.get("primary_funding_rate", 0)),
            hedge_funding_rate=float(opp.get("hedge_funding_rate", 0)),
            spread_bps=float(opp.get("spread_bps", 0)),
            fees_paid=fees,
            entry_spread_per_unit=float(
                extra.get("entry_spread_per_unit", opp.get("entry_spread_per_unit", 0))
            ),
            target_quantity=float(
                extra.get("target_quantity", opp.get("target_quantity", 0))
            ),
            primary_symbol=str(opp.get("primary_symbol", "")),
            hedge_symbol=str(opp.get("hedge_symbol", "")),
            settlement_at=str(settlement_at) if settlement_at else None,
            open_reason=open_reason,
        )
        self.next_position_id += 1
        self.positions.append(position)

        half_fees = fees / 2.0
        self.venue_balances[primary] = (
            self.venue_balances.get(primary, 0) - half_fees
        )
        self.venue_balances[hedge_venue] = (
            self.venue_balances.get(hedge_venue, 0) - half_fees
        )
        self.total_fees_paid += fees

        self.notify(self._open_message(position, opp))
        self.on_position_opened(position)

    def _check_position_exits(self, now: datetime, observed_at: str) -> None:
        primary = self._primary_venue
        for pos in self.positions:
            if pos.status != "open":
                continue

            if pos.strategy == "spread_arb":
                should_close, reason, exit_spread = self._check_spread_convergence(
                    pos, observed_at
                )
                if should_close:
                    self._close_position(pos, reason, observed_at, now, exit_spread)
                    continue
                opened = datetime.fromisoformat(pos.opened_at.replace("Z", "+00:00"))
                if opened.tzinfo is None:
                    opened = opened.replace(tzinfo=UTC)
                hold_hours = (now - opened).total_seconds() / 3600.0
                if hold_hours >= self.profile.spread_arb_max_hold_hours:
                    _, _, exit_spread = self._check_spread_convergence(
                        pos, observed_at
                    )
                    self._close_position(pos, "time_stop", observed_at, now, exit_spread)
                    continue

            target = datetime.fromisoformat(pos.target_close_at.replace("Z", "+00:00"))
            if target.tzinfo is None:
                target = target.replace(tzinfo=UTC)
            if now >= target:
                reason = (
                    "settlement_reached"
                    if pos.strategy == "funding_carry"
                    else "target_reached"
                )
                self._close_position(pos, reason, observed_at, now)

    def _check_spread_convergence(
        self,
        pos: PaperPosition,
        observed_at: str,
    ) -> tuple[bool, str, float]:
        primary = self._primary_venue
        try:
            primary_client = self.clients.get(primary)
            if primary_client is None:
                return False, "", 0.0
            _, primary_markets, _ = primary_client.catalog_and_markets(observed_at)
            primary_price = 0.0
            for m in primary_markets:
                if canonical_asset_symbol(m.get("canonical_asset")) == pos.canonical_asset:
                    primary_price = float(m.get("mark_price") or 0)
                    break

            hedge_client = self.clients.get(pos.hedge_venue)
            if hedge_client is None:
                return False, "", 0.0
            _, hedge_markets, _ = hedge_client.catalog_and_markets(observed_at)
            hedge_price = 0.0
            for m in hedge_markets:
                if canonical_asset_symbol(m.get("canonical_asset")) == pos.canonical_asset:
                    hedge_price = float(m.get("mark_price") or 0)
                    break

            if primary_price <= 0 or hedge_price <= 0:
                return False, "", 0.0

            if pos.primary_side == "long":
                current_spread = hedge_price - primary_price
            else:
                current_spread = primary_price - hedge_price

            if current_spread < 0:
                inversion_limit = -pos.entry_spread_per_unit * self.profile.spread_arb_inversion_threshold
                if pos.entry_spread_per_unit <= 0 or current_spread < inversion_limit:
                    return True, "spread_inverted", current_spread

            threshold = self.profile.spread_arb_convergence_threshold
            if (
                pos.entry_spread_per_unit > 0
                and current_spread < pos.entry_spread_per_unit * threshold
            ):
                return True, "spread_converged", current_spread

            return False, "", current_spread
        except FundingDataError:
            return False, "", 0.0

    def _close_position(
        self,
        pos: PaperPosition,
        reason: str,
        observed_at: str,
        now: datetime,
        exit_spread_per_unit: float | None = None,
    ) -> None:
        primary = self._primary_venue
        opened = datetime.fromisoformat(pos.opened_at.replace("Z", "+00:00"))
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=UTC)
        hold_hours = max(0.1, (now - opened).total_seconds() / 3600.0)

        if pos.strategy == "funding_carry":
            if pos.primary_side == "long":
                funding = (
                    (pos.hedge_funding_rate - pos.primary_funding_rate)
                    * pos.notional_usd
                    * hold_hours
                )
            else:
                funding = (
                    (pos.primary_funding_rate - pos.hedge_funding_rate)
                    * pos.notional_usd
                    * hold_hours
                )
            close_fees = pos.notional_usd * (
                market_fee_rate({"venue": primary})
                + market_fee_rate({"venue": pos.hedge_venue})
            )
            net_pnl = funding - close_fees
        else:
            if pos.primary_side == "long":
                funding = (
                    (pos.hedge_funding_rate - pos.primary_funding_rate)
                    * pos.notional_usd
                    * hold_hours
                )
            else:
                funding = (
                    (pos.primary_funding_rate - pos.hedge_funding_rate)
                    * pos.notional_usd
                    * hold_hours
                )
            if exit_spread_per_unit is None:
                exit_spread_per_unit = 0.0
            spread_pnl = (
                (pos.entry_spread_per_unit - exit_spread_per_unit)
                * pos.target_quantity
            )
            close_fees = pos.notional_usd * (
                market_fee_rate({"venue": primary})
                + market_fee_rate({"venue": pos.hedge_venue})
            )
            net_pnl = spread_pnl + funding - close_fees

        pos.status = "closed"
        pos.closed_at = observed_at
        pos.close_reason = reason
        pos.realized_pnl = net_pnl
        pos.funding_earned = funding
        pos.fees_paid += close_fees

        half_close = close_fees / 2.0
        self.venue_balances[primary] = (
            self.venue_balances.get(primary, 0) - half_close + net_pnl / 2.0
        )
        self.venue_balances[pos.hedge_venue] = (
            self.venue_balances.get(pos.hedge_venue, 0) - half_close + net_pnl / 2.0
        )
        self.total_fees_paid += close_fees
        self.total_funding_earned += funding
        self.total_realized_pnl += net_pnl
        self.closed_trades.append(pos)

        self.notify(self._close_message(pos, hold_hours, funding, close_fees, net_pnl))
        self.on_position_closed(pos)

    # ------------------------------------------------------------------
    # Telegram messages
    # ------------------------------------------------------------------

    def _armed_message(self, opp: dict[str, Any]) -> str:
        name = tg(self.profile.name)
        label = strategy_label(opp["strategy"])
        asset = tg(opp["canonical_asset"])
        primary = tg(self._primary_venue)
        if opp["strategy"] == "funding_carry":
            reason = (
                f"live net {fmt_signed(opp.get('net_profit'))} ≥ 0; "
                f"spread {float(opp.get('spread_bps', 0)):.2f} bps/h; "
                f"costs {fmt_money(opp.get('total_cost'))}"
            )
        else:
            reason = (
                f"spread {float(opp.get('spread_bps', 0)):.2f} bps; "
                f"net {fmt_signed(opp.get('net_profit'))} после всех расходов"
            )
        return (
            f"<b>{name} [{label}] ARMED</b>\n\n"
            f"<b>{asset}</b>\n"
            f"{primary}: {tg(opp.get('primary_side', '').upper())} "
            f"@ {fmt_money(opp.get('primary_price'))}\n"
            f"{tg(opp['hedge_venue'])}: {tg(opp['hedge_side'].upper())} "
            f"@ {fmt_money(opp.get('hedge_price'))}\n\n"
            f"Причина: {tg(reason)}\n"
            f"Notional: {fmt_money(opp.get('notional'))} | "
            f"Expected net: <b>{fmt_signed(opp.get('net_profit'))}</b>"
        )

    def _disarmed_message(
        self, opp: dict[str, Any], reason: str = "маршрут пропал из скана"
    ) -> str:
        name = tg(self.profile.name)
        label = strategy_label(opp["strategy"])
        return (
            f"<b>{name} [{label}] DISARMED</b>\n\n"
            f"<b>{tg(opp['canonical_asset'])}</b>\n"
            f"{tg(self._primary_venue)} / {tg(opp['hedge_venue'])}\n\n"
            f"Причина: {tg(reason)}"
        )

    def _open_message(self, pos: PaperPosition, opp: dict[str, Any]) -> str:
        name = tg(self.profile.name)
        label = strategy_label(pos.strategy)
        primary = tg(self._primary_venue)
        return (
            f"<b>{name} [{label}] OPEN #{pos.position_id}</b>\n\n"
            f"<b>{tg(pos.canonical_asset)}</b>\n"
            f"{primary}: {tg(pos.primary_side.upper())} "
            f"<code>{tg(pos.primary_symbol)}</code> "
            f"@ {fmt_money(pos.primary_entry_price)}\n"
            f"{tg(pos.hedge_venue)}: {tg(pos.hedge_side.upper())} "
            f"<code>{tg(pos.hedge_symbol)}</code> "
            f"@ {fmt_money(pos.hedge_entry_price)}\n\n"
            f"Size: <b>{fmt_money(pos.notional_usd)}</b> per leg\n"
            f"Expected net: <b>{fmt_signed(opp.get('net_profit'))}</b>\n"
            f"Costs: {fmt_money(pos.fees_paid)}\n"
            f"Funding: {primary} {fmt_rate(pos.primary_funding_rate)} | "
            f"{tg(pos.hedge_venue)} {fmt_rate(pos.hedge_funding_rate)}\n\n"
            f"Причина: {tg(pos.open_reason)}\n"
            f"Target close: {tg(pos.target_close_at[:16])} UTC"
        )

    def _close_message(
        self,
        pos: PaperPosition,
        hold_hours: float,
        funding: float,
        close_fees: float,
        net_pnl: float,
    ) -> str:
        name = tg(self.profile.name)
        label = strategy_label(pos.strategy)
        emoji = "✅" if net_pnl >= 0 else "❌"
        primary = tg(self._primary_venue)
        reason_text = {
            "settlement_reached": "funding settlement наступил",
            "target_reached": "целевое время закрытия",
            "spread_converged": "спред схлопнулся (конвергенция)",
            "spread_inverted": "спред инвертировался (расширился в убыток)",
            "time_stop": f"тайм-стоп {self.profile.spread_arb_max_hold_hours:.0f}ч",
        }.get(pos.close_reason or "", pos.close_reason or "-")

        if pos.strategy == "funding_carry":
            pnl_line = f"Funding earned: <b>{fmt_signed(funding)}</b>"
        else:
            pnl_line = f"Spread PnL: <b>{fmt_signed(net_pnl + close_fees)}</b>"

        return (
            f"<b>{name} [{label}] CLOSE #{pos.position_id}</b> {emoji}\n\n"
            f"<b>{tg(pos.canonical_asset)}</b>\n"
            f"{primary}: {tg(pos.primary_side.upper())} / "
            f"{tg(pos.hedge_venue)}: {tg(pos.hedge_side.upper())}\n\n"
            f"Причина закрытия: {tg(reason_text)}\n"
            f"Hold: {hold_hours:.1f}h\n\n"
            f"{pnl_line}\n"
            f"Close fees: {fmt_money(close_fees)}\n"
            f"Net PnL: <b>{fmt_signed(net_pnl)}</b>\n"
            f"Balance: <b>{fmt_money(self.total_balance)}</b>"
        )

    # ------------------------------------------------------------------
    # Status report
    # ------------------------------------------------------------------

    def _send_status_report(self, observed_at: str) -> None:
        p = self.profile
        primary = self._primary_venue
        open_positions = [pos for pos in self.positions if pos.status == "open"]
        wins = sum(1 for t in self.closed_trades if t.realized_pnl >= 0)
        losses = sum(1 for t in self.closed_trades if t.realized_pnl < 0)
        win_rate = wins / max(1, wins + losses) * 100

        lines = [
            f"<b>{tg(p.name)} Bot STATUS</b>",
            "",
            f"<b>Balance: {fmt_money(self.total_balance)}</b> "
            f"(start: {fmt_money(p.total_starting_balance)})",
            f"Return: <b>{(self.total_balance / p.total_starting_balance - 1) * 100:.3f}%</b>",
            f"Realized PnL: <b>{fmt_signed(self.total_realized_pnl)}</b>",
            f"Trades: {len(self.closed_trades)} (W:{wins} L:{losses} WR:{win_rate:.0f}%)",
            f"Open: {len(open_positions)} | Armed: {len(self.armed_routes)} | Scans: {self.scan_count}",
            f"Fees: {fmt_money(self.total_fees_paid)} | Funding: {fmt_money(self.total_funding_earned)}",
        ]

        lines.extend(["", "<b>Balances by venue</b>"])
        for venue in p.venues:
            bal = self.venue_balances.get(venue, 0)
            open_count = sum(
                1
                for pos in self.positions
                if pos.status == "open" and venue in (primary, pos.hedge_venue)
            )
            lines.append(f"  {tg(venue)}: {fmt_money(bal)} | open: {open_count}")

        if open_positions:
            lines.extend(["", "<b>Open positions</b>"])
            for pos in open_positions:
                label = strategy_label(pos.strategy)
                lines.append(
                    f"\n<b>#{pos.position_id} [{label}] {tg(pos.canonical_asset)}</b>\n"
                    f"{tg(primary)}:{tg(pos.primary_side)} / "
                    f"{tg(pos.hedge_venue)}:{tg(pos.hedge_side)} | "
                    f"{fmt_money(pos.notional_usd)} | {pos.spread_bps:.1f}bps\n"
                    f"Opened: {tg(pos.opened_at[:16])} | Close: {tg(pos.target_close_at[:16])}"
                )

        if self.armed_routes:
            lines.extend(["", "<b>Candidates (armed)</b>"])
            for _key, opp in self.armed_routes.items():
                label = strategy_label(opp["strategy"])
                lines.append(
                    f"\n<b>[{label}] {tg(opp['canonical_asset'])}</b>\n"
                    f"{tg(primary)}:{tg(opp.get('primary_side', ''))} / "
                    f"{tg(opp['hedge_venue'])}:{tg(opp['hedge_side'])} | "
                    f"{fmt_money(opp.get('notional'))} | "
                    f"{float(opp.get('spread_bps', 0)):.1f}bps\n"
                    f"Net: {fmt_signed(opp.get('net_profit'))} | "
                    f"Cost: {fmt_money(opp.get('total_cost'))}"
                )
        else:
            lines.extend(["", "<b>Candidates:</b> нет"])

        strategy_pnl: dict[str, float] = {}
        strategy_count: dict[str, int] = {}
        for t in self.closed_trades:
            strategy_pnl[t.strategy] = strategy_pnl.get(t.strategy, 0.0) + t.realized_pnl
            strategy_count[t.strategy] = strategy_count.get(t.strategy, 0) + 1
        if strategy_pnl:
            lines.extend(["", "<b>By strategy</b>"])
            for strat in sorted(strategy_pnl):
                label = strategy_label(strat)
                lines.append(
                    f"  [{label}] {strategy_count[strat]} trades, "
                    f"PnL {fmt_signed(strategy_pnl[strat])}"
                )

        extra = self.format_status_extra()
        if extra:
            lines.extend(extra)

        self.notify("\n".join(lines))

    # ------------------------------------------------------------------
    # Summary (for CLI / dashboard)
    # ------------------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        open_positions = [p for p in self.positions if p.status == "open"]
        wins = sum(1 for t in self.closed_trades if t.realized_pnl >= 0)
        losses = sum(1 for t in self.closed_trades if t.realized_pnl < 0)
        return {
            "model_version": self.profile.model_version,
            "total_starting_balance": self.profile.total_starting_balance,
            "current_balance": self.total_balance,
            "venue_balances": dict(self.venue_balances),
            "return_pct": (
                self.total_balance / self.profile.total_starting_balance - 1
            )
            * 100,
            "realized_pnl": self.total_realized_pnl,
            "total_fees": self.total_fees_paid,
            "total_funding": self.total_funding_earned,
            "scan_count": self.scan_count,
            "armed_count": len(self.armed_routes),
            "open_position_count": len(open_positions),
            "closed_trade_count": len(self.closed_trades),
            "win_count": wins,
            "loss_count": losses,
            "win_rate": wins / max(1, wins + losses) * 100,
            "open_positions": [
                {
                    "id": p.position_id,
                    "strategy": p.strategy,
                    "asset": p.canonical_asset,
                    "primary_side": p.primary_side,
                    "hedge_venue": p.hedge_venue,
                    "hedge_side": p.hedge_side,
                    "notional": p.notional_usd,
                    "spread_bps": p.spread_bps,
                    "opened_at": p.opened_at,
                }
                for p in open_positions
            ],
        }
