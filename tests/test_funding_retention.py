from pathlib import Path
from tempfile import TemporaryDirectory
from datetime import UTC, datetime, timedelta
import unittest

import smart_money_radar.funding.retention as retention_mod
from smart_money_radar.funding.retention import (
    apply_funding_retention_plan,
    build_funding_retention_plan,
    delete_funding_routes,
    refresh_funding_history_sync_state,
)
from smart_money_radar.storage import SQLiteStore


class FundingRetentionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.store = SQLiteStore(Path(self.directory.name) / "radar.sqlite")
        self.store.init_db()

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_prunes_old_scan_diagnostics_and_simulated_executions(self) -> None:
        with self.store.connect() as connection:
            seed_instrument(connection)
            scan_one = seed_scan(connection, "2026-01-01T00:00:00+00:00")
            scan_two = seed_scan(connection, "2026-01-02T00:00:00+00:00")
            scan_three = seed_scan(connection, "2026-01-03T00:00:00+00:00")
            route_one = seed_route(connection, scan_one, "route-one")
            seed_route(connection, scan_two, "route-two")
            seed_route(connection, scan_three, "route-three")
            seed_scan_children(connection, scan_one)
            seed_scan_children(connection, scan_two)
            seed_scan_children(connection, scan_three)
            seed_paper_execution(connection, scan_one, route_one)

        plan = build_funding_retention_plan(self.store, keep_latest_scans=1)

        self.assertEqual(plan.delete_scan_ids, [2, 1])
        self.assertEqual(plan.rows_by_table["funding_routes"], 2)
        self.assertEqual(plan.rows_by_table["funding_market_snapshots"], 2)
        self.assertEqual(plan.rows_by_table["funding_paper_executions"], 1)

        result = apply_funding_retention_plan(self.store, keep_latest_scans=1)

        self.assertEqual(result["deleted_rows"]["funding_scans"], 2)
        self.assertEqual(result["deleted_rows"]["funding_paper_executions"], 1)
        with self.store.connect() as connection:
            remaining_scans = [
                row["funding_scan_id"]
                for row in connection.execute(
                    "SELECT funding_scan_id FROM funding_scans ORDER BY funding_scan_id"
                )
            ]
            remaining_routes = [
                row["route_key"]
                for row in connection.execute(
                    "SELECT route_key FROM funding_routes ORDER BY route_key"
                )
            ]
            paper_count = connection.execute(
                "SELECT COUNT(*) FROM funding_paper_executions"
            ).fetchone()[0]

        self.assertEqual(remaining_scans, [3])
        self.assertEqual(remaining_routes, ["route-three"])
        self.assertEqual(paper_count, 0)

    def test_skips_stale_paper_execution_route_ids(self) -> None:
        with self.store.connect() as connection:
            seed_instrument(connection)
            scan_id = seed_scan(connection, "2026-01-01T00:00:00+00:00")
            route_id = seed_route(connection, scan_id, "route-one")

        inserted = self.store.insert_funding_paper_executions(
            scan_id,
            [
                {
                    "funding_route_id": route_id,
                    "model_version": "test",
                    "status": "filled",
                    "requested_notional": 500,
                    "filled_notional": 500,
                    "fill_ratio": 1,
                    "latency_ms": 1000,
                    "expected_net_profit": 1.2,
                    "repriced_net_profit": 1.1,
                    "result": {},
                },
                {
                    "funding_route_id": route_id + 10_000,
                    "model_version": "test",
                    "status": "stale",
                    "requested_notional": 500,
                    "filled_notional": 0,
                    "fill_ratio": 0,
                    "latency_ms": 1000,
                    "expected_net_profit": 1.2,
                    "repriced_net_profit": None,
                    "result": {},
                },
            ],
        )

        self.assertEqual(inserted, 1)
        with self.store.connect() as connection:
            paper_rows = connection.execute(
                """
                SELECT funding_route_id, status
                FROM funding_paper_executions
                """
            ).fetchall()
        self.assertEqual(len(paper_rows), 1)
        self.assertEqual(paper_rows[0]["funding_route_id"], route_id)
        self.assertEqual(paper_rows[0]["status"], "filled")

    def test_paper_event_detaches_stale_foreign_keys(self) -> None:
        event_id = self.store.insert_funding_paper_event(
            {
                "event_type": "disarmed",
                "severity": "warning",
                "funding_paper_position_id": 123_456,
                "funding_scan_id": 123_457,
                "funding_route_id": 123_458,
                "route_key": "stale-route",
                "message": "stale route",
                "payload": {"route_key": "stale-route"},
                "telegram_status": "not_sent",
            }
        )

        with self.store.connect() as connection:
            row = connection.execute(
                """
                SELECT funding_paper_position_id, funding_scan_id,
                       funding_route_id, route_key
                FROM funding_paper_events
                WHERE funding_paper_event_id = ?
                """,
                (event_id,),
            ).fetchone()

        self.assertIsNone(row["funding_paper_position_id"])
        self.assertIsNone(row["funding_scan_id"])
        self.assertIsNone(row["funding_route_id"])
        self.assertEqual(row["route_key"], "stale-route")

    def test_routine_paper_event_does_not_keep_scan_and_route(self) -> None:
        with self.store.connect() as connection:
            seed_instrument(connection)
            scan_one = seed_scan(connection, "2026-01-01T00:00:00+00:00")
            scan_two = seed_scan(connection, "2026-01-02T00:00:00+00:00")
            scan_three = seed_scan(connection, "2026-01-03T00:00:00+00:00")
            route_one = seed_route(connection, scan_one, "route-one")
            seed_route(connection, scan_two, "route-two")
            seed_route(connection, scan_three, "route-three")
            seed_scan_children(connection, scan_one)
            seed_scan_children(connection, scan_two)
            seed_scan_children(connection, scan_three)
            seed_paper_event(connection, scan_one, route_one, event_type="scan")

        plan = build_funding_retention_plan(self.store, keep_latest_scans=1)

        self.assertEqual(plan.delete_scan_ids, [2, 1])
        self.assertEqual(plan.rows_by_table["funding_routes"], 2)

        result = apply_funding_retention_plan(self.store, keep_latest_scans=1)

        self.assertEqual(result["deleted_rows"]["funding_scans"], 2)
        with self.store.connect() as connection:
            remaining_scans = [
                row["funding_scan_id"]
                for row in connection.execute(
                    "SELECT funding_scan_id FROM funding_scans ORDER BY funding_scan_id"
                )
            ]
            remaining_routes = [
                row["route_key"]
                for row in connection.execute(
                    "SELECT route_key FROM funding_routes ORDER BY route_key"
                )
            ]

        self.assertEqual(remaining_scans, [3])
        self.assertEqual(remaining_routes, ["route-three"])

    def test_focused_recheck_does_not_evict_latest_full_scan(self) -> None:
        with self.store.connect() as connection:
            seed_instrument(connection)
            full_scan = seed_scan(
                connection,
                "2026-01-01T00:00:00+00:00",
                config_json='{"scan_mode":"watch","horizon_mode":"next_settlement"}',
            )
            focused_scan = seed_scan(
                connection,
                "2026-01-01T00:01:00+00:00",
                config_json=(
                    '{"scan_mode":"watch","horizon_mode":"next_settlement",'
                    '"focused_route_key":"route-one"}'
                ),
            )
            seed_route(connection, full_scan, "full-route")
            seed_route(connection, focused_scan, "focused-route")
            seed_scan_children(connection, full_scan)
            seed_scan_children(connection, focused_scan)

        plan = build_funding_retention_plan(self.store, keep_latest_scans=1)

        self.assertEqual(plan.delete_scan_ids, [])
        self.assertEqual(
            set(plan.protected_scan_ids),
            {full_scan, focused_scan},
        )

    def test_recent_running_scan_is_never_pruned(self) -> None:
        with self.store.connect() as connection:
            seed_instrument(connection)
            old_scan = seed_scan(connection, "2026-01-01T00:00:00+00:00")
            running_scan = seed_running_scan(
                connection,
                datetime.now(UTC).isoformat(timespec="seconds"),
            )
            latest_scan = seed_scan(connection, "2026-01-03T00:00:00+00:00")
            seed_scan_children(connection, old_scan)
            seed_scan_children(connection, running_scan)
            seed_scan_children(connection, latest_scan)

        plan = build_funding_retention_plan(self.store, keep_latest_scans=1)

        self.assertIn(old_scan, plan.delete_scan_ids)
        self.assertNotIn(running_scan, plan.delete_scan_ids)
        self.assertNotIn(latest_scan, plan.delete_scan_ids)

        result = apply_funding_retention_plan(self.store, keep_latest_scans=1)

        self.assertEqual(result["deleted_rows"]["funding_scans"], 1)
        with self.store.connect() as connection:
            remaining_scans = [
                row["funding_scan_id"]
                for row in connection.execute(
                    "SELECT funding_scan_id FROM funding_scans ORDER BY funding_scan_id"
                )
            ]

        self.assertEqual(remaining_scans, [running_scan, latest_scan])

    def test_stale_running_scan_is_marked_failed_and_pruned(self) -> None:
        stale_started_at = (
            datetime.now(UTC) - timedelta(hours=2)
        ).isoformat(timespec="seconds")
        with self.store.connect() as connection:
            seed_instrument(connection)
            stale_scan = seed_running_scan(connection, stale_started_at)
            latest_scan = seed_scan(
                connection,
                datetime.now(UTC).isoformat(timespec="seconds"),
            )
            seed_scan_children(connection, stale_scan)
            seed_scan_children(connection, latest_scan)

        plan = build_funding_retention_plan(
            self.store,
            keep_latest_scans=1,
            stale_running_scan_seconds=3_600,
        )
        self.assertIn(stale_scan, plan.delete_scan_ids)

        result = apply_funding_retention_plan(
            self.store,
            keep_latest_scans=1,
            stale_running_scan_seconds=3_600,
        )

        self.assertEqual(result["stale_running_scans_marked"], 1)
        self.assertEqual(result["deleted_rows"]["funding_scans"], 1)
        with self.store.connect() as connection:
            remaining_scans = [
                row["funding_scan_id"]
                for row in connection.execute(
                    "SELECT funding_scan_id FROM funding_scans ORDER BY funding_scan_id"
                )
            ]
        self.assertEqual(remaining_scans, [latest_scan])

    def test_keeps_position_linked_scan_and_route(self) -> None:
        with self.store.connect() as connection:
            seed_instrument(connection)
            scan_one = seed_scan(connection, "2026-01-01T00:00:00+00:00")
            scan_two = seed_scan(connection, "2026-01-02T00:00:00+00:00")
            scan_three = seed_scan(connection, "2026-01-03T00:00:00+00:00")
            route_one = seed_route(connection, scan_one, "route-one")
            seed_route(connection, scan_two, "route-two")
            seed_route(connection, scan_three, "route-three")
            seed_scan_children(connection, scan_one)
            seed_scan_children(connection, scan_two)
            seed_scan_children(connection, scan_three)
            seed_paper_position(connection, scan_one, route_one)

        plan = build_funding_retention_plan(self.store, keep_latest_scans=1)

        self.assertEqual(plan.delete_scan_ids, [2])
        self.assertEqual(plan.rows_by_table["funding_routes"], 1)

        result = apply_funding_retention_plan(self.store, keep_latest_scans=1)

        self.assertEqual(result["deleted_rows"]["funding_scans"], 1)
        with self.store.connect() as connection:
            remaining_scans = [
                row["funding_scan_id"]
                for row in connection.execute(
                    "SELECT funding_scan_id FROM funding_scans ORDER BY funding_scan_id"
                )
            ]
            remaining_routes = [
                row["route_key"]
                for row in connection.execute(
                    "SELECT route_key FROM funding_routes ORDER BY route_key"
                )
            ]

        self.assertEqual(remaining_scans, [1, 3])
        self.assertEqual(remaining_routes, ["route-one", "route-three"])

    def test_position_event_route_link_does_not_block_scan_retention(self) -> None:
        with self.store.connect() as connection:
            seed_instrument(connection)
            event_scan = seed_scan(connection, "2026-01-01T00:00:00+00:00")
            latest_scan = seed_scan(connection, "2026-01-02T00:00:00+00:00")
            event_route = seed_route(connection, event_scan, "event-route")
            latest_route = seed_route(connection, latest_scan, "latest-route")
            seed_scan_children(connection, event_scan)
            seed_scan_children(connection, latest_scan)
            position_id = seed_paper_position(connection, latest_scan, latest_route)
            seed_position_paper_event(
                connection,
                position_id,
                event_scan,
                event_route,
                event_type="hold",
            )

        result = apply_funding_retention_plan(self.store, keep_latest_scans=1)

        self.assertEqual(result["deleted_rows"]["funding_scans"], 1)
        self.assertEqual(result["deleted_rows"]["funding_routes"], 1)
        with self.store.connect() as connection:
            event = connection.execute(
                """
                SELECT funding_paper_position_id, funding_scan_id, funding_route_id
                FROM funding_paper_events
                WHERE event_type = 'hold'
                """
            ).fetchone()
            remaining_routes = [
                row["route_key"]
                for row in connection.execute(
                    "SELECT route_key FROM funding_routes ORDER BY route_key"
                )
            ]

        self.assertEqual(event["funding_paper_position_id"], position_id)
        self.assertIsNone(event["funding_scan_id"])
        self.assertIsNone(event["funding_route_id"])
        self.assertEqual(remaining_routes, ["latest-route"])

    def test_delete_funding_routes_skips_position_linked_routes(self) -> None:
        with self.store.connect() as connection:
            seed_instrument(connection)
            scan_id = seed_scan(connection, "2026-01-01T00:00:00+00:00")
            protected_route = seed_route(connection, scan_id, "protected-route")
            disposable_route = seed_route(connection, scan_id, "disposable-route")
            position_id = seed_paper_position(connection, scan_id, protected_route)
            seed_position_paper_event(
                connection,
                position_id,
                scan_id,
                disposable_route,
                event_type="disarmed",
            )

            deleted = delete_funding_routes(
                connection,
                [protected_route, disposable_route],
            )
            remaining_routes = [
                row["route_key"]
                for row in connection.execute(
                    "SELECT route_key FROM funding_routes ORDER BY route_key"
                )
            ]
            detached_event = connection.execute(
                """
                SELECT funding_route_id
                FROM funding_paper_events
                WHERE event_type = 'disarmed'
                """
            ).fetchone()

        self.assertEqual(deleted, 1)
        self.assertEqual(remaining_routes, ["protected-route"])
        self.assertIsNone(detached_event["funding_route_id"])

    def test_prunes_funding_rate_history_by_age_with_latest_floor(self) -> None:
        with self.store.connect() as connection:
            seed_instrument(connection)
            now = datetime.now(UTC)
            rows = []
            for index in range(3):
                funding_at = (
                    now - timedelta(days=30, hours=3 - index)
                ).isoformat(timespec="seconds")
                rows.append(
                    (
                        "binance",
                        "BTCUSDT",
                        funding_at,
                        0.0001,
                        1.0,
                        0.0001,
                        funding_at,
                        "{}",
                    )
                )
            for index in range(5):
                funding_at = (
                    now - timedelta(hours=5 - index)
                ).isoformat(timespec="seconds")
                rows.append(
                    (
                        "binance",
                        "BTCUSDT",
                        funding_at,
                        0.0001,
                        1.0,
                        0.0001,
                        funding_at,
                        "{}",
                    )
                )
            connection.executemany(
                """
                INSERT INTO funding_rate_history (
                    venue, symbol, funding_at, funding_rate,
                    funding_interval_hours, hourly_funding_rate, observed_at, raw_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            sorted_funding_at = sorted(row[2] for row in rows)
            connection.execute(
                """
                INSERT INTO funding_history_sync_state (
                    venue, symbol, requested_start_at, fetched_at, row_count,
                    earliest_funding_at, latest_funding_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "binance",
                    "BTCUSDT",
                    sorted_funding_at[0],
                    now.isoformat(timespec="seconds"),
                    len(rows),
                    sorted_funding_at[0],
                    sorted_funding_at[-1],
                ),
            )
            refresh_funding_history_sync_state(connection)

        plan = build_funding_retention_plan(
            self.store,
            keep_latest_scans=20,
            keep_latest_history_per_market=2,
            keep_history_older_than_seconds=7 * 86_400,
        )

        self.assertEqual(plan.rows_by_table["funding_rate_history"], 3)

        result = apply_funding_retention_plan(
            self.store,
            keep_latest_scans=20,
            keep_latest_history_per_market=2,
            keep_history_older_than_seconds=7 * 86_400,
        )

        self.assertEqual(result["deleted_rows"]["funding_rate_history"], 3)
        with self.store.connect() as connection:
            remaining = [
                row["funding_at"]
                for row in connection.execute(
                    """
                    SELECT funding_at
                    FROM funding_rate_history
                    ORDER BY funding_at
                    """
                )
            ]
            sync_row = dict(
                connection.execute(
                    """
                    SELECT row_count, earliest_funding_at, latest_funding_at
                    FROM funding_history_sync_state
                    WHERE venue = 'binance' AND symbol = 'BTCUSDT'
                    """
                ).fetchone()
            )

        self.assertEqual(len(remaining), 5)
        self.assertTrue(all(funding_at > (datetime.now(UTC) - timedelta(days=7)).isoformat(timespec="seconds") for funding_at in remaining))
        self.assertEqual(sync_row["row_count"], 5)
        self.assertEqual(sync_row["earliest_funding_at"], remaining[0])
        self.assertEqual(sync_row["latest_funding_at"], remaining[-1])


