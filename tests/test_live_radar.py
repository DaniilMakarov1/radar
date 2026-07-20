from __future__ import annotations

import unittest
from urllib.parse import parse_qs, urlparse

from smart_money_radar.ingestion.market import GoPlusClient, normalize_goplus_risk
from smart_money_radar.ingestion.social import social_query, social_silence_score
from smart_money_radar.live_radar import attach_wallet_context, build_signal


class LiveRadarScoringTest(unittest.TestCase):
    def test_clean_liquid_accumulation_becomes_candidate_but_not_high(self) -> None:
        signal = build_signal(clean_observation(), dataset_target_count=20)

        self.assertEqual(signal["status"], "candidate")
        self.assertEqual(signal["signal_level"], "medium")
        self.assertLessEqual(signal["confidence_score"], 65)
        self.assertNotIn("x_social_coverage", signal["evidence"]["missing_features"])
        self.assertFalse(signal["evidence"]["x_social_gate_enabled"])

    def test_missing_x_coverage_is_ignored_in_onchain_mode(self) -> None:
        observation = clean_observation()
        observation["social_x_available"] = False
        observation["social_silence_score"] = None

        signal = build_signal(observation, dataset_target_count=20)

        self.assertEqual(signal["status"], "candidate")
        self.assertNotIn("x_social_coverage", signal["evidence"]["missing_features"])
        self.assertNotIn("social_silence", signal["evidence"]["missing_features"])

    def test_missing_x_coverage_needs_review_when_gate_is_enabled(self) -> None:
        observation = clean_observation()
        observation["social_x_available"] = False

        signal = build_signal(
            observation,
            dataset_target_count=20,
            require_x_social=True,
        )

        self.assertEqual(signal["status"], "needs_review")
        self.assertIn("x_social_coverage", signal["evidence"]["missing_features"])

    def test_moderate_flow_concentration_is_visible_but_can_pass(self) -> None:
        observation = clean_observation()
        observation["top_wallet_net_buy_share"] = 0.7
        observation["top_cluster_net_buy_share"] = 0.7
        observation["residual_independent_net_buy_usd"] = 14_400

        signal = build_signal(observation, dataset_target_count=20)

        self.assertEqual(signal["status"], "candidate")
        self.assertIn("wallet_flow_concentration", signal["risk_flags"])
        self.assertIn("cluster_flow_concentration", signal["risk_flags"])

    def test_severe_flow_concentration_is_filtered(self) -> None:
        observation = clean_observation()
        observation["top_wallet_net_buy_share"] = 0.85
        observation["top_cluster_net_buy_share"] = 0.85
        observation["residual_independent_net_buy_usd"] = 7_200

        signal = build_signal(observation, dataset_target_count=20)

        self.assertEqual(signal["status"], "filtered")
        self.assertIn("severe_flow_concentration", signal["risk_flags"])

    def test_linked_wallets_count_as_one_cluster_vote(self) -> None:
        observation = clean_observation()
        observation["cluster_count"] = 2
        observation["effective_wallet_count"] = 2
        observation["cluster_independence_ratio"] = 0.5

        signal = build_signal(observation, dataset_target_count=20)

        self.assertEqual(signal["status"], "filtered")
        self.assertIn("too_few_independent_clusters", signal["risk_flags"])

    def test_unknown_identity_coverage_cannot_vote_as_independent(self) -> None:
        observation = clean_observation()
        observation["identity_flow_coverage"] = 0.5
        observation["cluster_independence_available"] = False
        observation["flow_concentration_available"] = False

        signal = build_signal(observation, dataset_target_count=20)

        self.assertEqual(signal["status"], "filtered")
        self.assertIn("identity_coverage_incomplete", signal["risk_flags"])

    def test_stale_wallet_flow_is_filtered(self) -> None:
        observation = clean_observation()
        observation["last_trade_at"] = "2025-12-20T00:00:00+00:00"

        signal = build_signal(observation, dataset_target_count=20)

        self.assertEqual(signal["status"], "filtered")
        self.assertIn("stale_wallet_flow", signal["risk_flags"])

    def test_wallet_context_aggregates_flow_by_cluster(self) -> None:
        addresses = ["0x" + digit * 40 for digit in ("1", "2", "3")]
        observation = {
            "tracked_wallet_count": 3,
            "evidence": {
                "accumulating_wallet_addresses": addresses,
                "accumulating_wallet_flows": [
                    {"wallet_address": addresses[0], "net_buy_usd": 50},
                    {"wallet_address": addresses[1], "net_buy_usd": 25},
                    {"wallet_address": addresses[2], "net_buy_usd": 25},
                ],
            },
        }

        result = attach_wallet_context(FakeWalletContextStore(addresses), observation)

        self.assertEqual(result["effective_wallet_count"], 2)
        self.assertEqual(result["top_wallet_net_buy_share"], 0.5)
        self.assertEqual(result["top_cluster_net_buy_share"], 0.75)
        self.assertEqual(result["residual_independent_net_buy_usd"], 25)

    def test_missing_website_is_filtered(self) -> None:
        observation = clean_observation()
        observation["website_url"] = None

        signal = build_signal(observation, dataset_target_count=20)

        self.assertEqual(signal["status"], "filtered")
        self.assertIn("website_missing", signal["risk_flags"])

    def test_honeypot_is_filtered(self) -> None:
        observation = clean_observation()
        observation["is_honeypot"] = 1
        observation["risk_score"] = 100
        observation["risk_flags"] = ["honeypot"]

        signal = build_signal(observation, dataset_target_count=20)

        self.assertEqual(signal["status"], "filtered")

    def test_missing_contract_risk_needs_review(self) -> None:
        observation = clean_observation()
        observation["risk_score"] = None

        signal = build_signal(observation, dataset_target_count=20)

        self.assertEqual(signal["status"], "needs_review")
        self.assertIn("contract_risk_missing", signal["risk_flags"])

    def test_unverified_blockscout_contract_needs_review(self) -> None:
        observation = clean_observation()
        observation["blockscout_contract_verified"] = False

        signal = build_signal(observation, dataset_target_count=20)

        self.assertEqual(signal["status"], "needs_review")
        self.assertIn("blockscout_unverified_contract", signal["risk_flags"])

    def test_blockscout_scam_reputation_is_filtered(self) -> None:
        observation = clean_observation()
        observation["blockscout_is_scam"] = True
        observation["blockscout_reputation"] = "scam"

        signal = build_signal(observation, dataset_target_count=20)

        self.assertEqual(signal["status"], "filtered")
        self.assertIn("blockscout_scam_reputation", signal["risk_flags"])

    def test_moralis_concentrated_holders_are_filtered(self) -> None:
        observation = clean_observation()
        observation["moralis_top10_holder_ratio"] = 0.75
        observation["moralis_top10_eoa_holder_ratio"] = 0.65

        signal = build_signal(observation, dataset_target_count=20)

        self.assertEqual(signal["status"], "filtered")
        self.assertIn("concentrated_ownership", signal["risk_flags"])

    def test_goplus_checks_every_base_contract(self) -> None:
        http = SingleAddressGoPlusHttp()
        addresses = ["0x" + "1" * 40, "0x" + "2" * 40]

        snapshots = GoPlusClient(http=http).token_risk_snapshots(
            "base",
            addresses,
            observed_at="2026-01-01T00:00:00+00:00",
        )

        self.assertEqual(len(snapshots), 2)
        self.assertEqual(len(http.calls), 2)

    def test_goplus_normalization_penalizes_high_risk_contract(self) -> None:
        snapshot = normalize_goplus_risk(
            chain_id="base",
            token_address="0x" + "1" * 40,
            observed_at="2026-01-01T00:00:00+00:00",
            raw={
                "is_honeypot": "0",
                "is_open_source": "0",
                "is_proxy": "1",
                "is_mintable": "1",
                "sell_tax": "0.15",
                "buy_tax": "0.02",
                "holders": [{"percent": "0.6"}],
            },
        )

        self.assertGreaterEqual(snapshot["risk_score"], 70)
        self.assertIn("high_sell_tax", snapshot["flags"])
        self.assertIn("high_top10_concentration", snapshot["flags"])

    def test_social_proxy_scores_silence_without_claiming_x_coverage(self) -> None:
        self.assertEqual(social_query("OpenGradient", "OPG"), '"OpenGradient"')
        self.assertEqual(social_silence_score(0, 0), 100.0)
        self.assertLess(social_silence_score(10, 80) or 100, 100)


