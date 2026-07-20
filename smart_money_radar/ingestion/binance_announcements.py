from __future__ import annotations

import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

from smart_money_radar.storage import utc_now_iso


BINANCE_ANNOUNCEMENT_CATALOG_ID = 48
BINANCE_LIST_ENDPOINT = (
    "https://www.binance.com/bapi/composite/v1/public/cms/article/list/query"
)
BINANCE_DETAIL_ENDPOINT = (
    "https://www.binance.com/bapi/composite/v1/public/cms/article/detail/query"
)
BINANCE_ANNOUNCEMENT_URL = "https://www.binance.com/en/support/announcement/{code}"

QUOTE_ASSETS = {
    "AUD",
    "BNB",
    "BRL",
    "BTC",
    "BUSD",
    "DAI",
    "ETH",
    "EUR",
    "FDUSD",
    "TRY",
    "TUSD",
    "USD",
    "USDC",
    "USDP",
    "USDT",
}

STOP_SYMBOLS = {
    "ADGM",
    "API",
    "CEX",
    "DEX",
    "ETF",
    "VIP",
    "UTC",
}

PAIR_RE = re.compile(
    r"\b([A-Z0-9]{2,20})/(USDT|USDC|FDUSD|BTC|BNB|ETH|TRY|EUR|BRL|TUSD)\b"
)
PARENS_RE = re.compile(r"\(([A-Z0-9,\s/&.-]{2,120})\)")
EVM_ADDRESS_RE = re.compile(r"0x[a-fA-F0-9]{40}")
SOLANA_ADDRESS_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")

EXPLORER_HOST_TO_CHAIN = {
    "etherscan.io": "ethereum",
    "basescan.org": "base",
    "bscscan.com": "bsc",
    "arbiscan.io": "arbitrum",
    "optimistic.etherscan.io": "optimism",
    "polygonscan.com": "polygon",
    "snowtrace.io": "avalanche",
    "solscan.io": "solana",
    "solana.fm": "solana",
}


class BinanceAnnouncementError(RuntimeError):
    pass


class BinanceAnnouncementClient:
    def __init__(
        self,
        timeout_seconds: int = 30,
        min_delay_seconds: float = 0.5,
        max_retries: int = 4,
        retry_backoff_seconds: float = 3.0,
    ):
        self.timeout_seconds = timeout_seconds
        self.min_delay_seconds = min_delay_seconds
        self.max_retries = max_retries
        self.retry_backoff_seconds = retry_backoff_seconds
        self._last_request_at = 0.0

    def fetch_page(
        self,
        page_no: int,
        page_size: int,
        catalog_id: int = BINANCE_ANNOUNCEMENT_CATALOG_ID,
    ) -> dict[str, Any]:
        params = {
            "type": 1,
            "pageNo": page_no,
            "pageSize": page_size,
            "catalogId": catalog_id,
        }
        return self._get_json(BINANCE_LIST_ENDPOINT, params)

    def fetch_detail(self, article_code: str) -> dict[str, Any]:
        return self._get_json(BINANCE_DETAIL_ENDPOINT, {"articleCode": article_code})

    def _get_json(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        request_url = f"{url}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(
            request_url,
            headers={
                "Accept": "application/json",
                "User-Agent": "SmartMoneyRadar/0.1",
            },
        )

        for attempt in range(self.max_retries + 1):
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < self.min_delay_seconds:
                time.sleep(self.min_delay_seconds - elapsed)

            try:
                with urllib.request.urlopen(
                    request, timeout=self.timeout_seconds
                ) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as exc:
                self._last_request_at = time.monotonic()
                if exc.code == 429 and attempt < self.max_retries:
                    sleep_for = self.retry_backoff_seconds * (attempt + 1)
                    time.sleep(sleep_for)
                    continue
                raise BinanceAnnouncementError(
                    f"Binance request failed for {request_url}: {exc}"
                ) from exc
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                self._last_request_at = time.monotonic()
                if attempt < self.max_retries:
                    sleep_for = self.retry_backoff_seconds * (attempt + 1)
                    time.sleep(sleep_for)
                    continue
                raise BinanceAnnouncementError(
                    f"Binance request failed for {request_url}: {exc}"
                ) from exc
        else:
            raise BinanceAnnouncementError(f"Binance request failed for {request_url}")

        self._last_request_at = time.monotonic()

        if not payload.get("success") or payload.get("code") != "000000":
            raise BinanceAnnouncementError(
                f"Binance returned non-success payload for {request_url}: {payload}"
            )
        return payload