def test_apply_retention_rechecks_paper_linked_scans_at_apply_time(
    tmp_path,
    monkeypatch,
) -> None:
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    with store.connect() as connection:
        seed_instrument(connection)
        old_scan = seed_scan(connection, "2026-01-01T00:00:00+00:00")
        latest_scan = seed_scan(connection, "2026-01-02T00:00:00+00:00")
        old_route = seed_route(connection, old_scan, "old-route")
        seed_route(connection, latest_scan, "latest-route")
        seed_scan_children(connection, old_scan)
        seed_scan_children(connection, latest_scan)

    stale_plan = retention_mod.build_funding_retention_plan(
        store,
        keep_latest_scans=1,
    )
    assert stale_plan.delete_scan_ids == [old_scan]

    with store.connect() as connection:
        seed_paper_position(connection, old_scan, old_route)

    monkeypatch.setattr(
        retention_mod,
        "build_funding_retention_plan",
        lambda *args, **kwargs: stale_plan,
    )

    result = retention_mod.apply_funding_retention_plan(
        store,
        keep_latest_scans=1,
    )

    assert result["deleted_rows"]["funding_scans"] == 0
    assert result["applied_delete_scan_ids"] == []
    assert result["skipped_protected_scan_ids"] == [old_scan]
    with store.connect() as connection:
        remaining_scans = [
            row["funding_scan_id"]
            for row in connection.execute(
                "SELECT funding_scan_id FROM funding_scans ORDER BY funding_scan_id"
            )
        ]
        remaining_routes = [
            row["route_key"]
            for row in connection.execute(
                "SELECT route_key FROM funding_routes ORDER BY route_key"
            )
        ]

    assert remaining_scans == [old_scan, latest_scan]
    assert remaining_routes == ["latest-route", "old-route"]


