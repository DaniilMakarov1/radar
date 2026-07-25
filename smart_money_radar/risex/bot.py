from __future__ import annotations

import signal
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from types import FrameType
from typing import Any

from smart_money_radar.funding.adapters.base import FundingDataError, FundingHttpClient, as_float
from smart_money_radar.funding.adapters.risex import RiseXFundingClient
from smart_money_radar.funding.adapters.hyperliquid import HyperliquidFundingClient
from smart_money_radar.funding.adapters.dydx import DydxFundingClient
from smart_money_radar.funding.adapters.lighter import LighterFundingClient
from smart_money_radar.funding.normalization import canonical_asset_symbol
from smart_money_radar.notifications import TelegramNotifier
from smart_money_radar.storage import SQLiteStore, utc_now_iso


RISEX_BOT_MODEL_VERSION = "risex_paper_v1"

DEX_HEDGE_VENUES = ("hyperliquid", "dydx", "lighter")


@dataclass(frozen=True)
class RiseXBotConfig:
    starting_balance: float = 1_000.0
    target_notional_per_leg: float = 500.0
    scan_interval_seconds: int = 60
    status_report_interval_seconds: int = 1_800  # 30 min
    min_funding_spread_bps: float = 0.5
    min_spread_arb_bps: float = 3.0
    max_open_positions: int = 3
    position_hold_minutes: int = 120
    volume_farming_enabled: bool = True
    funding_carry_enabled: bool = True
    spread_arb_enabled: bool = True
    hedge_venues: tuple[str, ...] = DEX_HEDGE_VENUES
    iterations: int | None = None
    telegram_enabled: bool = True

    def validated(self) -> "RiseXBotConfig":
        return RiseXBotConfig(
            starting_balance=max(10.0, self.starting_balance),
            target_notional_per_leg=max(50.0, self.target_notional_per_leg),
            scan_interval_seconds=max(10, self.scan_interval_seconds),
            status_report_interval_seconds=max(60, self.status_report_interval_seconds),
            min_funding_spread_bps=max(0.0, self.min_funding_spread_bps),
            min_spread_arb_bps=max(0.0, self.min_spread_arb_bps),
            max_open_positions=max(1, self.max_open_positions),
            position_hold_minutes=max(5, self.position_hold_minutes),
            volume_farming_enabled=self.volume_farming_enabled,
            funding_carry_enabled=self.funding_carry_enabled,
            spread_arb_enabled=self.spread_arb_enabled,
            hedge_venues=self.hedge_venues,
            iterations=self.iterations,
            telegram_enabled=self.telegram_enabled,
        )


@dataclass
class PaperPosition:
    position_id: int
    strategy: str  # funding_carry | volume_farm | spread_arb
    canonical_asset: str
    risex_side: str  # long | short
    hedge_venue: str
    hedge_side: str  # long | short
    notional_usd: float
    risex_entry_price: float
    hedge_entry_price: float
    opened_at: str
    target_close_at: str
    risex_funding_rate: float
    hedge_funding_rate: float
    spread_bps: float
    status: str = "open"  # open | closed
    closed_at: str | None = None
    close_reason: str | None = None
    realized_pnl: float = 0.0
    fees_paid: float = 0.0
    funding_earned: float = 0.0
    volume_generated: float = 0.0