def clean_observation() -> dict[str, object]:
    return {
        "chain_id": "base",
        "token_address": "0x" + "1" * 40,
        "token_symbol": "TEST",
        "market_token_symbol": "TEST",
        "market_token_name": "Test Token",
        "observed_at": "2026-01-01T00:00:00+00:00",
        "last_trade_at": "2025-12-31T18:00:00+00:00",
        "window_hours": 336,
        "tracked_wallet_count": 4,
        "strong_wallet_count": 1,
        "watch_wallet_count": 3,
        "gross_buy_usd": 50_000,
        "gross_sell_usd": 2_000,
        "net_buy_usd": 48_000,
        "weighted_wallet_score": 70,
        "liquidity_usd": 500_000,
        "volume_24h_usd": 100_000,
        "market_cap_usd": 5_000_000,
        "fdv_usd": 5_000_000,
        "pair_created_at": "2025-12-01T00:00:00+00:00",
        "boosts_active": 0,
        "website_url": "https://example.com",
        "social_links": [{"platform": "twitter", "handle": "example"}],
        "risk_score": 10,
        "risk_flags": [],
        "is_honeypot": 0,
        "is_open_source": 1,
        "buy_tax": 0,
        "sell_tax": 0,
        "holder_count": 1_000,
        "top_holder_ratio": 0.25,
        "blockscout_contract_verified": True,
        "blockscout_is_contract": True,
        "blockscout_is_scam": False,
        "blockscout_reputation": "ok",
        "blockscout_proxy_type": None,
        "blockscout_implementations": [],
        "blockscout_holder_count": 980,
        "blockscout_flags": [],
        "moralis_top10_holder_ratio": 0.25,
        "moralis_top10_eoa_holder_ratio": 0.18,
        "moralis_top10_contract_holder_ratio": 0.07,
        "moralis_labeled_holder_ratio": 0.05,
        "moralis_flags": [],
        "hypersync_inbound_wallet_count": 4,
        "hypersync_outbound_wallet_count": 1,
        "hypersync_transfer_count": 12,
        "hypersync_last_activity_at": "2025-12-31T19:00:00+00:00",
        "hypersync_flags": [],
        "social_silence_score": 85,
        "social_x_available": True,
        "cluster_count": 4,
        "effective_wallet_count": 4,
        "cluster_independence_available": True,
        "identity_flow_coverage": 1.0,
        "cluster_independence_ratio": 1.0,
        "flow_concentration_available": True,
        "positive_wallet_flow_usd": 48_000,
        "top_wallet_net_buy_share": 0.35,
        "top_cluster_net_buy_share": 0.35,
        "residual_independent_net_buy_usd": 31_200,
    }


