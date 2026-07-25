PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS chains (
    chain_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    ecosystem TEXT NOT NULL,
    explorer_name TEXT NOT NULL,
    explorer_url TEXT NOT NULL,
    api_env_var TEXT,
    live_priority INTEGER NOT NULL,
    research_enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS raw_binance_announcements (
    article_id INTEGER PRIMARY KEY,
    article_code TEXT NOT NULL UNIQUE,
    catalog_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    release_ts_ms INTEGER NOT NULL,
    release_at TEXT NOT NULL,
    source_url TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    list_payload_json TEXT NOT NULL,
    detail_payload_json TEXT,
    body_text TEXT,
    body_links_json TEXT
);

CREATE TABLE IF NOT EXISTS binance_announcements (
    announcement_id INTEGER PRIMARY KEY,
    article_code TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    release_at TEXT NOT NULL,
    source_url TEXT NOT NULL,
    category TEXT NOT NULL,
    is_spot_listing INTEGER NOT NULL DEFAULT 0,
    is_futures_listing INTEGER NOT NULL DEFAULT 0,
    is_alpha_or_airdrop INTEGER NOT NULL DEFAULT 0,
    requires_manual_review INTEGER NOT NULL DEFAULT 1,
    extracted_symbols_json TEXT NOT NULL DEFAULT '[]',
    trading_pairs_json TEXT NOT NULL DEFAULT '[]',
    contract_links_json TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL,
    FOREIGN KEY (announcement_id) REFERENCES raw_binance_announcements(article_id)
);

CREATE INDEX IF NOT EXISTS idx_raw_binance_release_at
    ON raw_binance_announcements (release_at);

CREATE INDEX IF NOT EXISTS idx_binance_announcements_release_at
    ON binance_announcements (release_at);

CREATE INDEX IF NOT EXISTS idx_binance_announcements_category
    ON binance_announcements (category);

CREATE TABLE IF NOT EXISTS tokens (
    token_id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL UNIQUE,
    name TEXT,
    first_seen_at TEXT,
    first_source TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tokens_symbol
    ON tokens (symbol);

CREATE TABLE IF NOT EXISTS listing_events (
    listing_event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    announcement_id INTEGER NOT NULL,
    token_id INTEGER NOT NULL,
    exchange TEXT NOT NULL DEFAULT 'binance',
    event_type TEXT NOT NULL,
    announced_at TEXT NOT NULL,
    source_url TEXT NOT NULL,
    trading_pairs_json TEXT NOT NULL DEFAULT '[]',
    mapping_status TEXT NOT NULL,
    confidence_score REAL NOT NULL,
    is_backtest_target INTEGER NOT NULL DEFAULT 0,
    requires_manual_review INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (announcement_id) REFERENCES binance_announcements(announcement_id),
    FOREIGN KEY (token_id) REFERENCES tokens(token_id),
    UNIQUE (announcement_id, token_id, event_type)
);

CREATE INDEX IF NOT EXISTS idx_listing_events_announced_at
    ON listing_events (announced_at);

CREATE INDEX IF NOT EXISTS idx_listing_events_token_id
    ON listing_events (token_id);

CREATE INDEX IF NOT EXISTS idx_listing_events_backtest_target
    ON listing_events (is_backtest_target);

CREATE TABLE IF NOT EXISTS token_contracts (
    token_contract_id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_id INTEGER NOT NULL,
    chain_id TEXT NOT NULL,
    contract_address TEXT NOT NULL,
    explorer_url TEXT NOT NULL,
    source TEXT NOT NULL,
    confidence_score REAL NOT NULL,
    mapping_status TEXT NOT NULL,
    first_seen_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (token_id) REFERENCES tokens(token_id),
    FOREIGN KEY (chain_id) REFERENCES chains(chain_id),
    UNIQUE (token_id, chain_id, contract_address)
);

CREATE INDEX IF NOT EXISTS idx_token_contracts_chain_address
    ON token_contracts (chain_id, contract_address);

CREATE TABLE IF NOT EXISTS pre_listing_wallet_buys (
    pre_listing_wallet_buy_id INTEGER PRIMARY KEY AUTOINCREMENT,
    chain_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    token_address TEXT NOT NULL,
    announced_at TEXT NOT NULL,
    wallet_address TEXT NOT NULL,
    first_buy_at TEXT NOT NULL,
    last_buy_at TEXT NOT NULL,
    buy_trade_count INTEGER NOT NULL,
    gross_buy_usd REAL NOT NULL,
    source TEXT NOT NULL,
    execution_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (chain_id) REFERENCES chains(chain_id),
    UNIQUE (chain_id, symbol, token_address, announced_at, wallet_address)
);

CREATE INDEX IF NOT EXISTS idx_pre_listing_wallet_buys_wallet
    ON pre_listing_wallet_buys (wallet_address);

CREATE INDEX IF NOT EXISTS idx_pre_listing_wallet_buys_symbol
    ON pre_listing_wallet_buys (symbol, announced_at);

CREATE INDEX IF NOT EXISTS idx_pre_listing_wallet_buys_gross_buy_usd
    ON pre_listing_wallet_buys (gross_buy_usd DESC);

CREATE TABLE IF NOT EXISTS wallet_holding_metrics (
    wallet_holding_metric_id INTEGER PRIMARY KEY AUTOINCREMENT,
    chain_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    token_address TEXT NOT NULL,
    announced_at TEXT NOT NULL,
    wallet_address TEXT NOT NULL,
    pre_buy_usd REAL NOT NULL,
    pre_sell_usd REAL NOT NULL,
    post_buy_usd REAL NOT NULL,
    post_sell_usd REAL NOT NULL,
    pre_buy_trades INTEGER NOT NULL,
    pre_sell_trades INTEGER NOT NULL,
    post_buy_trades INTEGER NOT NULL,
    post_sell_trades INTEGER NOT NULL,
    first_buy_at TEXT,
    last_buy_at TEXT,
    first_sell_at TEXT,
    last_sell_at TEXT,
    net_pre_usd REAL NOT NULL,
    pre_sell_ratio REAL NOT NULL,
    post_sell_ratio REAL NOT NULL,
    holding_label TEXT NOT NULL,
    source TEXT NOT NULL,
    execution_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (chain_id) REFERENCES chains(chain_id),
    UNIQUE (chain_id, symbol, token_address, announced_at, wallet_address)
);

CREATE INDEX IF NOT EXISTS idx_wallet_holding_metrics_wallet
    ON wallet_holding_metrics (wallet_address);

CREATE INDEX IF NOT EXISTS idx_wallet_holding_metrics_label
    ON wallet_holding_metrics (holding_label);

CREATE TABLE IF NOT EXISTS wallet_scores (
    wallet_score_id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet_address TEXT NOT NULL,
    chain_id TEXT NOT NULL,
    model_version TEXT NOT NULL,
    interest_score REAL NOT NULL,
    noise_score REAL NOT NULL,
    confidence_score REAL NOT NULL,
    label TEXT NOT NULL,
    target_count INTEGER NOT NULL,
    symbol_count INTEGER NOT NULL,
    total_buy_trades INTEGER NOT NULL,
    total_gross_buy_usd REAL NOT NULL,
    avg_trade_usd REAL NOT NULL,
    earliest_buy_at TEXT NOT NULL,
    latest_buy_at TEXT NOT NULL,
    max_first_lead_days REAL NOT NULL,
    min_last_lead_hours REAL NOT NULL,
    active_span_days REAL NOT NULL,
    flags_json TEXT NOT NULL DEFAULT '[]',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (wallet_address, chain_id, model_version),
    FOREIGN KEY (chain_id) REFERENCES chains(chain_id)
);

CREATE INDEX IF NOT EXISTS idx_wallet_scores_interest
    ON wallet_scores (interest_score DESC);

CREATE INDEX IF NOT EXISTS idx_wallet_scores_label
    ON wallet_scores (label);

CREATE INDEX IF NOT EXISTS idx_wallet_scores_noise
    ON wallet_scores (noise_score DESC);

CREATE TABLE IF NOT EXISTS wallet_entities (
    chain_id TEXT NOT NULL,
    wallet_address TEXT NOT NULL,
    entity_type TEXT NOT NULL DEFAULT 'eoa',
    entity_label TEXT,
    is_contract INTEGER NOT NULL DEFAULT 0,
    is_exchange INTEGER NOT NULL DEFAULT 0,
    is_service INTEGER NOT NULL DEFAULT 0,
    excluded_from_research INTEGER NOT NULL DEFAULT 0,
    confidence_score REAL NOT NULL DEFAULT 0,
    source TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    checked_at TEXT NOT NULL,
    PRIMARY KEY (chain_id, wallet_address),
    FOREIGN KEY (chain_id) REFERENCES chains(chain_id)
);

CREATE INDEX IF NOT EXISTS idx_wallet_entities_excluded
    ON wallet_entities (chain_id, excluded_from_research, entity_type);

CREATE TABLE IF NOT EXISTS wallet_funding (
    chain_id TEXT NOT NULL,
    wallet_address TEXT NOT NULL,
    funder_address TEXT,
    funded_at TEXT,
    amount_native REAL,
    funder_label TEXT,
    funder_category TEXT,
    funder_cex TEXT,
    is_shared_service INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (chain_id, wallet_address),
    FOREIGN KEY (chain_id) REFERENCES chains(chain_id)
);

CREATE INDEX IF NOT EXISTS idx_wallet_funding_funder
    ON wallet_funding (chain_id, funder_address);

CREATE TABLE IF NOT EXISTS wallet_clusters (
    cluster_id TEXT NOT NULL,
    chain_id TEXT NOT NULL,
    model_version TEXT NOT NULL,
    member_count INTEGER NOT NULL,
    independence_score REAL NOT NULL,
    methods_json TEXT NOT NULL DEFAULT '[]',
    rationale_json TEXT NOT NULL DEFAULT '[]',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (cluster_id, model_version),
    FOREIGN KEY (chain_id) REFERENCES chains(chain_id)
);

CREATE TABLE IF NOT EXISTS wallet_cluster_members (
    cluster_id TEXT NOT NULL,
    model_version TEXT NOT NULL,
    chain_id TEXT NOT NULL,
    wallet_address TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'member',
    link_confidence REAL NOT NULL DEFAULT 0,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (cluster_id, model_version, wallet_address),
    FOREIGN KEY (cluster_id, model_version)
        REFERENCES wallet_clusters(cluster_id, model_version),
    FOREIGN KEY (chain_id) REFERENCES chains(chain_id)
);

CREATE INDEX IF NOT EXISTS idx_wallet_cluster_members_wallet
    ON wallet_cluster_members (chain_id, wallet_address, model_version);

CREATE VIEW IF NOT EXISTS backtest_targets AS
SELECT
    le.listing_event_id,
    t.symbol,
    t.name,
    le.exchange,
    le.event_type,
    le.announced_at,
    le.source_url,
    le.trading_pairs_json,
    le.mapping_status,
    le.confidence_score,
    le.requires_manual_review
FROM listing_events le
JOIN tokens t ON t.token_id = le.token_id
WHERE le.is_backtest_target = 1;

CREATE TABLE IF NOT EXISTS signals (
    signal_id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_symbol TEXT NOT NULL,
    token_name TEXT,
    chain_id TEXT NOT NULL,
    contract_address TEXT,
    signal_type TEXT NOT NULL,
    signal_level TEXT NOT NULL,
    confidence_score REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'candidate',
    detected_at TEXT NOT NULL,
    strong_wallet_count INTEGER NOT NULL DEFAULT 0,
    cluster_count INTEGER NOT NULL DEFAULT 0,
    net_buy_usd REAL,
    liquidity_usd REAL,
    social_silence_score REAL,
    risk_score REAL,
    thesis TEXT,
    risk_flags_json TEXT NOT NULL DEFAULT '[]',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL,
    FOREIGN KEY (chain_id) REFERENCES chains(chain_id)
);

CREATE INDEX IF NOT EXISTS idx_signals_detected_at
    ON signals (detected_at);

CREATE INDEX IF NOT EXISTS idx_signals_status
    ON signals (status);

CREATE INDEX IF NOT EXISTS idx_signals_chain_id
    ON signals (chain_id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_signals_unique_detection
    ON signals (chain_id, contract_address, signal_type, detected_at);

CREATE TABLE IF NOT EXISTS ingestion_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    records_seen INTEGER NOT NULL DEFAULT 0,
    records_written INTEGER NOT NULL DEFAULT 0,
    error TEXT
);

CREATE TABLE IF NOT EXISTS backtest_runs (
    backtest_run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_version TEXT NOT NULL,
    backtest_type TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    as_of TEXT NOT NULL,
    config_json TEXT NOT NULL DEFAULT '{}',
    metrics_json TEXT NOT NULL DEFAULT '{}',
    leakage_checks_json TEXT NOT NULL DEFAULT '{}',
    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_backtest_runs_started_at
    ON backtest_runs (started_at DESC);

CREATE TABLE IF NOT EXISTS backtest_evaluations (
    backtest_evaluation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    backtest_run_id INTEGER NOT NULL,
    snapshot_at TEXT NOT NULL,
    chain_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    token_address TEXT NOT NULL,
    history_target_count INTEGER NOT NULL,
    eligible_wallet_count INTEGER NOT NULL,
    predicted_wallet_count INTEGER NOT NULL,
    hit_wallet_count INTEGER NOT NULL,
    meaningful_buyer_count INTEGER NOT NULL,
    precision REAL,
    baseline_rate REAL,
    lift REAL,
    coverage REAL,
    median_hit_lead_days REAL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (backtest_run_id) REFERENCES backtest_runs(backtest_run_id),
    UNIQUE (backtest_run_id, chain_id, symbol, token_address, snapshot_at)
);

CREATE INDEX IF NOT EXISTS idx_backtest_evaluations_run
    ON backtest_evaluations (backtest_run_id);

CREATE TABLE IF NOT EXISTS backtest_token_candidates (
    backtest_token_candidate_id INTEGER PRIMARY KEY AUTOINCREMENT,
    backtest_run_id INTEGER NOT NULL,
    execution_id TEXT,
    snapshot_at TEXT NOT NULL,
    chain_id TEXT NOT NULL,
    positive_symbol TEXT NOT NULL,
    positive_token_address TEXT NOT NULL,
    token_symbol TEXT,
    token_address TEXT NOT NULL,
    is_positive INTEGER NOT NULL,
    cohort_wallet_count INTEGER NOT NULL,
    tracked_wallet_count INTEGER NOT NULL,
    gross_buy_usd REAL NOT NULL,
    gross_sell_usd REAL NOT NULL,
    net_buy_usd REAL NOT NULL,
    buy_trade_count INTEGER NOT NULL,
    sell_trade_count INTEGER NOT NULL,
    first_trade_at TEXT,
    last_trade_at TEXT,
    candidate_score REAL NOT NULL,
    candidate_rank INTEGER NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (backtest_run_id) REFERENCES backtest_runs(backtest_run_id),
    UNIQUE (backtest_run_id, snapshot_at, chain_id, token_address)
);

CREATE INDEX IF NOT EXISTS idx_backtest_token_candidates_run
    ON backtest_token_candidates (backtest_run_id, snapshot_at, candidate_rank);

CREATE TABLE IF NOT EXISTS radar_observations (
    radar_observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    chain_id TEXT NOT NULL,
    token_address TEXT NOT NULL,
    token_symbol TEXT,
    observed_at TEXT NOT NULL,
    window_hours INTEGER NOT NULL,
    tracked_wallet_count INTEGER NOT NULL,
    strong_wallet_count INTEGER NOT NULL DEFAULT 0,
    watch_wallet_count INTEGER NOT NULL DEFAULT 0,
    gross_buy_usd REAL NOT NULL,
    gross_sell_usd REAL NOT NULL,
    net_buy_usd REAL NOT NULL,
    buy_trade_count INTEGER NOT NULL,
    sell_trade_count INTEGER NOT NULL,
    first_trade_at TEXT,
    last_trade_at TEXT,
    weighted_wallet_score REAL,
    source TEXT NOT NULL,
    execution_id TEXT,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY (chain_id) REFERENCES chains(chain_id),
    UNIQUE (chain_id, token_address, observed_at, window_hours)
);

CREATE INDEX IF NOT EXISTS idx_radar_observations_recent
    ON radar_observations (observed_at DESC);

CREATE INDEX IF NOT EXISTS idx_radar_observations_token
    ON radar_observations (chain_id, token_address);

CREATE TABLE IF NOT EXISTS token_market_snapshots (
    token_market_snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    chain_id TEXT NOT NULL,
    token_address TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    source TEXT NOT NULL,
    token_name TEXT,
    token_symbol TEXT,
    pair_address TEXT,
    dex_id TEXT,
    price_usd REAL,
    liquidity_usd REAL,
    volume_24h_usd REAL,
    market_cap_usd REAL,
    fdv_usd REAL,
    pair_created_at TEXT,
    website_url TEXT,
    social_links_json TEXT NOT NULL DEFAULT '[]',
    boosts_active INTEGER,
    raw_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (chain_id) REFERENCES chains(chain_id),
    UNIQUE (chain_id, token_address, observed_at, source)
);

CREATE INDEX IF NOT EXISTS idx_token_market_snapshots_token
    ON token_market_snapshots (chain_id, token_address, observed_at DESC);

CREATE TABLE IF NOT EXISTS local_ingestion_cursors (
    local_ingestion_cursor_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    chain_id TEXT NOT NULL,
    cursor_key TEXT NOT NULL,
    block_number INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE (source, chain_id, cursor_key)
);

CREATE INDEX IF NOT EXISTS idx_local_ingestion_cursors_source
    ON local_ingestion_cursors (source, chain_id, cursor_key);

CREATE TABLE IF NOT EXISTS local_dex_trades (
    local_dex_trade_id INTEGER PRIMARY KEY AUTOINCREMENT,
    chain_id TEXT NOT NULL,
    project TEXT,
    dex_id TEXT,
    pair_address TEXT NOT NULL,
    token_address TEXT NOT NULL,
    token_symbol TEXT,
    wallet_address TEXT NOT NULL,
    tx_hash TEXT NOT NULL,
    log_index INTEGER NOT NULL,
    block_number INTEGER,
    block_time TEXT,
    side TEXT NOT NULL,
    amount_raw TEXT NOT NULL,
    amount_token REAL,
    amount_usd REAL,
    price_usd REAL,
    observed_at TEXT NOT NULL,
    window_hours INTEGER NOT NULL,
    source TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY (chain_id) REFERENCES chains(chain_id),
    UNIQUE (
        chain_id,
        tx_hash,
        log_index,
        token_address,
        wallet_address,
        source
    )
);

CREATE INDEX IF NOT EXISTS idx_local_dex_trades_snapshot
    ON local_dex_trades (source, observed_at DESC);

CREATE INDEX IF NOT EXISTS idx_local_dex_trades_token
    ON local_dex_trades (chain_id, token_address, observed_at DESC);

CREATE TABLE IF NOT EXISTS local_dex_rollups (
    local_dex_rollup_id INTEGER PRIMARY KEY AUTOINCREMENT,
    chain_id TEXT NOT NULL,
    token_address TEXT NOT NULL,
    token_symbol TEXT,
    observed_at TEXT NOT NULL,
    window_hours INTEGER NOT NULL,
    tracked_wallet_count INTEGER NOT NULL DEFAULT 0,
    strong_wallet_count INTEGER NOT NULL DEFAULT 0,
    watch_wallet_count INTEGER NOT NULL DEFAULT 0,
    gross_buy_usd REAL NOT NULL DEFAULT 0,
    gross_sell_usd REAL NOT NULL DEFAULT 0,
    net_buy_usd REAL NOT NULL DEFAULT 0,
    buy_trade_count INTEGER NOT NULL DEFAULT 0,
    sell_trade_count INTEGER NOT NULL DEFAULT 0,
    first_trade_at TEXT,
    last_trade_at TEXT,
    weighted_wallet_score REAL,
    source TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY (chain_id) REFERENCES chains(chain_id),
    UNIQUE (chain_id, token_address, observed_at, window_hours, source)
);

CREATE INDEX IF NOT EXISTS idx_local_dex_rollups_snapshot
    ON local_dex_rollups (source, observed_at DESC);

CREATE INDEX IF NOT EXISTS idx_local_dex_rollups_token
    ON local_dex_rollups (chain_id, token_address, observed_at DESC);

CREATE TABLE IF NOT EXISTS token_risk_snapshots (
    token_risk_snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    chain_id TEXT NOT NULL,
    token_address TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    source TEXT NOT NULL,
    risk_score REAL,
    is_honeypot INTEGER,
    is_open_source INTEGER,
    is_proxy INTEGER,
    is_mintable INTEGER,
    buy_tax REAL,
    sell_tax REAL,
    holder_count INTEGER,
    top_holder_ratio REAL,
    flags_json TEXT NOT NULL DEFAULT '[]',
    raw_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (chain_id) REFERENCES chains(chain_id),
    UNIQUE (chain_id, token_address, observed_at, source)
);

CREATE INDEX IF NOT EXISTS idx_token_risk_snapshots_token
    ON token_risk_snapshots (chain_id, token_address, observed_at DESC);

CREATE TABLE IF NOT EXISTS token_onchain_snapshots (
    token_onchain_snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    chain_id TEXT NOT NULL,
    token_address TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    source TEXT NOT NULL,
    contract_verified INTEGER,
    is_contract INTEGER,
    is_scam INTEGER,
    reputation TEXT,
    proxy_type TEXT,
    implementation_addresses_json TEXT NOT NULL DEFAULT '[]',
    holder_count INTEGER,
    top10_holder_ratio REAL,
    top10_eoa_holder_ratio REAL,
    top10_contract_holder_ratio REAL,
    labeled_holder_ratio REAL,
    inbound_wallet_count INTEGER,
    outbound_wallet_count INTEGER,
    transfer_count INTEGER,
    last_activity_at TEXT,
    flags_json TEXT NOT NULL DEFAULT '[]',
    raw_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (chain_id) REFERENCES chains(chain_id),
    UNIQUE (chain_id, token_address, observed_at, source)
);

CREATE INDEX IF NOT EXISTS idx_token_onchain_snapshots_token
    ON token_onchain_snapshots (
        chain_id,
        token_address,
        source,
        observed_at DESC
    );

CREATE TABLE IF NOT EXISTS token_social_snapshots (
    token_social_snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    chain_id TEXT NOT NULL,
    token_address TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    source TEXT NOT NULL,
    query_text TEXT NOT NULL,
    mentions_24h INTEGER,
    mentions_7d INTEGER,
    social_silence_score REAL,
    coverage_score REAL NOT NULL DEFAULT 0,
    x_available INTEGER NOT NULL DEFAULT 0,
    provider_counts_json TEXT NOT NULL DEFAULT '{}',
    flags_json TEXT NOT NULL DEFAULT '[]',
    raw_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (chain_id) REFERENCES chains(chain_id),
    UNIQUE (chain_id, token_address, observed_at, source)
);

CREATE INDEX IF NOT EXISTS idx_token_social_snapshots_token
    ON token_social_snapshots (chain_id, token_address, observed_at DESC);

CREATE TABLE IF NOT EXISTS signal_wallets (
    signal_id INTEGER NOT NULL,
    wallet_address TEXT NOT NULL,
    model_version TEXT NOT NULL,
    wallet_label TEXT NOT NULL,
    wallet_interest_score REAL NOT NULL,
    wallet_confidence_score REAL NOT NULL,
    buy_usd REAL,
    sell_usd REAL,
    net_buy_usd REAL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (signal_id, wallet_address, model_version),
    FOREIGN KEY (signal_id) REFERENCES signals(signal_id)
);

CREATE TABLE IF NOT EXISTS app_jobs (
    job_id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_type TEXT NOT NULL,
    status TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    progress REAL NOT NULL DEFAULT 0,
    message TEXT,
    error TEXT,
    result_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_app_jobs_requested_at
    ON app_jobs (requested_at DESC);

CREATE TABLE IF NOT EXISTS research_universe_snapshots (
    research_universe_snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    chain_id TEXT NOT NULL,
    token_address TEXT NOT NULL,
    token_symbol TEXT,
    snapshot_at TEXT NOT NULL,
    window_days INTEGER NOT NULL DEFAULT 7,
    first_trade_at TEXT,
    last_trade_at TEXT,
    pair_count INTEGER NOT NULL DEFAULT 0,
    dex_count INTEGER NOT NULL DEFAULT 0,
    trader_count INTEGER NOT NULL DEFAULT 0,
    buyer_count INTEGER NOT NULL DEFAULT 0,
    seller_count INTEGER NOT NULL DEFAULT 0,
    trade_count INTEGER NOT NULL DEFAULT 0,
    buy_trade_count INTEGER NOT NULL DEFAULT 0,
    sell_trade_count INTEGER NOT NULL DEFAULT 0,
    gross_buy_usd REAL NOT NULL DEFAULT 0,
    gross_sell_usd REAL NOT NULL DEFAULT 0,
    net_flow_usd REAL NOT NULL DEFAULT 0,
    volume_usd REAL NOT NULL DEFAULT 0,
    close_price_usd REAL,
    vwap_price_usd REAL,
    liquidity_usd REAL,
    market_cap_usd REAL,
    fdv_usd REAL,
    holder_count INTEGER,
    website_present INTEGER,
    contract_verified INTEGER,
    risk_score REAL,
    is_tradeable INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL,
    execution_id TEXT,
    quality_flags_json TEXT NOT NULL DEFAULT '[]',
    raw_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (chain_id) REFERENCES chains(chain_id),
    UNIQUE (chain_id, token_address, snapshot_at, window_days, source)
);

CREATE INDEX IF NOT EXISTS idx_research_universe_snapshot
    ON research_universe_snapshots (snapshot_at, chain_id, is_tradeable);

CREATE INDEX IF NOT EXISTS idx_research_universe_token
    ON research_universe_snapshots (chain_id, token_address, snapshot_at);

CREATE TABLE IF NOT EXISTS research_token_outcomes (
    research_token_outcome_id INTEGER PRIMARY KEY AUTOINCREMENT,
    chain_id TEXT NOT NULL,
    token_address TEXT NOT NULL,
    snapshot_at TEXT NOT NULL,
    methodology_version TEXT NOT NULL,
    listing_at TEXT,
    listed_within_30d INTEGER,
    listed_within_60d INTEGER,
    listed_within_90d INTEGER,
    return_7d REAL,
    return_30d REAL,
    return_60d REAL,
    return_90d REAL,
    max_favorable_excursion_30d REAL,
    max_drawdown_30d REAL,
    max_favorable_excursion_90d REAL,
    max_drawdown_90d REAL,
    activity_collapse_30d INTEGER,
    liquidity_collapse_30d INTEGER,
    rug_proxy_30d INTEGER,
    labels_matured_through TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (chain_id) REFERENCES chains(chain_id),
    UNIQUE (chain_id, token_address, snapshot_at, methodology_version)
);

CREATE INDEX IF NOT EXISTS idx_research_outcomes_snapshot
    ON research_token_outcomes (snapshot_at, chain_id);

CREATE INDEX IF NOT EXISTS idx_research_outcomes_labels
    ON research_token_outcomes (
        listed_within_90d,
        rug_proxy_30d,
        snapshot_at
    );

CREATE TABLE IF NOT EXISTS research_event_coverage (
    research_event_coverage_id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_event_id INTEGER NOT NULL,
    chain_id TEXT NOT NULL,
    token_address TEXT NOT NULL,
    token_symbol TEXT NOT NULL,
    announced_at TEXT NOT NULL,
    pre_announcement_flow_observed INTEGER NOT NULL DEFAULT 0,
    universe_snapshot_observed INTEGER NOT NULL DEFAULT 0,
    first_pre_announcement_trade_at TEXT,
    last_pre_announcement_trade_at TEXT,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL,
    FOREIGN KEY (listing_event_id) REFERENCES listing_events(listing_event_id),
    FOREIGN KEY (chain_id) REFERENCES chains(chain_id),
    UNIQUE (listing_event_id, chain_id, token_address)
);

CREATE INDEX IF NOT EXISTS idx_research_event_coverage_chain
    ON research_event_coverage (chain_id, announced_at);

CREATE TABLE IF NOT EXISTS wallet_token_opportunities (
    wallet_token_opportunity_id INTEGER PRIMARY KEY AUTOINCREMENT,
    chain_id TEXT NOT NULL,
    wallet_address TEXT NOT NULL,
    token_address TEXT NOT NULL,
    token_symbol TEXT,
    first_buy_at TEXT NOT NULL,
    last_buy_at TEXT NOT NULL,
    first_sell_at TEXT,
    last_sell_at TEXT,
    buy_trade_count INTEGER NOT NULL DEFAULT 0,
    sell_trade_count INTEGER NOT NULL DEFAULT 0,
    active_day_count INTEGER NOT NULL DEFAULT 0,
    gross_buy_usd REAL NOT NULL DEFAULT 0,
    gross_sell_usd REAL NOT NULL DEFAULT 0,
    net_cash_flow_usd REAL NOT NULL DEFAULT 0,
    token_bought_amount REAL,
    token_sold_amount REAL,
    average_buy_price_usd REAL,
    average_sell_price_usd REAL,
    wallet_observed_buy_usd REAL,
    position_to_observed_flow REAL,
    turnover_ratio REAL,
    listing_at TEXT,
    listed_within_30d INTEGER,
    listed_within_60d INTEGER,
    listed_within_90d INTEGER,
    outcome_matured INTEGER NOT NULL DEFAULT 0,
    mark_price_usd REAL,
    estimated_pnl_usd REAL,
    estimated_return REAL,
    max_favorable_excursion_90d REAL,
    max_drawdown_90d REAL,
    holding_days REAL,
    exit_quality_score REAL,
    source TEXT NOT NULL,
    execution_id TEXT,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (chain_id) REFERENCES chains(chain_id),
    UNIQUE (chain_id, wallet_address, token_address, first_buy_at, source)
);

CREATE INDEX IF NOT EXISTS idx_wallet_opportunities_wallet
    ON wallet_token_opportunities (
        chain_id,
        wallet_address,
        outcome_matured,
        listed_within_90d
    );

CREATE INDEX IF NOT EXISTS idx_wallet_opportunities_token
    ON wallet_token_opportunities (chain_id, token_address, first_buy_at);

CREATE TABLE IF NOT EXISTS wallet_token_weekly_flows (
    wallet_token_weekly_flow_id INTEGER PRIMARY KEY AUTOINCREMENT,
    chain_id TEXT NOT NULL,
    wallet_address TEXT NOT NULL,
    token_address TEXT NOT NULL,
    token_symbol TEXT,
    snapshot_at TEXT NOT NULL,
    gross_buy_usd REAL NOT NULL DEFAULT 0,
    gross_sell_usd REAL NOT NULL DEFAULT 0,
    net_buy_usd REAL NOT NULL DEFAULT 0,
    buy_trade_count INTEGER NOT NULL DEFAULT 0,
    sell_trade_count INTEGER NOT NULL DEFAULT 0,
    first_trade_at TEXT,
    last_trade_at TEXT,
    source TEXT NOT NULL,
    execution_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (chain_id) REFERENCES chains(chain_id),
    UNIQUE (
        chain_id,
        wallet_address,
        token_address,
        snapshot_at,
        source
    )
);

CREATE INDEX IF NOT EXISTS idx_wallet_weekly_flows_snapshot
    ON wallet_token_weekly_flows (
        chain_id,
        snapshot_at,
        token_address,
        wallet_address
    );

CREATE TABLE IF NOT EXISTS wallet_research_scores (
    wallet_research_score_id INTEGER PRIMARY KEY AUTOINCREMENT,
    chain_id TEXT NOT NULL,
    wallet_address TEXT NOT NULL,
    model_version TEXT NOT NULL,
    opportunity_count INTEGER NOT NULL,
    matured_opportunity_count INTEGER NOT NULL,
    hit_count_30d INTEGER NOT NULL,
    hit_count_60d INTEGER NOT NULL,
    hit_count_90d INTEGER NOT NULL,
    miss_count_90d INTEGER NOT NULL,
    baseline_rate REAL NOT NULL,
    posterior_hit_rate REAL NOT NULL,
    posterior_lower_95 REAL NOT NULL,
    posterior_upper_95 REAL NOT NULL,
    posterior_lift REAL,
    estimated_pnl_usd REAL,
    median_return REAL,
    win_rate REAL,
    median_max_drawdown REAL,
    median_turnover REAL,
    median_position_to_flow REAL,
    median_exit_quality REAL,
    research_score REAL NOT NULL,
    confidence_score REAL NOT NULL,
    label TEXT NOT NULL,
    rationale_json TEXT NOT NULL DEFAULT '[]',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (chain_id) REFERENCES chains(chain_id),
    UNIQUE (chain_id, wallet_address, model_version)
);

CREATE INDEX IF NOT EXISTS idx_wallet_research_score
    ON wallet_research_scores (
        model_version,
        label,
        research_score DESC
    );

CREATE TABLE IF NOT EXISTS wallet_identity_edges (
    wallet_identity_edge_id INTEGER PRIMARY KEY AUTOINCREMENT,
    chain_id TEXT NOT NULL,
    wallet_address_a TEXT NOT NULL,
    wallet_address_b TEXT NOT NULL,
    edge_type TEXT NOT NULL,
    hop_count INTEGER NOT NULL DEFAULT 1,
    confidence REAL NOT NULL,
    first_observed_at TEXT,
    last_observed_at TEXT,
    source TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (chain_id) REFERENCES chains(chain_id),
    UNIQUE (
        chain_id,
        wallet_address_a,
        wallet_address_b,
        edge_type,
        source
    )
);

CREATE INDEX IF NOT EXISTS idx_wallet_identity_edges_wallet_a
    ON wallet_identity_edges (chain_id, wallet_address_a, confidence DESC);

CREATE INDEX IF NOT EXISTS idx_wallet_identity_edges_wallet_b
    ON wallet_identity_edges (chain_id, wallet_address_b, confidence DESC);

CREATE TABLE IF NOT EXISTS wallet_identity_coverage (
    chain_id TEXT NOT NULL,
    wallet_address TEXT NOT NULL,
    coverage_type TEXT NOT NULL,
    status TEXT NOT NULL,
    source TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    checked_at TEXT NOT NULL,
    FOREIGN KEY (chain_id) REFERENCES chains(chain_id),
    UNIQUE (chain_id, wallet_address, coverage_type, source)
);

CREATE INDEX IF NOT EXISTS idx_wallet_identity_coverage_wallet
    ON wallet_identity_coverage (
        chain_id,
        wallet_address,
        coverage_type,
        status
    );

CREATE TABLE IF NOT EXISTS token_attention_snapshots (
    token_attention_snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    chain_id TEXT NOT NULL,
    token_address TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    source TEXT NOT NULL,
    dexscreener_boosts INTEGER,
    dexscreener_ads INTEGER,
    dexscreener_profile INTEGER,
    farcaster_mentions_24h INTEGER,
    farcaster_mentions_7d INTEGER,
    github_commits_30d INTEGER,
    github_contributors_90d INTEGER,
    news_mentions_24h INTEGER,
    news_mentions_7d INTEGER,
    onchain_trader_growth_7d REAL,
    onchain_volume_growth_7d REAL,
    public_attention_score REAL,
    onchain_attention_score REAL,
    attention_gap_score REAL,
    coverage_score REAL NOT NULL DEFAULT 0,
    flags_json TEXT NOT NULL DEFAULT '[]',
    raw_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY (chain_id) REFERENCES chains(chain_id),
    UNIQUE (chain_id, token_address, observed_at, source)
);

CREATE INDEX IF NOT EXISTS idx_token_attention_snapshot
    ON token_attention_snapshots (chain_id, token_address, observed_at DESC);

CREATE TABLE IF NOT EXISTS research_model_runs (
    research_model_run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_name TEXT NOT NULL,
    model_version TEXT NOT NULL,
    model_family TEXT NOT NULL,
    target_label TEXT NOT NULL,
    status TEXT NOT NULL,
    train_start_at TEXT,
    train_end_at TEXT,
    validation_start_at TEXT,
    validation_end_at TEXT,
    feature_names_json TEXT NOT NULL DEFAULT '[]',
    config_json TEXT NOT NULL DEFAULT '{}',
    metrics_json TEXT NOT NULL DEFAULT '{}',
    feature_importance_json TEXT NOT NULL DEFAULT '{}',
    leakage_checks_json TEXT NOT NULL DEFAULT '{}',
    artifact_path TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    notes TEXT,
    UNIQUE (model_name, model_version, started_at)
);

CREATE INDEX IF NOT EXISTS idx_research_model_runs_latest
    ON research_model_runs (started_at DESC);

CREATE TABLE IF NOT EXISTS research_model_predictions (
    research_model_prediction_id INTEGER PRIMARY KEY AUTOINCREMENT,
    research_model_run_id INTEGER NOT NULL,
    chain_id TEXT NOT NULL,
    token_address TEXT NOT NULL,
    snapshot_at TEXT NOT NULL,
    target_label TEXT NOT NULL,
    actual_label INTEGER,
    probability REAL NOT NULL,
    rank_at_snapshot INTEGER,
    split TEXT NOT NULL,
    feature_values_json TEXT NOT NULL DEFAULT '{}',
    explanation_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (research_model_run_id)
        REFERENCES research_model_runs(research_model_run_id),
    FOREIGN KEY (chain_id) REFERENCES chains(chain_id),
    UNIQUE (
        research_model_run_id,
        chain_id,
        token_address,
        snapshot_at,
        target_label
    )
);

CREATE INDEX IF NOT EXISTS idx_research_model_predictions_rank
    ON research_model_predictions (
        research_model_run_id,
        snapshot_at,
        rank_at_snapshot
    );

CREATE TABLE IF NOT EXISTS funding_scans (
    funding_scan_id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    instrument_count INTEGER NOT NULL DEFAULT 0,
    market_snapshot_count INTEGER NOT NULL DEFAULT 0,
    orderbook_count INTEGER NOT NULL DEFAULT 0,
    history_row_count INTEGER NOT NULL DEFAULT 0,
    route_count INTEGER NOT NULL DEFAULT 0,
    paper_candidate_count INTEGER NOT NULL DEFAULT 0,
    paper_execution_count INTEGER NOT NULL DEFAULT 0,
    config_json TEXT NOT NULL DEFAULT '{}',
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_funding_scans_started
    ON funding_scans (started_at DESC);

CREATE TABLE IF NOT EXISTS funding_scan_warnings (
    funding_scan_warning_id INTEGER PRIMARY KEY AUTOINCREMENT,
    funding_scan_id INTEGER NOT NULL,
    warning TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (funding_scan_id) REFERENCES funding_scans(funding_scan_id)
);

CREATE INDEX IF NOT EXISTS idx_funding_scan_warnings_scan
    ON funding_scan_warnings (funding_scan_id, funding_scan_warning_id);

CREATE TABLE IF NOT EXISTS funding_instruments (
    venue TEXT NOT NULL,
    symbol TEXT NOT NULL,
    canonical_asset TEXT NOT NULL,
    base_asset TEXT NOT NULL,
    quote_asset TEXT NOT NULL,
    collateral_asset TEXT NOT NULL,
    contract_type TEXT NOT NULL,
    contract_multiplier REAL NOT NULL DEFAULT 1,
    status TEXT NOT NULL,
    source_url TEXT,
    observed_at TEXT NOT NULL,
    raw_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (venue, symbol)
);

CREATE INDEX IF NOT EXISTS idx_funding_instruments_asset
    ON funding_instruments (canonical_asset, venue, status);

CREATE TABLE IF NOT EXISTS funding_market_snapshots (
    funding_market_snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    funding_scan_id INTEGER NOT NULL,
    venue TEXT NOT NULL,
    symbol TEXT NOT NULL,
    canonical_asset TEXT NOT NULL,
    funding_rate REAL NOT NULL,
    funding_interval_hours REAL NOT NULL,
    hourly_funding_rate REAL NOT NULL,
    funding_rate_kind TEXT NOT NULL,
    next_funding_at TEXT,
    mark_price REAL,
    index_price REAL,
    open_interest_usd REAL,
    volume_24h_usd REAL,
    observed_at TEXT NOT NULL,
    raw_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (funding_scan_id) REFERENCES funding_scans(funding_scan_id),
    FOREIGN KEY (venue, symbol) REFERENCES funding_instruments(venue, symbol),
    UNIQUE (funding_scan_id, venue, symbol)
);

CREATE INDEX IF NOT EXISTS idx_funding_market_asset
    ON funding_market_snapshots (
        funding_scan_id,
        canonical_asset,
        hourly_funding_rate DESC
    );

CREATE TABLE IF NOT EXISTS funding_orderbook_snapshots (
    funding_orderbook_snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    funding_scan_id INTEGER NOT NULL,
    venue TEXT NOT NULL,
    symbol TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    bids_json TEXT NOT NULL DEFAULT '[]',
    asks_json TEXT NOT NULL DEFAULT '[]',
    best_bid REAL,
    best_ask REAL,
    mid_price REAL,
    bid_depth_usd REAL NOT NULL DEFAULT 0,
    ask_depth_usd REAL NOT NULL DEFAULT 0,
    raw_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (funding_scan_id) REFERENCES funding_scans(funding_scan_id),
    FOREIGN KEY (venue, symbol) REFERENCES funding_instruments(venue, symbol),
    UNIQUE (funding_scan_id, venue, symbol)
);

CREATE INDEX IF NOT EXISTS idx_funding_books_market
    ON funding_orderbook_snapshots (venue, symbol, observed_at DESC);

CREATE TABLE IF NOT EXISTS funding_route_universe (
    funding_route_universe_id INTEGER PRIMARY KEY AUTOINCREMENT,
    funding_scan_id INTEGER NOT NULL,
    canonical_asset TEXT NOT NULL,
    long_venue TEXT NOT NULL,
    long_symbol TEXT NOT NULL,
    short_venue TEXT NOT NULL,
    short_symbol TEXT NOT NULL,
    current_hourly_spread REAL NOT NULL,
    quick_gross_rate REAL NOT NULL,
    quick_taker_cost_rate REAL NOT NULL,
    quick_maker_cost_rate REAL NOT NULL,
    quick_best_case_net_rate REAL NOT NULL,
    quick_schedule_ready INTEGER NOT NULL DEFAULT 0,
    execution_eligible INTEGER NOT NULL DEFAULT 0,
    execution_screen_reason TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    FOREIGN KEY (funding_scan_id) REFERENCES funding_scans(funding_scan_id),
    UNIQUE (
        funding_scan_id, canonical_asset,
        long_venue, long_symbol, short_venue, short_symbol
    )
);

CREATE INDEX IF NOT EXISTS idx_funding_route_universe_scan
    ON funding_route_universe (
        funding_scan_id, execution_eligible, quick_best_case_net_rate DESC
    );

CREATE TABLE IF NOT EXISTS funding_rate_history (
    venue TEXT NOT NULL,
    symbol TEXT NOT NULL,
    funding_at TEXT NOT NULL,
    funding_rate REAL NOT NULL,
    funding_interval_hours REAL NOT NULL,
    hourly_funding_rate REAL NOT NULL,
    mark_price REAL,
    observed_at TEXT NOT NULL,
    raw_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (venue, symbol, funding_at),
    FOREIGN KEY (venue, symbol) REFERENCES funding_instruments(venue, symbol)
);

CREATE INDEX IF NOT EXISTS idx_funding_history_market
    ON funding_rate_history (venue, symbol, funding_at DESC);

CREATE TABLE IF NOT EXISTS funding_history_sync_state (
    venue TEXT NOT NULL,
    symbol TEXT NOT NULL,
    requested_start_at TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    row_count INTEGER NOT NULL DEFAULT 0,
    earliest_funding_at TEXT,
    latest_funding_at TEXT,
    PRIMARY KEY (venue, symbol),
    FOREIGN KEY (venue, symbol) REFERENCES funding_instruments(venue, symbol)
);

CREATE INDEX IF NOT EXISTS idx_funding_history_sync_fetched
    ON funding_history_sync_state (fetched_at DESC, requested_start_at);

CREATE TABLE IF NOT EXISTS funding_routes (
    funding_route_id INTEGER PRIMARY KEY AUTOINCREMENT,
    funding_scan_id INTEGER NOT NULL,
    route_key TEXT NOT NULL,
    route_type TEXT NOT NULL,
    canonical_asset TEXT NOT NULL,
    venue_scope TEXT NOT NULL,
    long_venue TEXT NOT NULL,
    long_symbol TEXT NOT NULL,
    short_venue TEXT NOT NULL,
    short_symbol TEXT NOT NULL,
    status TEXT NOT NULL,
    confidence_score REAL NOT NULL DEFAULT 0,
    target_notional REAL NOT NULL DEFAULT 0,
    market_capacity REAL NOT NULL DEFAULT 0,
    capital_required REAL NOT NULL DEFAULT 0,
    horizon_days REAL NOT NULL,
    current_hourly_spread REAL NOT NULL DEFAULT 0,
    current_gross_apr REAL NOT NULL DEFAULT 0,
    projected_hourly_spread REAL NOT NULL DEFAULT 0,
    projected_gross_apr REAL NOT NULL DEFAULT 0,
    historical_median_hourly_spread REAL NOT NULL DEFAULT 0,
    positive_spread_fraction REAL NOT NULL DEFAULT 0,
    persistence_score REAL NOT NULL DEFAULT 0,
    history_point_count INTEGER NOT NULL DEFAULT 0,
    expected_gross_funding REAL NOT NULL DEFAULT 0,
    expected_net_profit REAL NOT NULL DEFAULT 0,
    net_roc_annualized REAL NOT NULL DEFAULT 0,
    total_fees REAL NOT NULL DEFAULT 0,
    slippage_cost REAL NOT NULL DEFAULT 0,
    basis_gap REAL NOT NULL DEFAULT 0,
    basis_reserve REAL NOT NULL DEFAULT 0,
    operations_buffer REAL NOT NULL DEFAULT 0,
    long_next_funding_at TEXT,
    short_next_funding_at TEXT,
    observed_at TEXT NOT NULL,
    legs_json TEXT NOT NULL DEFAULT '[]',
    rationale_json TEXT NOT NULL DEFAULT '[]',
    risk_flags_json TEXT NOT NULL DEFAULT '[]',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (funding_scan_id) REFERENCES funding_scans(funding_scan_id),
    UNIQUE (funding_scan_id, route_key)
);

CREATE INDEX IF NOT EXISTS idx_funding_routes_decision
    ON funding_routes (
        funding_scan_id,
        status,
        net_roc_annualized DESC,
        expected_net_profit DESC
    );

CREATE TABLE IF NOT EXISTS funding_paper_executions (
    funding_paper_execution_id INTEGER PRIMARY KEY AUTOINCREMENT,
    funding_route_id INTEGER NOT NULL,
    funding_scan_id INTEGER NOT NULL,
    model_version TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    requested_notional REAL NOT NULL,
    filled_notional REAL NOT NULL DEFAULT 0,
    fill_ratio REAL NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL,
    expected_net_profit REAL,
    repriced_net_profit REAL,
    result_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (funding_route_id) REFERENCES funding_routes(funding_route_id),
    FOREIGN KEY (funding_scan_id) REFERENCES funding_scans(funding_scan_id),
    UNIQUE (funding_route_id, funding_scan_id, model_version)
);

CREATE INDEX IF NOT EXISTS idx_funding_paper_created
    ON funding_paper_executions (created_at DESC);

CREATE TABLE IF NOT EXISTS funding_paper_accounts (
    venue TEXT PRIMARY KEY,
    starting_balance REAL NOT NULL,
    cash_balance REAL NOT NULL,
    reserved_margin REAL NOT NULL DEFAULT 0,
    realized_pnl REAL NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS funding_paper_positions (
    funding_paper_position_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_key TEXT NOT NULL UNIQUE,
    route_key TEXT NOT NULL,
    status TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    open_funding_scan_id INTEGER,
    open_funding_route_id INTEGER,
    close_funding_scan_id INTEGER,
    close_funding_route_id INTEGER,
    canonical_asset TEXT NOT NULL,
    long_venue TEXT NOT NULL,
    long_symbol TEXT NOT NULL,
    short_venue TEXT NOT NULL,
    short_symbol TEXT NOT NULL,
    base_quantity REAL NOT NULL DEFAULT 0,
    target_notional REAL NOT NULL DEFAULT 0,
    long_notional REAL NOT NULL DEFAULT 0,
    short_notional REAL NOT NULL DEFAULT 0,
    long_reserved_margin REAL NOT NULL DEFAULT 0,
    short_reserved_margin REAL NOT NULL DEFAULT 0,
    long_settlement_at TEXT,
    short_settlement_at TEXT,
    max_settlement_at TEXT,
    expected_live_gross REAL NOT NULL DEFAULT 0,
    expected_live_net REAL NOT NULL DEFAULT 0,
    expected_execution_cost REAL NOT NULL DEFAULT 0,
    entry_cross_spread REAL,
    entry_basis_bps REAL,
    actual_funding_pnl REAL,
    actual_basis_pnl REAL,
    actual_execution_cost REAL,
    actual_net_pnl REAL,
    close_reason TEXT,
    entry_legs_json TEXT NOT NULL DEFAULT '[]',
    entry_evidence_json TEXT NOT NULL DEFAULT '{}',
    close_legs_json TEXT NOT NULL DEFAULT '[]',
    close_evidence_json TEXT NOT NULL DEFAULT '{}',
    settlement_json TEXT NOT NULL DEFAULT '{}',
    notes_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (open_funding_scan_id) REFERENCES funding_scans(funding_scan_id),
    FOREIGN KEY (open_funding_route_id) REFERENCES funding_routes(funding_route_id),
    FOREIGN KEY (close_funding_scan_id) REFERENCES funding_scans(funding_scan_id),
    FOREIGN KEY (close_funding_route_id) REFERENCES funding_routes(funding_route_id)
);

CREATE INDEX IF NOT EXISTS idx_funding_paper_positions_status
    ON funding_paper_positions (status, max_settlement_at, opened_at);

CREATE INDEX IF NOT EXISTS idx_funding_paper_positions_route
    ON funding_paper_positions (route_key, status, opened_at DESC);

CREATE TABLE IF NOT EXISTS funding_paper_events (
    funding_paper_event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    event_type TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'info',
    funding_paper_position_id INTEGER,
    funding_scan_id INTEGER,
    funding_route_id INTEGER,
    route_key TEXT,
    message TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    telegram_status TEXT NOT NULL DEFAULT 'not_configured',
    telegram_error TEXT,
    FOREIGN KEY (funding_paper_position_id)
        REFERENCES funding_paper_positions(funding_paper_position_id),
    FOREIGN KEY (funding_scan_id) REFERENCES funding_scans(funding_scan_id),
    FOREIGN KEY (funding_route_id) REFERENCES funding_routes(funding_route_id)
);

CREATE INDEX IF NOT EXISTS idx_funding_paper_events_created
    ON funding_paper_events (created_at DESC);

CREATE TABLE IF NOT EXISTS funding_paper_balance_ledger (
    funding_paper_balance_ledger_id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    venue TEXT NOT NULL,
    funding_paper_position_id INTEGER,
    event_type TEXT NOT NULL,
    cash_delta REAL NOT NULL DEFAULT 0,
    reserved_delta REAL NOT NULL DEFAULT 0,
    cash_balance_after REAL NOT NULL,
    reserved_margin_after REAL NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (venue) REFERENCES funding_paper_accounts(venue),
    FOREIGN KEY (funding_paper_position_id)
        REFERENCES funding_paper_positions(funding_paper_position_id)
);

CREATE INDEX IF NOT EXISTS idx_funding_paper_balance_ledger_created
    ON funding_paper_balance_ledger (created_at DESC, venue);

CREATE TABLE IF NOT EXISTS funding_paper_equity_snapshots (
    funding_paper_equity_snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_at TEXT NOT NULL,
    total_cash REAL NOT NULL,
    total_reserved_margin REAL NOT NULL,
    total_equity REAL NOT NULL,
    realized_pnl REAL NOT NULL,
    open_position_count INTEGER NOT NULL,
    closed_position_count INTEGER NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_funding_paper_equity_snapshots_time
    ON funding_paper_equity_snapshots (observed_at DESC);

CREATE TABLE IF NOT EXISTS signal_shadow_marks (
    signal_shadow_mark_id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER NOT NULL,
    chain_id TEXT NOT NULL,
    token_address TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    horizon_hours REAL NOT NULL,
    entry_price_usd REAL,
    mark_price_usd REAL,
    entry_liquidity_usd REAL,
    mark_liquidity_usd REAL,
    gross_return REAL,
    estimated_round_trip_slippage_bps REAL,
    net_return_after_slippage REAL,
    source TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY (signal_id) REFERENCES signals(signal_id),
    FOREIGN KEY (chain_id) REFERENCES chains(chain_id),
    UNIQUE (signal_id, observed_at, source)
);

CREATE INDEX IF NOT EXISTS idx_signal_shadow_marks_signal
    ON signal_shadow_marks (signal_id, observed_at);