def seed_instrument(connection) -> None:
    connection.execute(
        """
        INSERT INTO funding_instruments (
            venue, symbol, canonical_asset, base_asset, quote_asset,
            collateral_asset, contract_type, status, observed_at, updated_at
        )
        VALUES (
            'binance', 'BTCUSDT', 'BTC', 'BTC', 'USDT',
            'USDT', 'perp', 'trading',
            '2026-01-01T00:00:00+00:00',
            '2026-01-01T00:00:00+00:00'
        )
        """
    )


def seed_scan(
    connection,
    started_at: str,
    config_json: str = '{"scan_mode":"manual"}',
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO funding_scans (status, started_at, finished_at, config_json)
        VALUES ('success', ?, ?, ?)
        """,
        (started_at, started_at, config_json),
    )
    return int(cursor.lastrowid)


def seed_running_scan(
    connection,
    started_at: str,
    config_json: str = '{"scan_mode":"manual"}',
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO funding_scans (status, started_at, config_json)
        VALUES ('running', ?, ?)
        """,
        (started_at, config_json),
    )
    return int(cursor.lastrowid)


def seed_route(connection, scan_id: int, route_key: str) -> int:
    cursor = connection.execute(
        """
        INSERT INTO funding_routes (
            funding_scan_id, route_key, route_type, canonical_asset, venue_scope,
            long_venue, long_symbol, short_venue, short_symbol, status,
            horizon_days, observed_at
        )
        VALUES (
            ?, ?, 'venue_pair', 'BTC', 'cross_venue',
            'binance', 'BTCUSDT', 'okx', 'BTC-USDT-SWAP',
            'paper_candidate', 1, '2026-01-01T00:00:00+00:00'
        )
        """,
        (scan_id, route_key),
    )
    return int(cursor.lastrowid)


