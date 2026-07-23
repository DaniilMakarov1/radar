from __future__ import annotations

from dataclasses import dataclass

from smart_money_radar.config import FUNDING_MINIMUM_ACTIONABLE_NOTIONAL


@dataclass(frozen=True)
class FundingScanConfig:
    target_notional: float = 10_000.0
    horizon_mode: str = "fixed"
    horizon_hours: float | None = None
    max_candidates: int | None = None
    near_miss_full_depth_routes: int | None = None
    history_days: int = 90
    history_refresh_hours: float = 1.0
    max_live_history_markets: int | None = 24
    minimum_history_points: int = 24
    minimum_history_coverage: float = 0.8
    maximum_history_age_hours: float = 24.0
    maximum_nowcast_age_seconds: int = 120
    minimum_market_capacity: float = FUNDING_MINIMUM_ACTIONABLE_NOTIONAL
    depth_haircut: float = 0.75
    margin_fraction_per_leg: float = 1.0
    collateral_reserve_fraction: float = 0.2
    basis_reserve_bps: float = 5.0
    maximum_basis_gap_bps: float = 500.0
    maximum_research_basis_gap_bps: float = 2_000.0
    large_basis_threshold_bps: float = 50.0
    basis_stress_fraction: float = 0.25
    operations_buffer_bps: float = 2.0
    minimum_persistence: float = 0.55
    minimum_net_profit: float = 1.0
    minimum_net_return_bps: float = 0.0
    minimum_profit_probability: float = 0.70
    minimum_forward_samples: int = 20
    minimum_settlement_lead_seconds: int = 0
    conservative_quantile: float = 0.25
    paper_latency_ms: int = 1_000
    maker_fill_probability: float = 0.55
    maker_timeout_penalty_bps: float = 4.0
    liquidity_sequence_limit: int = 60
    minimum_liquidity_snapshots: int = 6
    maker_timeout_seconds: int = 120
    minimum_maker_observations: int = 10
    max_full_depth_orderbook_markets: int | None = None
    orderbook_cache_ttl_seconds: int = 0
    market_snapshot_cache_ttl_seconds: int = 0
    store_diagnostic_raw_json: bool = False
    adaptive_near_miss_min_score: float = 55.0
    adaptive_near_miss_min_cost_coverage: float = 0.35
    adaptive_near_miss_emergency_floor_bps: float = 2.0
    adaptive_near_miss_emergency_cost_multiplier: float = 0.75
    adaptive_near_miss_near_break_even_coverage: float = 0.65
    adaptive_near_miss_liquidity_score: float = 0.70
    adaptive_near_miss_urgency_score: float = 0.35

    def validated(self) -> "FundingScanConfig":
        mode = str(self.horizon_mode or "fixed").strip().lower()
        if mode not in {"next_settlement", "fixed", "research"}:
            mode = "fixed"
        requested_hours = float(self.horizon_hours or 24.0)
        if mode == "next_settlement":
            normalized_hours = 0.0
        elif mode == "research":
            normalized_hours = 72.0
        else:
            normalized_hours = max(1.0, min(requested_hours, 24.0))
        return FundingScanConfig(
            target_notional=max(
                FUNDING_MINIMUM_ACTIONABLE_NOTIONAL,
                min(float(self.target_notional), 1_000_000.0),
            ),
            horizon_mode=mode,
            horizon_hours=normalized_hours,
            max_candidates=(
                None
                if self.max_candidates is None or int(self.max_candidates) <= 0
                else int(self.max_candidates)
            ),
            near_miss_full_depth_routes=(
                None
                if self.near_miss_full_depth_routes is None
                else max(0, int(self.near_miss_full_depth_routes))
            ),
            history_days=max(30, min(int(self.history_days), 90)),
            history_refresh_hours=max(0.25, min(float(self.history_refresh_hours), 48.0)),
            max_live_history_markets=(
                None
                if self.max_live_history_markets is None
                else max(0, int(self.max_live_history_markets))
            ),
            minimum_history_points=max(2, int(self.minimum_history_points)),
            minimum_history_coverage=max(0.1, min(float(self.minimum_history_coverage), 1.0)),
            maximum_history_age_hours=max(1.0, min(float(self.maximum_history_age_hours), 72.0)),
            maximum_nowcast_age_seconds=max(
                10,
                min(int(self.maximum_nowcast_age_seconds), 900),
            ),
            minimum_market_capacity=max(
                FUNDING_MINIMUM_ACTIONABLE_NOTIONAL,
                float(self.minimum_market_capacity),
            ),
            depth_haircut=max(0.1, min(float(self.depth_haircut), 1.0)),
            margin_fraction_per_leg=max(0.1, min(float(self.margin_fraction_per_leg), 1.0)),
            collateral_reserve_fraction=max(0.0, min(float(self.collateral_reserve_fraction), 2.0)),
            basis_reserve_bps=max(0.0, min(float(self.basis_reserve_bps), 500.0)),
            maximum_basis_gap_bps=max(1.0, min(float(self.maximum_basis_gap_bps), 1_000.0)),
            maximum_research_basis_gap_bps=max(
                1.0,
                min(float(self.maximum_research_basis_gap_bps), 2_000.0),
            ),
            large_basis_threshold_bps=max(
                1.0,
                min(float(self.large_basis_threshold_bps), 1_000.0),
            ),
            basis_stress_fraction=max(
                0.05,
                min(float(self.basis_stress_fraction), 1.0),
            ),
            operations_buffer_bps=max(0.0, min(float(self.operations_buffer_bps), 100.0)),
            minimum_persistence=max(0.0, min(float(self.minimum_persistence), 1.0)),
            minimum_net_profit=max(0.0, float(self.minimum_net_profit)),
            minimum_net_return_bps=max(
                0.0,
                min(float(self.minimum_net_return_bps), 1_000.0),
            ),
            minimum_profit_probability=max(
                0.5,
                min(float(self.minimum_profit_probability), 0.99),
            ),
            minimum_forward_samples=max(5, int(self.minimum_forward_samples)),
            minimum_settlement_lead_seconds=max(
                0,
                min(int(self.minimum_settlement_lead_seconds), 3_600),
            ),
            conservative_quantile=max(
                0.05,
                min(float(self.conservative_quantile), 0.5),
            ),
            paper_latency_ms=max(0, int(self.paper_latency_ms)),
            maker_fill_probability=max(
                0.0,
                min(float(self.maker_fill_probability), 1.0),
            ),
            maker_timeout_penalty_bps=max(
                0.0,
                min(float(self.maker_timeout_penalty_bps), 100.0),
            ),
            liquidity_sequence_limit=max(
                10,
                min(int(self.liquidity_sequence_limit), 360),
            ),
            minimum_liquidity_snapshots=max(
                2,
                min(int(self.minimum_liquidity_snapshots), 100),
            ),
            maker_timeout_seconds=max(
                10,
                min(int(self.maker_timeout_seconds), 900),
            ),
            minimum_maker_observations=max(
                3,
                min(int(self.minimum_maker_observations), 100),
            ),
            max_full_depth_orderbook_markets=(
                None
                if self.max_full_depth_orderbook_markets is None
                or int(self.max_full_depth_orderbook_markets) <= 0
                else max(25, int(self.max_full_depth_orderbook_markets))
            ),
            orderbook_cache_ttl_seconds=max(
                0,
                min(int(self.orderbook_cache_ttl_seconds), 120),
            ),
            market_snapshot_cache_ttl_seconds=max(
                0,
                min(int(self.market_snapshot_cache_ttl_seconds), 60),
            ),
            store_diagnostic_raw_json=bool(self.store_diagnostic_raw_json),
            adaptive_near_miss_min_score=max(
                0.0,
                min(float(self.adaptive_near_miss_min_score), 100.0),
            ),
            adaptive_near_miss_min_cost_coverage=max(
                0.0,
                min(float(self.adaptive_near_miss_min_cost_coverage), 1.0),
            ),
            adaptive_near_miss_emergency_floor_bps=max(
                0.0,
                min(float(self.adaptive_near_miss_emergency_floor_bps), 200.0),
            ),
            adaptive_near_miss_emergency_cost_multiplier=max(
                0.0,
                min(float(self.adaptive_near_miss_emergency_cost_multiplier), 5.0),
            ),
            adaptive_near_miss_near_break_even_coverage=max(
                0.0,
                min(float(self.adaptive_near_miss_near_break_even_coverage), 1.0),
            ),
            adaptive_near_miss_liquidity_score=max(
                0.0,
                min(float(self.adaptive_near_miss_liquidity_score), 1.0),
            ),
            adaptive_near_miss_urgency_score=max(
                0.0,
                min(float(self.adaptive_near_miss_urgency_score), 1.0),
            ),
        )

    @property
    def research_only(self) -> bool:
        return self.horizon_mode == "research"