class RiseXBot:
    def __init__(
        self,
        store: SQLiteStore,
        config: RiseXBotConfig | None = None,
        notifier: TelegramNotifier | None = None,
    ) -> None:
        self.store = store
        self.config = (config or RiseXBotConfig()).validated()
        self.notifier = notifier or TelegramNotifier(
            token_env_var="RISEX_TELEGRAM_BOT_TOKEN",
            chat_id_env_var="RISEX_TELEGRAM_CHAT_ID",
        )
        self.risex = RiseXFundingClient()
        self.hedge_clients: dict[str, Any] = {}
        self._init_hedge_clients()

        self.balance = self.config.starting_balance
        self.positions: list[PaperPosition] = []
        self.next_position_id = 1
        self.total_fees_paid = 0.0
        self.total_funding_earned = 0.0
        self.total_volume = 0.0
        self.total_realized_pnl = 0.0
        self.closed_trades: list[PaperPosition] = []
        self.scan_count = 0
        self.last_status_report_monotonic = 0.0
        self.stop_requested = False
        self.stop_reason: str | None = None

    def _init_hedge_clients(self) -> None:
        for venue in self.config.hedge_venues:
            if venue == "hyperliquid":
                self.hedge_clients[venue] = HyperliquidFundingClient()
            elif venue == "dydx":
                self.hedge_clients[venue] = DydxFundingClient()
            elif venue == "lighter":
                self.hedge_clients[venue] = LighterFundingClient()

    def install_signal_handlers(self) -> dict[int, Any]:
        previous: dict[int, Any] = {}
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                previous[sig] = signal.getsignal(sig)
                signal.signal(sig, self._handle_signal)
            except (OSError, ValueError):
                pass
        return previous

    def _handle_signal(self, signum: int, frame: FrameType | None) -> None:
        self.stop_requested = True
        self.stop_reason = f"signal_{signum}"

    def run_loop(self) -> None:
        completed = 0
        previous_handlers = self.install_signal_handlers()
        try:
            self._notify(
                "RiseX Bot STARTED\n"
                f"  capital: ${self.config.starting_balance:,.2f} (paper)\n"
                f"  notional/leg: ${self.config.target_notional_per_leg:,.0f}\n"
                f"  strategies: {self._strategy_list()}\n"
                f"  hedge venues: {', '.join(self.config.hedge_venues)}\n"
                f"  scan interval: {self.config.scan_interval_seconds}s\n"
                f"  status report: every {self.config.status_report_interval_seconds // 60} min"
            )
            while (
                not self.stop_requested
                and (self.config.iterations is None or completed < self.config.iterations)
            ):
                try:
                    self._run_iteration()
                except Exception as exc:
                    self._notify(
                        f"RiseX Bot ERROR\n"
                        f"  {type(exc).__name__}: {exc}\n"
                        f"  Continuing..."
                    )
                completed += 1
                if self.config.iterations is not None and completed >= self.config.iterations:
                    break
                time.sleep(max(5, self.config.scan_interval_seconds))
        finally:
            for sig, handler in previous_handlers.items():
                try:
                    signal.signal(sig, handler)
                except (OSError, ValueError):
                    pass
            self._notify(
                f"RiseX Bot STOPPED\n"
                f"  reason: {self.stop_reason or 'completed'}\n"
                f"  scans: {self.scan_count}\n"
                f"  trades: {len(self.closed_trades)}\n"
                f"  realized PnL: ${self.total_realized_pnl:.4f}\n"
                f"  volume: ${self.total_volume:,.0f}\n"
                f"  fees: ${self.total_fees_paid:.4f}\n"
                f"  funding: ${self.total_funding_earned:.4f}"
            )

    def _run_iteration(self) -> None:
        self.scan_count += 1
        observed_at = utc_now_iso()
        now = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))

        # 1. Check and close expired positions
        self._check_position_exits(now, observed_at)

        # 2. Scan for new opportunities
        opportunities = self._scan_opportunities(observed_at)

        # 3. Open new positions if slots available
        open_count = sum(1 for p in self.positions if p.status == "open")
        slots = self.config.max_open_positions - open_count
        if slots > 0 and opportunities:
            for opp in opportunities[:slots]:
                self._open_position(opp, observed_at, now)

        # 4. Status report
        elapsed = time.monotonic() - self.last_status_report_monotonic
        if elapsed >= self.config.status_report_interval_seconds:
            self._send_status_report(observed_at)
            self.last_status_report_monotonic = time.monotonic()

    def _scan_opportunities(self, observed_at: str) -> list[dict[str, Any]]:
        """Scan RiseX + hedge venues for delta-neutral opportunities."""
        opportunities: list[dict[str, Any]] = []

        try:
            _, risex_markets, _ = self.risex.catalog_and_markets(observed_at)
        except FundingDataError:
            return opportunities

        risex_by_asset: dict[str, dict[str, Any]] = {}
        for m in risex_markets:
            asset = canonical_asset_symbol(m.get("canonical_asset"))
            if asset:
                risex_by_asset[asset] = m

        for venue_name, client in self.hedge_clients.items():
            try:
                _, hedge_markets, _ = client.catalog_and_markets(observed_at)
            except FundingDataError:
                continue

            hedge_by_asset: dict[str, dict[str, Any]] = {}
            for m in hedge_markets:
                asset = canonical_asset_symbol(m.get("canonical_asset"))
                if asset:
                    hedge_by_asset[asset] = m

            for asset, risex_m in risex_by_asset.items():
                hedge_m = hedge_by_asset.get(asset)
                if not hedge_m:
                    continue

                risex_hourly = as_float(risex_m.get("hourly_funding_rate"))
                hedge_hourly = as_float(hedge_m.get("hourly_funding_rate"))
                risex_price = as_float(risex_m.get("mark_price"))
                hedge_price = as_float(hedge_m.get("mark_price"))

                if risex_price <= 0 or hedge_price <= 0:
                    continue

                # Funding spread: positive = short RiseX, long hedge
                funding_spread = hedge_hourly - risex_hourly
                funding_spread_bps = funding_spread * 10_000

                # Price spread for arb
                price_spread_bps = abs(risex_price - hedge_price) / min(risex_price, hedge_price) * 10_000

                # Strategy 1: Funding carry
                if (
                    self.config.funding_carry_enabled
                    and abs(funding_spread_bps) >= self.config.min_funding_spread_bps
                ):
                    if funding_spread > 0:
                        risex_side, hedge_side = "short", "long"
                    else:
                        risex_side, hedge_side = "long", "short"
                    opportunities.append({
                        "strategy": "funding_carry",
                        "canonical_asset": asset,
                        "risex_side": risex_side,
                        "hedge_venue": venue_name,
                        "hedge_side": hedge_side,
                        "risex_price": risex_price,
                        "hedge_price": hedge_price,
                        "risex_funding_rate": risex_hourly,
                        "hedge_funding_rate": hedge_hourly,
                        "spread_bps": abs(funding_spread_bps),
                        "score": abs(funding_spread_bps),
                    })

                # Strategy 2: Volume farming (always open/close for volume)
                if self.config.volume_farming_enabled:
                    opportunities.append({
                        "strategy": "volume_farm",
                        "canonical_asset": asset,
                        "risex_side": "long",
                        "hedge_venue": venue_name,
                        "hedge_side": "short",
                        "risex_price": risex_price,
                        "hedge_price": hedge_price,
                        "risex_funding_rate": risex_hourly,
                        "hedge_funding_rate": hedge_hourly,
                        "spread_bps": abs(funding_spread_bps),
                        "score": 0.1,  # Low priority, only if no better opps
                    })

                # Strategy 3: Spread arbitrage
                if (
                    self.config.spread_arb_enabled
                    and price_spread_bps >= self.config.min_spread_arb_bps
                ):
                    if risex_price < hedge_price:
                        risex_side, hedge_side = "long", "short"
                    else:
                        risex_side, hedge_side = "short", "long"
                    opportunities.append({
                        "strategy": "spread_arb",
                        "canonical_asset": asset,
                        "risex_side": risex_side,
                        "hedge_venue": venue_name,
                        "hedge_side": hedge_side,
                        "risex_price": risex_price,
                        "hedge_price": hedge_price,
                        "risex_funding_rate": risex_hourly,
                        "hedge_funding_rate": hedge_hourly,
                        "spread_bps": price_spread_bps,
                        "score": price_spread_bps,
                    })

        # Deduplicate: keep best opportunity per asset per strategy
        best: dict[tuple[str, str], dict[str, Any]] = {}
        for opp in opportunities:
            key = (opp["canonical_asset"], opp["strategy"])
            if key not in best or opp["score"] > best[key]["score"]:
                best[key] = opp

        result = sorted(best.values(), key=lambda o: o["score"], reverse=True)
        return result

    def _open_position(
        self,
        opp: dict[str, Any],
        observed_at: str,
        now: datetime,
    ) -> None:
        notional = min(
            self.config.target_notional_per_leg,
            self.balance * 0.9,  # Keep 10% reserve
        )
        if notional < 50:
            return

        # Fee calculation
        risex_fee = notional * 0.0001  # 1 bp maker
        hedge_fee = notional * 0.0002  # 2 bp taker on hedge venue
        total_fees = risex_fee + hedge_fee

        if total_fees > self.balance * 0.5:
            return

        hold_minutes = self.config.position_hold_minutes
        if opp["strategy"] == "funding_carry":
            hold_minutes = max(hold_minutes, 60)  # Hold at least 1h for funding
        elif opp["strategy"] == "volume_farm":
            hold_minutes = min(hold_minutes, 30)  # Short hold for volume

        target_close = now + timedelta(minutes=hold_minutes)

        position = PaperPosition(
            position_id=self.next_position_id,
            strategy=opp["strategy"],
            canonical_asset=opp["canonical_asset"],
            risex_side=opp["risex_side"],
            hedge_venue=opp["hedge_venue"],
            hedge_side=opp["hedge_side"],
            notional_usd=notional,
            risex_entry_price=opp["risex_price"],
            hedge_entry_price=opp["hedge_price"],
            opened_at=observed_at,
            target_close_at=target_close.isoformat(),
            risex_funding_rate=opp["risex_funding_rate"],
            hedge_funding_rate=opp["hedge_funding_rate"],
            spread_bps=opp["spread_bps"],
            fees_paid=total_fees,
            volume_generated=notional * 2,  # open + close
        )
        self.next_position_id += 1
        self.positions.append(position)
        self.balance -= total_fees
        self.total_fees_paid += total_fees
        self.total_volume += notional * 2

        self._notify(
            f"RiseX OPEN #{position.position_id}\n"
            f"  {position.strategy} | {position.canonical_asset}\n"
            f"  RiseX: {position.risex_side.upper()} @ ${position.risex_entry_price:,.2f}\n"
            f"  {position.hedge_venue}: {position.hedge_side.upper()} @ ${position.hedge_entry_price:,.2f}\n"
            f"  notional: ${notional:,.0f} | spread: {position.spread_bps:.2f} bps\n"
            f"  fees: ${total_fees:.4f}\n"
            f"  target close: {target_close.strftime('%H:%M UTC')}"
        )

    def _check_position_exits(self, now: datetime, observed_at: str) -> None:
        for pos in self.positions:
            if pos.status != "open":
                continue
            target = datetime.fromisoformat(pos.target_close_at.replace("Z", "+00:00"))
            if target.tzinfo is None:
                target = target.replace(tzinfo=UTC)

            if now >= target:
                self._close_position(pos, "target_reached", observed_at, now)

    def _close_position(
        self,
        pos: PaperPosition,
        reason: str,
        observed_at: str,
        now: datetime,
    ) -> None:
        # Calculate PnL
        # For funding carry: earn the funding spread over hold time
        hold_hours = max(0.1, (now - datetime.fromisoformat(
            pos.opened_at.replace("Z", "+00:00")
        ).replace(tzinfo=UTC)).total_seconds() / 3600)

        if pos.strategy == "funding_carry":
            # Funding earned = spread × notional × hours
            if pos.risex_side == "short":
                funding = (pos.hedge_funding_rate - pos.risex_funding_rate) * pos.notional_usd * hold_hours
            else:
                funding = (pos.risex_funding_rate - pos.hedge_funding_rate) * pos.notional_usd * hold_hours
        else:
            funding = 0.0

        # Close fees
        close_fees = pos.notional_usd * (0.0001 + 0.0002)  # maker + taker

        net_pnl = funding - close_fees

        pos.status = "closed"
        pos.closed_at = observed_at
        pos.close_reason = reason
        pos.realized_pnl = net_pnl
        pos.funding_earned = funding
        pos.fees_paid += close_fees
        pos.volume_generated += pos.notional_usd * 2  # close volume

        self.balance += net_pnl
        self.total_fees_paid += close_fees
        self.total_funding_earned += funding
        self.total_realized_pnl += net_pnl
        self.total_volume += pos.notional_usd * 2
        self.closed_trades.append(pos)

        emoji = "✅" if net_pnl >= 0 else "❌"
        self._notify(
            f"RiseX CLOSE #{pos.position_id} {emoji}\n"
            f"  {pos.strategy} | {pos.canonical_asset}\n"
            f"  reason: {reason}\n"
            f"  hold: {hold_hours:.1f}h\n"
            f"  funding: ${funding:.4f}\n"
            f"  close fees: ${close_fees:.4f}\n"
            f"  net PnL: ${net_pnl:.4f}\n"
            f"  balance: ${self.balance:.2f}"
        )

    def _send_status_report(self, observed_at: str) -> None:
        open_positions = [p for p in self.positions if p.status == "open"]
        wins = sum(1 for t in self.closed_trades if t.realized_pnl >= 0)
        losses = sum(1 for t in self.closed_trades if t.realized_pnl < 0)
        win_rate = wins / max(1, wins + losses) * 100

        lines = [
            f"RiseX Bot STATUS ({observed_at[:16]})",
            f"  balance: ${self.balance:.2f} (start: ${self.config.starting_balance:.2f})",
            f"  return: {(self.balance / self.config.starting_balance - 1) * 100:.3f}%",
            f"  realized PnL: ${self.total_realized_pnl:.4f}",
            f"  trades: {len(self.closed_trades)} (W:{wins} L:{losses} WR:{win_rate:.0f}%)",
            f"  open positions: {len(open_positions)}",
            f"  volume: ${self.total_volume:,.0f}",
            f"  fees paid: ${self.total_fees_paid:.4f}",
            f"  funding earned: ${self.total_funding_earned:.4f}",
            f"  scans: {self.scan_count}",
        ]

        if open_positions:
            lines.append("")
            lines.append("Open positions:")
            for p in open_positions:
                lines.append(
                    f"  #{p.position_id} {p.strategy} | {p.canonical_asset} | "
                    f"RiseX:{p.risex_side} / {p.hedge_venue}:{p.hedge_side} | "
                    f"${p.notional_usd:,.0f} | {p.spread_bps:.1f}bps"
                )

        # Strategy breakdown
        strategy_pnl: dict[str, float] = {}
        strategy_count: dict[str, int] = {}
        for t in self.closed_trades:
            strategy_pnl[t.strategy] = strategy_pnl.get(t.strategy, 0.0) + t.realized_pnl
            strategy_count[t.strategy] = strategy_count.get(t.strategy, 0) + 1
        if strategy_pnl:
            lines.append("")
            lines.append("By strategy:")
            for strat in sorted(strategy_pnl):
                lines.append(
                    f"  {strat}: {strategy_count[strat]} trades, "
                    f"PnL ${strategy_pnl[strat]:.4f}"
                )

        self._notify("\n".join(lines))

    def _notify(self, text: str) -> None:
        print(text, flush=True)
        if not self.config.telegram_enabled:
            return
        result = self.notifier.send(text)
        if result.status != "sent":
            print(f"[telegram {result.status}] {result.error or ''}", flush=True)

    def _strategy_list(self) -> str:
        parts = []
        if self.config.funding_carry_enabled:
            parts.append("funding_carry")
        if self.config.volume_farming_enabled:
            parts.append("volume_farm")
        if self.config.spread_arb_enabled:
            parts.append("spread_arb")
        return ", ".join(parts) or "none"

    def summary(self) -> dict[str, Any]:
        open_positions = [p for p in self.positions if p.status == "open"]
        wins = sum(1 for t in self.closed_trades if t.realized_pnl >= 0)
        losses = sum(1 for t in self.closed_trades if t.realized_pnl < 0)
        return {
            "model_version": RISEX_BOT_MODEL_VERSION,
            "starting_balance": self.config.starting_balance,
            "current_balance": self.balance,
            "return_pct": (self.balance / self.config.starting_balance - 1) * 100,
            "realized_pnl": self.total_realized_pnl,
            "total_fees": self.total_fees_paid,
            "total_funding": self.total_funding_earned,
            "total_volume": self.total_volume,
            "scan_count": self.scan_count,
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
                    "risex_side": p.risex_side,
                    "hedge_venue": p.hedge_venue,
                    "hedge_side": p.hedge_side,
                    "notional": p.notional_usd,
                    "spread_bps": p.spread_bps,
                    "opened_at": p.opened_at,
                }
                for p in open_positions
            ],
        }