def seed_scan_children(connection, scan_id: int) -> None:
    connection.execute(
        """
        INSERT INTO funding_market_snapshots (
            funding_scan_id, venue, symbol, canonical_asset, funding_rate,
            funding_interval_hours, hourly_funding_rate, funding_rate_kind,
            observed_at
        )
        VALUES (?, 'binance', 'BTCUSDT', 'BTC', 0.01, 8, 0.00125, 'current', ?)
        """,
        (scan_id, "2026-01-01T00:00:00+00:00"),
    )
    connection.execute(
        """
        INSERT INTO funding_orderbook_snapshots (
            funding_scan_id, venue, symbol, observed_at
        )
        VALUES (?, 'binance', 'BTCUSDT', ?)
        """,
        (scan_id, "2026-01-01T00:00:00+00:00"),
    )
    connection.execute(
        """
        INSERT INTO funding_route_universe (
            funding_scan_id, canonical_asset, long_venue, long_symbol,
            short_venue, short_symbol, current_hourly_spread, quick_gross_rate,
            quick_taker_cost_rate, quick_maker_cost_rate,
            quick_best_case_net_rate, execution_screen_reason, observed_at
        )
        VALUES (
            ?, 'BTC', 'binance', 'BTCUSDT', 'okx', 'BTC-USDT-SWAP',
            0.001, 0.001, 0.0002, 0.0001, 0.0008, 'eligible', ?
        )
        """,
        (scan_id, "2026-01-01T00:00:00+00:00"),
    )