class SingleAddressGoPlusHttp:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def get_json(self, url: str) -> dict[str, object]:
        self.calls.append(url)
        address = parse_qs(urlparse(url).query)["contract_addresses"][0]
        return {
            "code": 1,
            "result": {
                address.upper(): {
                    "is_honeypot": "0",
                    "is_open_source": "1",
                    "buy_tax": "0",
                    "sell_tax": "0",
                }
            },
        }


class FakeWalletContextStore:
    def __init__(self, addresses: list[str]) -> None:
        self.addresses = addresses

    def wallet_context_for_addresses(
        self,
        addresses: list[str],
        model_version: str,
    ) -> list[dict[str, object]]:
        del addresses, model_version
        return [
            {
                "wallet_address": self.addresses[0],
                "cluster_id": "cluster-a",
                "entity_checked": True,
                "cluster_evidence": {"independence_status": "supported"},
            },
            {
                "wallet_address": self.addresses[1],
                "cluster_id": "cluster-a",
                "entity_checked": True,
                "cluster_evidence": {"independence_status": "supported"},
            },
            {
                "wallet_address": self.addresses[2],
                "cluster_id": "cluster-b",
                "entity_checked": True,
                "cluster_evidence": {"independence_status": "supported"},
            },
        ]


if __name__ == "__main__":
    unittest.main()
