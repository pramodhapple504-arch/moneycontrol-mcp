"""Shared HTTP client, endpoint helpers, and parsing utilities for Moneycontrol.

Moneycontrol has no official public API. This module talks to the same public
endpoints the website/app use:

* ``autosuggestion_solr.php``  - search/autocomplete (resolves names -> sc_id / index code)
* ``priceapi.moneycontrol.com/pricefeed/...``  - live quotes, fundamentals, indices, pivots
* ``www.moneycontrol.com/markets/fii-dii-data/``  - FII/DII activity (embedded Next.js data)
* ``www.moneycontrol.com/rss/*.xml``  - news feeds

All functions raise :class:`MoneycontrolError` with an actionable message on failure.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any, Optional

import httpx

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

PRICE_API = "https://priceapi.moneycontrol.com/pricefeed"
TECHCHARTS = "https://priceapi.moneycontrol.com/techCharts/indianMarket/stock"
AUTOSUGGEST = "https://www.moneycontrol.com/mccode/common/autosuggestion_solr.php"
FII_DII_URL = "https://www.moneycontrol.com/markets/fii-dii-data/"

# Friendly history interval -> techCharts UDF resolution code.
HISTORY_RESOLUTIONS: dict[str, str] = {
    "1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30", "1h": "60",
    "daily": "D", "1d": "D", "weekly": "W", "1w": "W", "monthly": "M", "1mo": "M",
}

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
DEFAULT_HEADERS = {
    "User-Agent": _UA,
    "Accept": "application/json, text/javascript, text/html, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.moneycontrol.com/",
    "Origin": "https://www.moneycontrol.com",
    "X-Requested-With": "XMLHttpRequest",
}

# Autosuggest "type" discriminators.
SUGGEST_STOCK = "1"
SUGGEST_NEWS = "3"
SUGGEST_INDEX = "4"

# Valid stock exchanges for the equitycash pricefeed.
STOCK_EXCHANGES = {"nse", "bse"}

# Verified pricefeed codes for popular indices (key -> "in;<code>").
# Anything not listed here is resolved at runtime via :func:`resolve_index_code`.
INDEX_CODES: dict[str, str] = {
    "nifty 50": "NSX",
    "nifty": "NSX",
    "sensex": "SEN",
    "nifty bank": "nbx",
    "bank nifty": "nbx",
    "nifty it": "cnit",
    "nifty auto": "cnxa",
    "nifty pharma": "cpr",
    "nifty fmcg": "cfm",
    "nifty metal": "CNXM",
    "nifty midcap 100": "ccx",
    "nifty smallcap 100": "cnxs",
    "nifty 500": "ncx",
}

# Default search keywords per news category. Moneycontrol's RSS feeds are stale,
# so news is fetched live via the autosuggest news index (type=3), which returns
# current articles. A category just supplies a default query.
NEWS_CATEGORY_QUERY: dict[str, str] = {
    "markets": "stock market",
    "latest": "sensex nifty",
    "business": "business",
    "economy": "economy",
    "results": "earnings results",
    "stocks": "stock",
    "ipo": "IPO",
    "mutual-funds": "mutual fund",
}


class MoneycontrolError(Exception):
    """Raised when a Moneycontrol request fails or returns no usable data."""


# --------------------------------------------------------------------------- #
# Low-level HTTP
# --------------------------------------------------------------------------- #

async def _get(url: str, *, params: Optional[dict] = None, expect: str = "json") -> Any:
    """Perform a GET request and return parsed JSON, decoded text, or bytes.

    Args:
        url: Absolute URL to fetch.
        params: Optional query parameters.
        expect: ``"json"``, ``"text"``, or ``"bytes"``.

    Raises:
        MoneycontrolError: On network errors, non-2xx status, or JSON decode failure.
    """
    try:
        async with httpx.AsyncClient(headers=DEFAULT_HEADERS, follow_redirects=True) as client:
            resp = await client.get(url, params=params, timeout=20.0)
            resp.raise_for_status()
            if expect == "json":
                return resp.json()
            if expect == "bytes":
                return resp.content
            return resp.text
    except httpx.HTTPStatusError as exc:
        raise MoneycontrolError(
            f"Moneycontrol returned HTTP {exc.response.status_code} for {url}. "
            "The symbol/code may be wrong or the endpoint temporarily unavailable."
        ) from exc
    except httpx.TimeoutException as exc:
        raise MoneycontrolError("Request to Moneycontrol timed out. Please try again.") from exc
    except json.JSONDecodeError as exc:
        raise MoneycontrolError(
            f"Moneycontrol returned a non-JSON response for {url} (often an HTML error page). "
            "Check the parameters and try again."
        ) from exc
    except httpx.HTTPError as exc:
        raise MoneycontrolError(f"Network error contacting Moneycontrol: {exc}") from exc


# Keys we read off autosuggest records, for the lenient fallback parser.
_SUGGEST_FIELDS = ("link_src", "pdt_dis_nm", "name", "sc_id", "stock_name", "sc_sector", "symbol")


def _extract_suggest_records(text: str) -> list[dict]:
    """Extract autosuggest records even when the JSON is malformed.

    Moneycontrol's autosuggest frequently emits invalid JSON (unescaped quotes in
    news titles). Records are reliably delimited by ``"link_src":"``; this splits on
    that and pulls each field with a bounded regex, so one bad title doesn't sink
    the whole response.
    """
    records: list[dict] = []
    for chunk in text.split('"link_src":"')[1:]:
        chunk = '"link_src":"' + chunk
        rec: dict[str, str] = {}
        for key in _SUGGEST_FIELDS:
            m = re.search(rf'"{key}":"(.*?)"(?:,"|\}}|\])', chunk)
            if m:
                rec[key] = m.group(1)
        if rec.get("link_src"):
            records.append(rec)
    return records


async def _fetch_suggest(query: str, suggest_type: str) -> list[dict]:
    """Fetch autosuggest, tolerating throttled HTML, JSONP wrappers, and bad JSON.

    Retries with backoff when the endpoint returns no usable body (throttling),
    parses strict JSON when valid, and falls back to regex extraction otherwise.
    Returns ``[]`` rather than raising when nothing usable comes back.
    """
    params = {"classic": "true", "query": query, "type": suggest_type, "format": "json"}
    for attempt in range(3):
        text = (await _get(AUTOSUGGEST, params=params, expect="text")).strip()
        # Strip a JSONP wrapper like ``callback([...])`` if present.
        if text and not text.startswith("["):
            m = re.search(r"(\[.*\])", text, re.S)
            text = m.group(1) if m else ""
        if text.startswith("["):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    return parsed
            except json.JSONDecodeError:
                records = _extract_suggest_records(text)
                if records:
                    return records
        if attempt < 2:
            await asyncio.sleep(0.6 * (attempt + 1))  # brief backoff on throttle
    return []


def _unwrap_pricefeed(payload: dict, *, context: str) -> dict:
    """Validate a pricefeed envelope ({code, message, data}) and return ``data``."""
    if not isinstance(payload, dict):
        raise MoneycontrolError(f"Unexpected response shape for {context}.")
    code = str(payload.get("code", ""))
    data = payload.get("data")
    if code != "200" or not data:
        msg = payload.get("message") or "no data returned"
        raise MoneycontrolError(f"No Moneycontrol data for {context}: {msg}.")
    return data


# --------------------------------------------------------------------------- #
# Search / autosuggest
# --------------------------------------------------------------------------- #

_SPAN_RE = re.compile(r"<span>(.*?)</span>", re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def _clean(text: str) -> str:
    """Strip HTML tags and normalise the &nbsp; / whitespace used by autosuggest."""
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    return _TAG_RE.sub("", text).strip()


async def search(query: str, suggest_type: str = SUGGEST_STOCK, limit: int = 10) -> list[dict]:
    """Search Moneycontrol's autosuggest and return normalised matches.

    Returns a list of dicts with keys: ``sc_id``, ``name``, ``symbol``, ``isin``,
    ``bse_code``, ``sector``, ``link``. ``sc_id`` is the identifier used by the
    quote/fundamentals pricefeed.
    """
    raw = await _fetch_suggest(query, suggest_type)
    if not raw:
        return []

    results: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        sc_id = (item.get("sc_id") or "").strip()
        name = (item.get("name") or item.get("stock_name") or "").strip()
        if not name or name.startswith("No Result"):
            continue
        # pdt_dis_nm embeds "<span>ISIN, SYMBOL, BSECODE</span>" for equities.
        isin = symbol = bse_code = ""
        span = _SPAN_RE.search(item.get("pdt_dis_nm", ""))
        if span:
            parts = [p.strip() for p in _clean(span.group(1)).split(",") if p.strip()]
            for p in parts:
                if re.fullmatch(r"[A-Z]{2}[0-9A-Z]{9}[0-9]", p):
                    isin = p
                elif p.isdigit():
                    bse_code = p
                else:
                    symbol = p
        results.append(
            {
                "sc_id": sc_id,
                "name": _clean(name),
                "symbol": symbol or item.get("symbol", ""),
                "isin": isin,
                "bse_code": bse_code,
                "sector": item.get("sc_sector", ""),
                "link": item.get("link_src", ""),
            }
        )
        if len(results) >= limit:
            break
    return results


async def resolve_stock_id(query: str) -> dict:
    """Resolve a free-text stock query to its best-matching search record.

    Raises MoneycontrolError if nothing matches.
    """
    matches = await search(query, SUGGEST_STOCK, limit=5)
    matches = [m for m in matches if m["sc_id"]]
    if not matches:
        raise MoneycontrolError(
            f"No stock found matching '{query}'. Try a fuller company name "
            "(e.g. 'Reliance Industries', 'HDFC Bank')."
        )
    return matches[0]


# --------------------------------------------------------------------------- #
# Quotes / fundamentals (pricefeed equitycash)
# --------------------------------------------------------------------------- #

async def get_stock_pricefeed(sc_id: str, exchange: str = "nse") -> dict:
    """Fetch the raw equitycash pricefeed ``data`` block for a stock."""
    exchange = exchange.lower()
    if exchange not in STOCK_EXCHANGES:
        raise MoneycontrolError(f"exchange must be one of {sorted(STOCK_EXCHANGES)}, got '{exchange}'.")
    payload = await _get(f"{PRICE_API}/{exchange}/equitycash/{sc_id}", expect="json")
    return _unwrap_pricefeed(payload, context=f"{exchange.upper()} stock '{sc_id}'")


async def get_technicals(sc_id: str, exchange: str = "nse", period: str = "D") -> dict:
    """Fetch pivot-point / support-resistance technicals for a stock.

    ``period`` is one of ``D`` (daily), ``W`` (weekly), ``M`` (monthly).
    """
    period = period.upper()
    if period not in {"D", "W", "M"}:
        raise MoneycontrolError("period must be 'D', 'W', or 'M'.")
    payload = await _get(f"{PRICE_API}/techindicator/{period}/{sc_id}", expect="json")
    return _unwrap_pricefeed(payload, context=f"technicals for '{sc_id}'")


# --------------------------------------------------------------------------- #
# Historical OHLC (techCharts UDF feed)
# --------------------------------------------------------------------------- #

async def resolve_udf_ticker(query: str) -> str:
    """Resolve a name/symbol to the NSE trading ticker used by the techCharts feed.

    Returns the input unchanged if it already looks like a plain ticker and search
    yields nothing. Prefers the NSE listing.
    """
    raw = await _get(
        f"{TECHCHARTS}/search",
        params={"query": query, "type": "", "exchange": "", "limit": 10},
        expect="json",
    )
    if isinstance(raw, list) and raw:
        nse = [r for r in raw if isinstance(r, dict) and r.get("exchange") == "NSE" and r.get("type") == "stock"]
        chosen = (nse or [r for r in raw if isinstance(r, dict)])[0]
        ticker = (chosen.get("ticker") or chosen.get("symbol") or "").strip()
        if ticker:
            return ticker
    return query.strip().upper()


async def get_history(ticker: str, resolution: str = "D", count: int = 30) -> list[dict]:
    """Fetch historical OHLCV bars (most recent last) for an NSE ticker.

    ``resolution`` is a techCharts UDF code ('1','5','15','30','60','D','W','M').
    Uses ``countback`` (the ``from`` parameter is blocked by Moneycontrol's WAF).
    """
    payload = await _get(
        f"{TECHCHARTS}/history",
        params={"symbol": ticker, "resolution": resolution, "countback": count, "to": int(time.time())},
        expect="json",
    )
    if not isinstance(payload, dict):
        raise MoneycontrolError(f"Unexpected history response for '{ticker}'.")
    status = payload.get("s")
    if status == "no_data":
        raise MoneycontrolError(f"No historical data available for '{ticker}' at this resolution.")
    if status != "ok":
        raise MoneycontrolError(f"Could not fetch history for '{ticker}': {payload.get('errmsg', 'unknown error')}.")

    ts, o, h, l, c = payload.get("t", []), payload.get("o", []), payload.get("h", []), payload.get("l", []), payload.get("c", [])
    v = payload.get("v", [])
    bars: list[dict] = []
    for i, t in enumerate(ts):
        bars.append(
            {
                "time": time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if resolution.isdigit() else time.strftime("%Y-%m-%d", time.localtime(t)),
                "open": o[i] if i < len(o) else None,
                "high": h[i] if i < len(h) else None,
                "low": l[i] if i < len(l) else None,
                "close": c[i] if i < len(c) else None,
                "volume": v[i] if i < len(v) else None,
            }
        )
    if not bars:
        raise MoneycontrolError(f"No bars returned for '{ticker}'.")
    return bars


# --------------------------------------------------------------------------- #
# Indices
# --------------------------------------------------------------------------- #

_INDEX_CODE_RE = re.compile(r"inidicesindia/in%3B([A-Za-z0-9]+)", re.I)


async def resolve_index_code(name: str) -> str:
    """Resolve an index name to its pricefeed code (without the ``in;`` prefix).

    Checks the curated map first; otherwise scrapes the index landing page that
    autosuggest points to and extracts the embedded pricefeed code.
    """
    key = name.strip().lower()
    if key in INDEX_CODES:
        return INDEX_CODES[key]

    matches = await search(name, SUGGEST_INDEX, limit=1)
    link = matches[0]["link"] if matches else ""
    if not link or "indian-indices" not in link:
        raise MoneycontrolError(
            f"Could not resolve index '{name}'. Known indices include: "
            f"{', '.join(sorted(set(INDEX_CODES)))}."
        )
    html = await _get(link, expect="text")
    m = _INDEX_CODE_RE.search(html)
    if not m:
        raise MoneycontrolError(f"Found an index page for '{name}' but no pricefeed code on it.")
    return m.group(1)


async def get_index_pricefeed(name_or_code: str) -> dict:
    """Fetch the indices pricefeed for an index by friendly name or raw code."""
    token = name_or_code.strip()
    # Treat a short alnum token that isn't a known name as a raw code.
    if token.lower() in INDEX_CODES:
        code = INDEX_CODES[token.lower()]
    elif re.fullmatch(r"[A-Za-z0-9]{2,8}", token) and " " not in token and token.lower() not in INDEX_CODES:
        # Could be a raw code or a one-word name; try as code, fall back to resolve.
        code = token
    else:
        code = await resolve_index_code(token)

    payload = await _get(f"{PRICE_API}/notapplicable/inidicesindia/in%3B{code}", expect="json")
    try:
        return _unwrap_pricefeed(payload, context=f"index '{name_or_code}' (code {code})")
    except MoneycontrolError:
        # Token may have been a name we mis-treated as a code; retry via resolver.
        code = await resolve_index_code(token)
        payload = await _get(f"{PRICE_API}/notapplicable/inidicesindia/in%3B{code}", expect="json")
        return _unwrap_pricefeed(payload, context=f"index '{name_or_code}' (code {code})")


# --------------------------------------------------------------------------- #
# FII / DII activity
# --------------------------------------------------------------------------- #

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S
)


async def get_fii_dii(days: int = 10) -> list[dict]:
    """Return recent daily FII/DII activity rows (most-recent first).

    Each row includes cash-market net (``fiiCM``/``diiCM``) and F&O segments, in
    INR crore, plus Nifty/Sensex closes. Data is parsed from the page's embedded
    Next.js payload.
    """
    html = await _get(FII_DII_URL, expect="text")
    m = _NEXT_DATA_RE.search(html)
    if not m:
        raise MoneycontrolError("Could not locate FII/DII data on the Moneycontrol page (layout may have changed).")
    try:
        data = json.loads(m.group(1))
        rows = data["props"]["pageProps"]["FiiDiiData"]["fiiDiiData"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise MoneycontrolError("FII/DII page structure changed; could not extract the data table.") from exc
    if not isinstance(rows, list) or not rows:
        raise MoneycontrolError("Moneycontrol returned an empty FII/DII data set.")
    return rows[: max(1, days)]


# --------------------------------------------------------------------------- #
# News (live, via the autosuggest news index)
# --------------------------------------------------------------------------- #

async def search_news(query: str, limit: int = 15) -> list[dict]:
    """Search Moneycontrol's live news index for a topic, stock, or keyword.

    Returns a list of dicts with ``title`` and ``link`` (most recent / most
    relevant first). Use a stock or company name for stock-specific news, or a
    topic like 'sensex', 'rbi policy', 'crude oil'.
    """
    raw = await _fetch_suggest(query, SUGGEST_NEWS)
    if not raw:
        return []
    items: list[dict] = []
    seen: set[str] = set()
    for it in raw:
        if not isinstance(it, dict):
            continue
        link = (it.get("link_src") or "").strip()
        title = _clean(it.get("pdt_dis_nm") or it.get("name") or "")
        if not title or not link or "/news/" not in link or link in seen:
            continue
        seen.add(link)
        items.append({"title": title, "link": link})
        if len(items) >= max(1, limit):
            break
    return items