def seed_paper_execution(connection, scan_id: int, route_id: int) -> None:
    connection.execute(
        """
        INSERT INTO funding_paper_executions (
            funding_route_id, funding_scan_id, model_version, status, created_at,
            requested_notional, filled_notional, fill_ratio, latency_ms
        )
        VALUES (
            ?, ?, 'test', 'filled', '2026-01-01T00:00:00+00:00',
            500, 500, 1, 1000
        )
        """,
        (route_id, scan_id),
    )


def seed_paper_event(
    connection,
    scan_id: int,
    route_id: int,
    *,
    event_type: str = "open",
) -> None:
    connection.execute(
        """
        INSERT INTO funding_paper_events (
            created_at, event_type, severity, funding_scan_id,
            funding_route_id, message, payload_json, telegram_status
        )
        VALUES (
            '2026-01-01T00:00:00+00:00',
            ?, 'info', ?, ?, 'test event', '{}', 'not_sent'
        )
        """,
        (event_type, scan_id, route_id),
    )


def seed_position_paper_event(
    connection,
    position_id: int,
    scan_id: int,
    route_id: int,
    *,
    event_type: str = "hold",
) -> None:
    connection.execute(
        """
        INSERT INTO funding_paper_events (
            created_at, event_type, severity, funding_paper_position_id,
            funding_scan_id, funding_route_id, message, payload_json,
            telegram_status
        )
        VALUES (
            '2026-01-01T00:00:00+00:00',
            ?, 'info', ?, ?, ?, 'position event', '{}', 'not_sent'
        )
        """,
        (event_type, position_id, scan_id, route_id),
    )