class BinanceAnnouncementCollector:
    def __init__(self, client: BinanceAnnouncementClient | None = None):
        self.client = client or BinanceAnnouncementClient()
        self.detail_errors: list[dict[str, str]] = []

    def collect(
        self,
        start_page: int,
        pages: int,
        page_size: int,
        with_details: bool,
        listing_details_only: bool = False,
    ) -> Iterator[dict[str, Any]]:
        for page_no in range(start_page, start_page + pages):
            page_payload = self.client.fetch_page(page_no=page_no, page_size=page_size)
            catalogs = page_payload.get("data", {}).get("catalogs", [])
            for catalog in catalogs:
                catalog_id = int(catalog.get("catalogId") or 0)
                for article in catalog.get("articles", []):
                    detail_payload = None
                    if with_details and (
                        not listing_details_only
                        or should_fetch_listing_detail(str(article.get("title") or ""))
                    ):
                        try:
                            detail_payload = self.client.fetch_detail(article["code"])
                        except BinanceAnnouncementError as exc:
                            self.detail_errors.append(
                                {
                                    "article_code": str(article["code"]),
                                    "error": str(exc),
                                }
                            )
                    yield build_announcement(
                        article=article,
                        catalog_id=catalog_id,
                        list_payload=article,
                        detail_payload=detail_payload,
                    )


