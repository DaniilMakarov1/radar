from __future__ import annotations

from typing import Any

from smart_money_radar.bots.strategies.base import Opportunity
from smart_money_radar.funding.adapters.base import FundingDataError, FundingVenueClient
from smart_money_radar.funding.models import FundingScanConfig
from smart_money_radar.funding.scanner import (
    execution_shortlist,
    rank_perp_pairs,
    scan_ranked_pairs,
)


class FundingCarryStrategy:
    """Funding-rate carry: reuse the funding scanner pipeline, filter to
    routes involving a target venue, and convert to opportunities."""

    name = "funding_carry"

    def __init__(
        self,
        target_venue: str,
        clients: dict[str, FundingVenueClient],
        scan_config: FundingScanConfig,
    ) -> None:
        self.target_venue = target_venue
        self.clients = clients
        self.scan_config = scan_config

    def scan(self, observed_at: str) -> list[Opportunity]:
        all_markets: list[dict[str, Any]] = []
        for client in self.clients.values():
            try:
                _, markets, _ = client.catalog_and_markets(observed_at)
                all_markets.extend(markets)
            except FundingDataError:
                continue

        if not all_markets:
            return []

        candidates = rank_perp_pairs(
            all_markets,
            maximum=None,
            config=self.scan_config,
            observed_at=observed_at,
        )

        target_candidates = [
            c
            for c in candidates
            if str(c.get("long_market", {}).get("venue")) == self.target_venue
            or str(c.get("short_market", {}).get("venue")) == self.target_venue
        ]
        if not target_candidates:
            return []

        shortlist = execution_shortlist(target_candidates, 0, config=self.scan_config)
        if not shortlist:
            return []

        books: dict[tuple[str, str], dict[str, Any]] = {}
        candidate_markets: dict[tuple[str, str], dict[str, Any]] = {}
        for candidate in shortlist:
            for market in (candidate["long_market"], candidate["short_market"]):
                key = (str(market["venue"]), str(market["symbol"]))
                candidate_markets[key] = market

        for key in candidate_markets:
            venue_name, symbol = key
            client = self.clients.get(venue_name)
            if client is None:
                continue
            try:
                books[key] = client.orderbook(symbol, observed_at)
            except FundingDataError:
                continue

        routes = scan_ranked_pairs(shortlist, books, {}, observed_at, self.scan_config)

        return [
            self._route_to_opportunity(r)
            for r in routes
            if r.get("status") == "paper_candidate"
        ]

    def _route_to_opportunity(self, route: dict[str, Any]) -> Opportunity:
        long_venue = str(route.get("long_venue", ""))
        short_venue = str(route.get("short_venue", ""))
        legs = route.get("legs", [])

        if long_venue == self.target_venue:
            primary_side, hedge_side = "long", "short"
            hedge_venue = short_venue
            primary_leg = legs[0] if legs else {}
            hedge_leg = legs[1] if len(legs) > 1 else {}
        else:
            primary_side, hedge_side = "short", "long"
            hedge_venue = long_venue
            primary_leg = legs[1] if len(legs) > 1 else {}
            hedge_leg = legs[0] if legs else {}

        evidence = route.get("evidence", {})
        horizon = evidence.get("horizon", {})

        return Opportunity(
            strategy=self.name,
            canonical_asset=str(route.get("canonical_asset", "")),
            primary_side=primary_side,
            hedge_venue=hedge_venue,
            hedge_side=hedge_side,
            notional=float(route.get("target_notional", 0)),
            spread_bps=float(route.get("current_hourly_spread", 0)) * 10_000,
            net_profit=float(evidence.get("current_nowcast_net", 0)),
            total_cost=float(evidence.get("execution_cost", 0)),
            gross_profit=float(evidence.get("current_nowcast_gross", 0)),
            primary_price=float(primary_leg.get("mark_price", 0)),
            hedge_price=float(hedge_leg.get("mark_price", 0)),
            primary_funding_rate=float(primary_leg.get("hourly_funding_rate", 0)),
            hedge_funding_rate=float(hedge_leg.get("hourly_funding_rate", 0)),
            primary_symbol=str(primary_leg.get("symbol", "")),
            hedge_symbol=str(hedge_leg.get("symbol", "")),
            settlement_at=horizon.get("authorization_valid_until"),
            confidence_score=float(route.get("confidence_score", 0)),
            blocking_reasons=evidence.get("blocking_reasons", []),
            extra={"primary_venue": self.target_venue, "route": route},
        )