def seed_paper_position(connection, scan_id: int, route_id: int) -> int:
    cursor = connection.execute(
        """
        INSERT INTO funding_paper_positions (
            entry_key, route_key, status, opened_at,
            open_funding_scan_id, open_funding_route_id,
            canonical_asset, long_venue, long_symbol, short_venue, short_symbol,
            base_quantity, target_notional, long_notional, short_notional,
            long_reserved_margin, short_reserved_margin,
            expected_live_gross, expected_live_net, expected_execution_cost
        )
        VALUES (
            'entry-one', 'route-one', 'open', '2026-01-01T00:00:00+00:00',
            ?, ?, 'BTC', 'binance', 'BTCUSDT', 'okx', 'BTC-USDT-SWAP',
            0.01, 500, 500, 500, 500, 500, 1, 1, 0
        )
        """,
        (scan_id, route_id),
    )
    return int(cursor.lastrowid)


def seed_funding_history(connection, row_count: int) -> None:
    rows = []
    for index in range(row_count):
        funding_at = f"2026-01-01T{index:02d}:00:00+00:00"
        rows.append(
            (
                "binance",
                "BTCUSDT",
                funding_at,
                0.0001,
                1.0,
                0.0001,
                funding_at,
                "{}",
            )
        )
    connection.executemany(
        """
        INSERT INTO funding_rate_history (
            venue, symbol, funding_at, funding_rate,
            funding_interval_hours, hourly_funding_rate, observed_at, raw_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    connection.execute(
        """
        INSERT INTO funding_history_sync_state (
            venue, symbol, requested_start_at, fetched_at, row_count,
            earliest_funding_at, latest_funding_at
        )
        VALUES (
            'binance', 'BTCUSDT',
            '2026-01-01T00:00:00+00:00',
            '2026-01-01T04:00:00+00:00',
            ?, '2026-01-01T00:00:00+00:00',
            '2026-01-01T04:00:00+00:00'
        )
        """,
        (row_count,),
    )


def test_closed_position_pnl_survives_retention(tmp_path) -> None:
    """Retention must never delete paper positions or accounts."""
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    with store.connect() as connection:
        seed_instrument(connection)
        old_scan = seed_scan(connection, "2026-01-01T00:00:00+00:00")
        latest_scan = seed_scan(connection, "2026-01-02T00:00:00+00:00")
        old_route = seed_route(connection, old_scan, "old-route")
        seed_route(connection, latest_scan, "latest-route")
        seed_scan_children(connection, old_scan)
        seed_scan_children(connection, latest_scan)
        position_id = seed_paper_position(connection, old_scan, old_route)
        connection.execute(
            """
            UPDATE funding_paper_positions
            SET status = 'closed', closed_at = '2026-01-01T08:00:00+00:00',
                close_funding_scan_id = ?, close_funding_route_id = ?,
                actual_funding_pnl = 5.0, actual_execution_cost = 1.5,
                actual_net_pnl = 3.5, close_reason = 'test'
            WHERE funding_paper_position_id = ?
            """,
            (latest_scan, old_route, position_id),
        )
        connection.execute(
            """
            INSERT INTO funding_paper_accounts (
                venue, starting_balance, cash_balance, reserved_margin,
                realized_pnl, updated_at
            )
            VALUES ('binance', 1000, 1003.5, 0, 3.5, '2026-01-01T08:00:00+00:00')
            """
        )
        connection.execute(
            """
            INSERT INTO funding_paper_balance_ledger (
                created_at, venue, funding_paper_position_id, event_type,
                cash_delta, reserved_delta, cash_balance_after,
                reserved_margin_after, payload_json
            )
            VALUES (
                '2026-01-01T08:00:00+00:00', 'binance', ?, 'close',
                3.5, 0, 1003.5, 0, '{}'
            )
            """,
            (position_id,),
        )

    apply_funding_retention_plan(store, keep_latest_scans=1)

    with store.connect() as connection:
        position = connection.execute(
            "SELECT * FROM funding_paper_positions WHERE funding_paper_position_id = ?",
            (position_id,),
        ).fetchone()
        account = connection.execute(
            "SELECT * FROM funding_paper_accounts WHERE venue = 'binance'"
        ).fetchone()
        ledger = connection.execute(
            "SELECT * FROM funding_paper_balance_ledger WHERE funding_paper_position_id = ?",
            (position_id,),
        ).fetchone()
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()

    assert position is not None
    assert position["status"] == "closed"
    assert float(position["actual_net_pnl"]) == 3.5
    assert position["open_funding_scan_id"] == old_scan
    assert position["open_funding_route_id"] == old_route
    assert account is not None
    assert float(account["realized_pnl"]) == 3.5
    assert ledger is not None
    assert violations == []


def test_fk_integrity_after_retention(tmp_path) -> None:
    """No FK violations may remain after retention runs."""
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    with store.connect() as connection:
        seed_instrument(connection)
        scans = []
        for day in range(1, 6):
            scan_id = seed_scan(connection, f"2026-01-{day:02d}T00:00:00+00:00")
            route_id = seed_route(connection, scan_id, f"route-{day}")
            seed_scan_children(connection, scan_id)
            seed_paper_execution(connection, scan_id, route_id)
            seed_paper_event(connection, scan_id, route_id, event_type="scan")
            scans.append((scan_id, route_id))
        position_id = seed_paper_position(connection, scans[0][0], scans[0][1])
        seed_position_paper_event(
            connection, position_id, scans[1][0], scans[1][1], event_type="open"
        )

    apply_funding_retention_plan(store, keep_latest_scans=1)

    with store.connect() as connection:
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        remaining_scans = connection.execute(
            "SELECT COUNT(*) FROM funding_scans"
        ).fetchone()[0]
        remaining_positions = connection.execute(
            "SELECT COUNT(*) FROM funding_paper_positions"
        ).fetchone()[0]

    assert violations == []
    assert remaining_scans >= 1
    assert remaining_positions == 1


def test_trade_events_preserved_during_routine_pruning(tmp_path) -> None:
    """Pruning routine events must not delete trade events (open/close)."""
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    with store.connect() as connection:
        seed_instrument(connection)
        scan_id = seed_scan(connection, "2026-01-01T00:00:00+00:00")
        route_id = seed_route(connection, scan_id, "route-one")
        for i in range(5):
            seed_paper_event(connection, scan_id, route_id, event_type="scan")
        seed_paper_event(connection, scan_id, route_id, event_type="open")
        seed_paper_event(connection, scan_id, route_id, event_type="close")

    apply_funding_retention_plan(
        store,
        keep_latest_scans=20,
        keep_latest_routine_paper_events=2,
    )

    with store.connect() as connection:
        trade_events = connection.execute(
            "SELECT event_type FROM funding_paper_events WHERE event_type IN ('open', 'close')"
        ).fetchall()
        routine_events = connection.execute(
            "SELECT COUNT(*) FROM funding_paper_events WHERE event_type = 'scan'"
        ).fetchone()[0]

    assert len(trade_events) == 2
    assert routine_events == 2


def test_equity_snapshot_pruning(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    with store.connect() as connection:
        seed_instrument(connection)
        seed_scan(connection, "2026-01-01T00:00:00+00:00")
        for i in range(10):
            connection.execute(
                """
                INSERT INTO funding_paper_equity_snapshots (
                    observed_at, total_cash, total_reserved_margin,
                    total_equity, realized_pnl, open_position_count,
                    closed_position_count, payload_json
                )
                VALUES (?, 1000, 0, 1000, 0, 0, 0, '{}')
                """,
                (f"2026-01-01T{i:02d}:00:00+00:00",),
            )

    apply_funding_retention_plan(
        store,
        keep_latest_scans=20,
        keep_latest_equity_snapshots=3,
    )

    with store.connect() as connection:
        remaining = connection.execute(
            "SELECT COUNT(*) FROM funding_paper_equity_snapshots"
        ).fetchone()[0]

    assert remaining == 3


if __name__ == "__main__":
    unittest.main()