def build_announcement(
    article: dict[str, Any],
    catalog_id: int,
    list_payload: dict[str, Any],
    detail_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    release_ts_ms = int(article["releaseDate"])
    release_at = datetime.fromtimestamp(release_ts_ms / 1000, tz=UTC).isoformat()
    article_code = str(article["code"])
    source_url = BINANCE_ANNOUNCEMENT_URL.format(code=article_code)
    body_text, body_links = extract_body(detail_payload)
    title = str(article["title"]).strip()
    normalized = normalize_announcement(title, body_text, body_links)

    return {
        "raw": {
            "article_id": int(article["id"]),
            "article_code": article_code,
            "catalog_id": catalog_id,
            "title": title,
            "release_ts_ms": release_ts_ms,
            "release_at": release_at,
            "source_url": source_url,
            "fetched_at": utc_now_iso(),
            "list_payload": list_payload,
            "detail_payload": detail_payload,
            "body_text": body_text,
            "body_links": body_links,
        },
        "normalized": normalized,
    }


def extract_body(
    detail_payload: dict[str, Any] | None,
) -> tuple[str | None, list[dict[str, str]]]:
    if not detail_payload:
        return None, []

    data = detail_payload.get("data") or {}
    body = data.get("body")
    if not body:
        return None, []

    try:
        body_json = json.loads(body)
    except (TypeError, json.JSONDecodeError):
        return html.unescape(str(body)), []

    text_parts: list[str] = []
    links: list[dict[str, str]] = []
    walk_rich_text(body_json, text_parts, links)
    body_text = " ".join(" ".join(text_parts).split())
    return body_text, dedupe_links(links)


def walk_rich_text(
    value: Any,
    text_parts: list[str],
    links: list[dict[str, str]],
) -> None:
    if isinstance(value, dict):
        node_type = value.get("node")
        rich_id = value.get("id")
        config = value.get("config") if isinstance(value.get("config"), dict) else {}

        if node_type == "text":
            text = value.get("text")
            if text:
                text_parts.append(html.unescape(str(text)))

        if rich_id == "RichTextText":
            content = config.get("content")
            if content:
                text_parts.append(html.unescape(str(content)))

        link = extract_link(value, config)
        if link:
            links.append(link)

        for child in value.values():
            walk_rich_text(child, text_parts, links)
    elif isinstance(value, list):
        for item in value:
            walk_rich_text(item, text_parts, links)


def extract_link(value: dict[str, Any], config: dict[str, Any]) -> dict[str, str] | None:
    attrs = value.get("attr") if isinstance(value.get("attr"), dict) else {}
    href = attrs.get("href") or config.get("href")
    if not href or not isinstance(href, str):
        return None

    content = config.get("content")
    if not content:
        content = collect_plain_text(value)
    return {
        "href": href,
        "text": html.unescape(str(content or "")).strip(),
    }


def collect_plain_text(value: Any) -> str:
    text_parts: list[str] = []
    collect_plain_text_parts(value, text_parts)
    return " ".join(" ".join(text_parts).split())


def collect_plain_text_parts(value: Any, text_parts: list[str]) -> None:
    if isinstance(value, dict):
        if value.get("node") == "text" and value.get("text"):
            text_parts.append(html.unescape(str(value["text"])))

        config = value.get("config") if isinstance(value.get("config"), dict) else {}
        if value.get("id") == "RichTextText" and config.get("content"):
            text_parts.append(html.unescape(str(config["content"])))

        for child in value.values():
            collect_plain_text_parts(child, text_parts)
    elif isinstance(value, list):
        for item in value:
            collect_plain_text_parts(item, text_parts)


def dedupe_links(links: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    deduped: list[dict[str, str]] = []
    for link in links:
        href = link["href"]
        if href in seen:
            continue
        seen.add(href)
        deduped.append(link)
    return deduped


def normalize_announcement(
    title: str,
    body_text: str | None,
    body_links: list[dict[str, str]],
) -> dict[str, Any]:
    search_text = f"{title} {body_text or ''}"
    title_lower = title.lower()
    lower = search_text.lower()
    is_tokenized_stock = "bstock" in title_lower or "stock trading" in title_lower
    is_futures = (
        "futures will launch" in title_lower
        or "usdⓢ-margined" in title_lower
        or "perpetual contract" in title_lower
    )
    is_alpha_or_airdrop = any(
        phrase in title_lower
        for phrase in (
            "hodler airdrops",
            "launchpool",
            "megadrop",
            "binance alpha",
        )
    )
    is_distribution_listing = is_alpha_or_airdrop and any(
        phrase in title_lower
        for phrase in ("hodler airdrops", "launchpool", "megadrop")
    )
    is_margin = "margin will add" in title_lower or title_lower.startswith("binance margin")
    is_product_bundle = (
        "binance will add" in title_lower
        and any(
            phrase in title_lower
            for phrase in ("earn", "buy crypto", "convert", "vip loan", "margin")
        )
    )
    is_spot = (
        not is_tokenized_stock
        and not is_futures
        and not is_margin
        and not is_product_bundle
        and (
            "spot" in title_lower
            or "will list" in title_lower
            or "binance lists" in title_lower
        )
    )

    if is_tokenized_stock:
        category = "tokenized_stock"
    elif is_futures:
        category = "futures"
    elif is_alpha_or_airdrop:
        category = "alpha_or_airdrop"
    elif is_product_bundle:
        category = "product_bundle"
    elif is_margin:
        category = "margin"
    elif is_spot:
        category = "spot"
    elif "earn" in title_lower:
        category = "earn"
    else:
        category = "unknown"

    trading_pairs = extract_trading_pairs(search_text)
    extracted_symbols = extract_symbols(title, trading_pairs)
    contract_links = extract_contract_links(body_links)

    return {
        "category": category,
        "is_spot_listing": category == "spot" or is_distribution_listing,
        "is_futures_listing": category == "futures",
        "is_alpha_or_airdrop": category == "alpha_or_airdrop",
        "requires_manual_review": True,
        "extracted_symbols": extracted_symbols,
        "trading_pairs": trading_pairs,
        "contract_links": contract_links,
    }


def extract_trading_pairs(text: str) -> list[str]:
    pairs = {f"{base}/{quote}" for base, quote in PAIR_RE.findall(text)}
    return sorted(pairs)


def should_fetch_listing_detail(title: str) -> bool:
    category = normalize_announcement(title, None, [])["category"]
    return category in {"spot", "alpha_or_airdrop"}


def extract_symbols(text: str, trading_pairs: list[str]) -> list[str]:
    symbols: set[str] = {pair.split("/")[0] for pair in trading_pairs}
    for match in PARENS_RE.findall(text):
        for token in re.split(r"[,/&]|\s+and\s+", match, flags=re.IGNORECASE):
            symbol = token.strip(" .-")
            if (
                symbol.isupper()
                and 2 <= len(symbol) <= 20
                and symbol not in QUOTE_ASSETS
                and symbol not in STOP_SYMBOLS
            ):
                symbols.add(symbol)
    return sorted(symbols)


def extract_contract_links(links: list[dict[str, str]]) -> list[dict[str, str]]:
    contracts: list[dict[str, str]] = []
    for link in links:
        href = link["href"]
        parsed = urllib.parse.urlparse(href)
        host = parsed.netloc.lower().removeprefix("www.")
        chain_id = EXPLORER_HOST_TO_CHAIN.get(host)
        if not chain_id:
            continue

        address = None
        evm_match = EVM_ADDRESS_RE.search(href)
        if evm_match:
            address = evm_match.group(0).lower()
        elif chain_id == "solana":
            sol_match = SOLANA_ADDRESS_RE.search(parsed.path)
            if sol_match:
                address = sol_match.group(0)

        contracts.append(
            {
                "chain_id": chain_id,
                "address": address or "",
                "href": href,
                "text": link.get("text", ""),
            }
        )

    unique: dict[tuple[str, str, str], dict[str, str]] = {}
    for contract in contracts:
        key = (contract["chain_id"], contract["address"], contract["href"])
        unique[key] = contract
    return list(unique.values())
