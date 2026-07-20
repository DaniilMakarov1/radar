# Dune Backfill Runbook V4

## Current State (July 13, 2026)

- Billing reset supplied 2,500 credits, not an unlimited allowance.
- Base V3 execution `01KXEAYGHRD3FBMJDRWANSWDPY` completed with 510,890 rows.
- That execution is audit-only: it has only four follow-up weeks and cannot support
  honest 60/90-day outcomes.
- No partial V3 export was imported into Research Validity.
- API export attempts reached 2,499.346 / 2,500 credits. Do not start another Dune
  execution until credits are added or the period resets on August 13, 2026.
- V4 is prepared locally with 13 follow-up weeks, a safe `$200k / 20 trades /
  10 traders` seed threshold, and a 14-column export contract.

## Safety Rule

Do not start a Dune execution until `dune-usage` confirms that the billing-period
allowance has reset. The preparation command below only writes SQL and a manifest;
it never calls Dune.

```bash
python3 -m smart_money_radar.cli prepare-dune-backfill
```

Prepared manifest:

```text
exports/dune_backfill_manifest_v4.json
```

## Next Dune Window

Run one stage at a time and inspect its output before continuing.

```bash
python3 -m smart_money_radar.cli dune-usage
python3 -m smart_money_radar.cli research-universe-probe --chain base
python3 -m smart_money_radar.cli research-universe-backfill --chain base
python3 -m smart_money_radar.cli research-status
```

The V4 universe contains completed weeks only. It adds thirteen explicit
follow-up weeks after a qualifying token-week, so 30/60/90-day zero activity is
observed rather than inferred from a missing row. Sparse or missing future data
remains `unknown`.

After an execution completes, inspect its export budget before any manual fetch:

```bash
python3 -m smart_money_radar.cli dune-export-plan \
  --execution-id EXECUTION_ID \
  --columns-profile universe
```

The automated backfill performs the same fail-closed check. It downloads no rows
when projected export cost plus reserve exceeds the remaining allowance.

Then build the wallet denominator in two controlled passes:

```bash
python3 -m smart_money_radar.cli wallet-opportunity-backfill --chain base --max-wallets 50
python3 -m smart_money_radar.cli wallet-opportunity-backfill --chain base --max-wallets 200
python3 -m smart_money_radar.cli wallet-weekly-flows --chain base --max-wallets 200
python3 -m smart_money_radar.cli identity-graph-v2 --max-wallets 200
python3 -m smart_money_radar.cli research-status
```

Only after those imports should the model gate be evaluated:

```bash
python3 -m smart_money_radar.cli train-research-models --chain base
python3 -m smart_money_radar.cli research-status
```

## Stop Conditions

- Dune usage has not reset.
- Export plan is not affordable with a 10% reserve.
- The threshold probe indicates an unexpectedly large or tiny universe.
- An export is incomplete or pagination fails.
- Identity coverage is below 95%.
- Fewer than 100,000 snapshots or 100 independent listing events are available.
- Validation has fewer than 20 independent positive events or 12 snapshot dates.

Stopping is a valid result. The system must remain `research_only` until every
capital gate is satisfied.

## Cross-Chain Sequence

After Base is internally consistent, repeat EVM research in this order:

```bash
python3 -m smart_money_radar.cli verify-evm-contracts --chain bsc
python3 -m smart_money_radar.cli research-universe-probe --chain bsc
python3 -m smart_money_radar.cli research-universe-backfill --chain bsc
python3 -m smart_money_radar.cli cross-chain-history --chain bsc

python3 -m smart_money_radar.cli verify-evm-contracts --chain ethereum
python3 -m smart_money_radar.cli research-universe-probe --chain ethereum
python3 -m smart_money_radar.cli research-universe-backfill --chain ethereum
python3 -m smart_money_radar.cli cross-chain-history --chain ethereum
```

Solana remains a separate schema and pipeline:

```bash
python3 -m smart_money_radar.cli solana-history --with-universe
```

Do not launch all networks together. Each stage must pass row-count, time-bound,
contract-mapping, and label-maturity checks before the next network is added.