DEFAULT_TAKER_FEE_RATES = {
    "aevo": 0.0008,
    "aster": 0.0004,
    "backpack": 0.0005,
    "bingx": 0.0005,
    "binance": 0.0005,
    "bitmart": 0.0006,
    "bitunix": 0.0006,
    "bitget": 0.0006,
    "blofin": 0.0006,
    "bybit": 0.00055,
    "coinex": 0.0005,
    "dydx": 0.0006,
    "ethereal": 0.0003,
    "gate": 0.00075,
    "htx": 0.0005,
    "hyperliquid": 0.0005,
    "deribit": 0.0005,
    "extended": 0.00025,
    "kraken": 0.0005,
    "kucoin": 0.0006,
    "lighter": 0.0,
    "mexc": 0.0002,
    "okx": 0.0005,
    "paradex": 0.0002,
    "phemex": 0.0006,
    "drift": 0.001,
    "vertex_base": 0.0003,
    "woox": 0.0005,
}


DEFAULT_MAKER_FEE_RATES = {
    "aevo": 0.0005,
    "aster": 0.0,
    "backpack": 0.0002,
    "bingx": 0.0002,
    "binance": 0.0002,
    "bitmart": 0.0002,
    "bitunix": 0.0002,
    "bitget": 0.0002,
    "blofin": 0.0002,
    "bybit": 0.0002,
    "coinex": 0.0003,
    "dydx": 0.0001,
    "ethereal": 0.0,
    "gate": 0.0002,
    "htx": 0.0002,
    "hyperliquid": 0.0002,
    "deribit": 0.0,
    "extended": 0.0,
    "kraken": 0.0002,
    "kucoin": 0.0002,
    "lighter": 0.0,
    "mexc": 0.0,
    "okx": 0.0002,
    "paradex": 0.0,
    "phemex": 0.0001,
    "drift": 0.0,
    "vertex_base": 0.0,
    "woox": 0.0002,
}
