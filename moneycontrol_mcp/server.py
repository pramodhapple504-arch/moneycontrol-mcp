#!/usr/bin/env python3
"""MCP server for Moneycontrol market data.

Exposes Moneycontrol's public market data as MCP tools: symbol search, live
equity quotes, key fundamentals/ratios, index levels, FII/DII institutional
activity (cash + F&O), news headlines, and technical pivot levels.

Run locally over stdio:  ``python -m moneycontrol_mcp``  (or ``moneycontrol-mcp``)
"""

from __future__ import annotations

import json
from enum import Enum
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import client
from .client import MoneycontrolError

mcp = FastMCP("moneycontrol_mcp")

READ_ONLY = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": True,
}


# --------------------------------------------------------------------------- #
# Shared models / helpers
# --------------------------------------------------------------------------- #

class ResponseFormat(str, Enum):
    """Output format for tool responses."""

    MARKDOWN = "markdown"
    JSON = "json"


class _Base(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True, extra="forbid")


def _g(data: dict, *keys: str, default: str = "—") -> str:
    """Return the first present, non-empty value among ``keys`` as a string."""
    for k in keys:
        v = data.get(k)
        if v not in (None, "", "0", 0, "0.00"):
            return str(v)
    # fall back to a present-but-zero value if that is all we have
    for k in keys:
        if data.get(k) not in (None, ""):
            return str(data[k])
    return default


def _emit(payload: Any, lines: list[str], fmt: ResponseFormat) -> str:
    """Return JSON dump or joined markdown lines depending on ``fmt``."""
    if fmt == ResponseFormat.JSON:
        return json.dumps(payload, indent=2, ensure_ascii=False)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Tool: search
# --------------------------------------------------------------------------- #

class SearchInput(_Base):
    query: str = Field(..., description="Company/index name to search (e.g. 'Reliance', 'HDFC Bank', 'Nifty IT').", min_length=1, max_length=120)
    kind: str = Field(default="stock", description="What to search: 'stock' or 'index'.")
    limit: int = Field(default=10, description="Maximum matches to return.", ge=1, le=25)
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN, description="'markdown' or 'json'.")

    @field_validator("kind")
    @classmethod
    def _kind(cls, v: str) -> str:
        v = v.lower()
        if v not in {"stock", "index"}:
            raise ValueError("kind must be 'stock' or 'index'")
        return v


@mcp.tool(name="moneycontrol_search", annotations={"title": "Search Moneycontrol Symbols", **READ_ONLY})
async def moneycontrol_search(params: SearchInput) -> str:
    """Search Moneycontrol for a stock or index and resolve its identifiers.

    Use this FIRST to obtain a stock's ``sc_id`` (needed by moneycontrol_get_quote,
    moneycontrol_get_fundamentals, moneycontrol_get_technicals) or to discover an
    index name accepted by moneycontrol_get_index.

    Args:
        params (SearchInput):
            - query (str): Name to search.
            - kind (str): 'stock' (default) or 'index'.
            - limit (int): Max results (1-25, default 10).
            - response_format (ResponseFormat): 'markdown' or 'json'.

    Returns:
        str: Matches. Each record has:
        {
            "sc_id": str,     # Moneycontrol id, e.g. "RI" — pass to quote/fundamentals
            "name": str,      # Display name
            "symbol": str,    # Trading symbol, e.g. "RELIANCE"
            "isin": str,      # ISIN if available
            "bse_code": str,  # BSE numeric code if available
            "sector": str,
            "link": str       # Moneycontrol page URL
        }
        Returns "No matches..." when nothing is found.
    """
    try:
        kind = client.SUGGEST_INDEX if params.kind == "index" else client.SUGGEST_STOCK
        results = await client.search(params.query, kind, params.limit)
        if not results:
            return f"No {params.kind} matches found for '{params.query}'."
        if params.response_format == ResponseFormat.JSON:
            return json.dumps({"query": params.query, "count": len(results), "results": results}, indent=2, ensure_ascii=False)
        lines = [f"# {params.kind.title()} matches for '{params.query}'", ""]
        for r in results:
            extra = " · ".join(p for p in [r["symbol"], r["sector"], r["isin"]] if p)
            lines.append(f"- **{r['name']}** (sc_id: `{r['sc_id']}`){' — ' + extra if extra else ''}")
        return "\n".join(lines)
    except MoneycontrolError as e:
        return f"Error: {e}"


# --------------------------------------------------------------------------- #
# Tool: quote
# --------------------------------------------------------------------------- #

class QuoteInput(_Base):
    symbol: str = Field(..., description="Stock sc_id (e.g. 'RI') from moneycontrol_search, or a company name to auto-resolve.", min_length=1, max_length=120)
    exchange: str = Field(default="nse", description="Exchange: 'nse' or 'bse'.")
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN, description="'markdown' or 'json'.")

    @field_validator("exchange")
    @classmethod
    def _exch(cls, v: str) -> str:
        v = v.lower()
        if v not in client.STOCK_EXCHANGES:
            raise ValueError("exchange must be 'nse' or 'bse'")
        return v


async def _resolve_then(symbol, coro_factory):
    """Fetch stock data, resolving ``symbol`` to a valid sc_id.

    sc_ids are short alnum tokens (e.g. 'RI'), but users often pass an NSE ticker
    ('RELIANCE') or a name ('Reliance Industries'). Try the literal value as an
    sc_id first (cheap, correct for true sc_ids); if that fails — which it does for
    tickers/names — fall back to search-based resolution and retry.

    Args:
        symbol: User-supplied sc_id, ticker, or company name.
        coro_factory: async callable taking an sc_id and returning the fetched data.

    Returns:
        tuple[str, Any]: (resolved sc_id, fetched data).
    """
    looks_like_id = " " not in symbol and len(symbol) <= 12 and symbol.isalnum()
    if looks_like_id:
        try:
            return symbol, await coro_factory(symbol)
        except MoneycontrolError:
            pass  # not a valid sc_id — resolve via search below
    sc_id = (await client.resolve_stock_id(symbol))["sc_id"]
    return sc_id, await coro_factory(sc_id)


@mcp.tool(name="moneycontrol_get_quote", annotations={"title": "Get Stock Quote", **READ_ONLY})
async def moneycontrol_get_quote(params: QuoteInput) -> str:
    """Get a live equity quote: price, change, OHLC, 52-week range, volume, market cap.

    Args:
        params (QuoteInput):
            - symbol (str): sc_id (preferred) or company name.
            - exchange (str): 'nse' (default) or 'bse'.
            - response_format (ResponseFormat): 'markdown' or 'json'.

    Returns:
        str: Quote data with this shape (JSON mode):
        {
            "name": str, "symbol": str, "exchange": str, "sc_id": str,
            "price": str, "change": str, "percent_change": str, "prev_close": str,
            "open": str, "high": str, "volume": str,
            "week52_high": str, "week52_low": str,
            "market_cap_cr": str, "market_state": str, "last_updated": str
        }
        Returns "Error: ..." on failure (e.g. unknown symbol).
    """
    try:
        sc_id, d = await _resolve_then(params.symbol, lambda i: client.get_stock_pricefeed(i, params.exchange))
        payload = {
            "name": _g(d, "SC_FULLNM", "company"),
            "symbol": _g(d, "NSEID" if params.exchange == "nse" else "BSEID", "symbol"),
            "exchange": params.exchange.upper(),
            "sc_id": sc_id,
            "price": _g(d, "pricecurrent", "LP"),
            "change": _g(d, "pricechange"),
            "percent_change": _g(d, "pricepercentchange"),
            "prev_close": _g(d, "priceprevclose"),
            "open": _g(d, "OPN"),
            "high": _g(d, "HP"),
            "volume": _g(d, "VOL"),
            "week52_high": _g(d, "52H"),
            "week52_low": _g(d, "52L"),
            "market_cap_cr": _g(d, "MKTCAP"),
            "market_state": _g(d, "market_state", default=""),
            "last_updated": _g(d, "lastupd", default=""),
        }
        lines = [
            f"# {payload['name']} ({payload['symbol']}, {payload['exchange']})",
            "",
            f"**₹{payload['price']}**  ({payload['change']}, {payload['percent_change']}%)",
            "",
            f"- Open: ₹{payload['open']}  ·  High: ₹{payload['high']}  ·  Prev close: ₹{payload['prev_close']}",
            f"- 52-week range: ₹{payload['week52_low']} – ₹{payload['week52_high']}",
            f"- Volume: {payload['volume']}",
            f"- Market cap: ₹{payload['market_cap_cr']} cr",
        ]
        if payload["last_updated"]:
            lines.append(f"- Last updated: {payload['last_updated']} ({payload['market_state']})")
        return _emit(payload, lines, params.response_format)
    except MoneycontrolError as e:
        return f"Error: {e}"


# --------------------------------------------------------------------------- #
# Tool: fundamentals
# --------------------------------------------------------------------------- #

class FundamentalsInput(_Base):
    symbol: str = Field(..., description="Stock sc_id (e.g. 'RI') or company name.", min_length=1, max_length=120)
    exchange: str = Field(default="nse", description="Exchange: 'nse' or 'bse'.")
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN, description="'markdown' or 'json'.")

    @field_validator("exchange")
    @classmethod
    def _exch(cls, v: str) -> str:
        v = v.lower()
        if v not in client.STOCK_EXCHANGES:
            raise ValueError("exchange must be 'nse' or 'bse'")
        return v


@mcp.tool(name="moneycontrol_get_fundamentals", annotations={"title": "Get Stock Fundamentals & Ratios", **READ_ONLY})
async def moneycontrol_get_fundamentals(params: FundamentalsInput) -> str:
    """Get valuation ratios, per-share metrics, sector and trailing returns for a stock.

    Covers P/E (standalone & consolidated), industry P/E, P/B, book value, cash EPS,
    face value, dividend yield, market cap, sector classification, and price returns
    over 1w / 1m / 3m / 1y / YTD plus 5-year CAGR.

    Args:
        params (FundamentalsInput):
            - symbol (str): sc_id (preferred) or company name.
            - exchange (str): 'nse' (default) or 'bse'.
            - response_format (ResponseFormat): 'markdown' or 'json'.

    Returns:
        str: Fundamentals with this shape (JSON mode):
        {
            "name": str, "sc_id": str, "sector": str, "sub_sector": str,
            "pe": str, "pe_consolidated": str, "industry_pe": str,
            "pb": str, "book_value": str, "cash_eps": str, "face_value": str,
            "dividend_yield": str, "market_cap_cr": str,
            "returns": {"1w": str, "1m": str, "3m": str, "1y": str, "ytd": str, "cagr_5y": str}
        }
        Returns "Error: ..." on failure.
    """
    try:
        sc_id, d = await _resolve_then(params.symbol, lambda i: client.get_stock_pricefeed(i, params.exchange))
        returns = {
            "1w": _g(d, "cl1wPerChange"),
            "1m": _g(d, "cl1mPerChange"),
            "3m": _g(d, "cl3mPerChange"),
            "1y": _g(d, "cl1yPerChange"),
            "ytd": _g(d, "clYtdPerChange"),
            "cagr_5y": _g(d, "cagr5Y"),
        }
        payload = {
            "name": _g(d, "SC_FULLNM", "company"),
            "sc_id": sc_id,
            "sector": _g(d, "main_sector", default=""),
            "sub_sector": _g(d, "newSubsector", "SC_SUBSEC", default=""),
            "pe": _g(d, "PE"),
            "pe_consolidated": _g(d, "PECONS"),
            "industry_pe": _g(d, "IND_PE"),
            "pb": _g(d, "PB", "PBCONS"),
            "book_value": _g(d, "BV", "BVCONS"),
            "cash_eps": _g(d, "CEPS"),
            "face_value": _g(d, "FV"),
            "dividend_yield": _g(d, "DY", "DYCONS"),
            "market_cap_cr": _g(d, "MKTCAP"),
            "returns": returns,
        }
        lines = [
            f"# Fundamentals — {payload['name']}",
            f"_{payload['sector']} · {payload['sub_sector']}_".strip(" _"),
            "",
            "## Valuation",
            f"- P/E (standalone): {payload['pe']}  ·  P/E (consolidated): {payload['pe_consolidated']}  ·  Industry P/E: {payload['industry_pe']}",
            f"- P/B: {payload['pb']}  ·  Book value: ₹{payload['book_value']}",
            f"- Cash EPS: ₹{payload['cash_eps']}  ·  Face value: ₹{payload['face_value']}",
            f"- Dividend yield: {payload['dividend_yield']}%  ·  Market cap: ₹{payload['market_cap_cr']} cr",
            "",
            "## Price returns",
            f"- 1W {returns['1w']}%  ·  1M {returns['1m']}%  ·  3M {returns['3m']}%",
            f"- 1Y {returns['1y']}%  ·  YTD {returns['ytd']}%  ·  5Y CAGR {returns['cagr_5y']}%",
        ]
        return _emit(payload, lines, params.response_format)
    except MoneycontrolError as e:
        return f"Error: {e}"


# --------------------------------------------------------------------------- #
# Tool: index
# --------------------------------------------------------------------------- #

class IndexInput(_Base):
    index: str = Field(..., description="Index name (e.g. 'Nifty 50', 'Sensex', 'Nifty Bank', 'Nifty IT') or raw pricefeed code.", min_length=1, max_length=60)
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN, description="'markdown' or 'json'.")


@mcp.tool(name="moneycontrol_get_index", annotations={"title": "Get Index Level", **READ_ONLY})
async def moneycontrol_get_index(params: IndexInput) -> str:
    """Get the current level and movement for a market index.

    Built-in fast-path names: Nifty 50, Sensex, Nifty Bank, Nifty IT, Nifty Auto,
    Nifty Pharma, Nifty FMCG, Nifty Metal, Nifty Midcap 100, Nifty Smallcap 100,
    Nifty 500. Other index names are resolved automatically via Moneycontrol search.

    Args:
        params (IndexInput):
            - index (str): Index name or raw pricefeed code.
            - response_format (ResponseFormat): 'markdown' or 'json'.

    Returns:
        str: Index data with this shape (JSON mode):
        {
            "name": str, "level": str, "change": str, "percent_change": str,
            "open": str, "high": str, "low": str, "prev_close": str,
            "advances": str, "declines": str,
            "year_high": str, "year_low": str, "ytd_percent": str
        }
        Returns "Error: ..." on failure.
    """
    try:
        d = await client.get_index_pricefeed(params.index)
        payload = {
            "name": _g(d, "company", "HN", default=params.index),
            "level": _g(d, "pricecurrent"),
            "change": _g(d, "pricechange"),
            "percent_change": _g(d, "pricepercentchange"),
            "open": _g(d, "OPEN"),
            "high": _g(d, "HIGH"),
            "low": _g(d, "LOW"),
            "prev_close": _g(d, "priceprevclose"),
            "advances": _g(d, "adv", default=""),
            "declines": _g(d, "decl", default=""),
            "year_high": _g(d, "52wkhi"),
            "year_low": _g(d, "52wklow"),
            "ytd_percent": _g(d, "YTD", default=""),
        }
        lines = [
            f"# {payload['name']}",
            "",
            f"**{payload['level']}**  ({payload['change']}, {payload['percent_change']}%)",
            "",
            f"- Open: {payload['open']}  ·  High: {payload['high']}  ·  Low: {payload['low']}  ·  Prev close: {payload['prev_close']}",
            f"- 52-week range: {payload['year_low']} – {payload['year_high']}",
        ]
        if payload["advances"] or payload["declines"]:
            lines.append(f"- Advances/Declines: {payload['advances']} / {payload['declines']}")
        return _emit(payload, lines, params.response_format)
    except MoneycontrolError as e:
        return f"Error: {e}"


# --------------------------------------------------------------------------- #
# Tool: FII / DII
# --------------------------------------------------------------------------- #

class FiiDiiInput(_Base):
    days: int = Field(default=10, description="Number of recent trading days to return.", ge=1, le=60)
    segment: str = Field(default="cash", description="'cash' (cash market only), 'fno' (F&O segments), or 'all'.")
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN, description="'markdown' or 'json'.")

    @field_validator("segment")
    @classmethod
    def _seg(cls, v: str) -> str:
        v = v.lower()
        if v not in {"cash", "fno", "all"}:
            raise ValueError("segment must be 'cash', 'fno', or 'all'")
        return v


def _f(num: str) -> float:
    """Parse a Moneycontrol number string like '-5,722.25' to float (0.0 on failure)."""
    try:
        return float(str(num).replace(",", ""))
    except (ValueError, TypeError):
        return 0.0


@mcp.tool(name="moneycontrol_fii_dii", annotations={"title": "Get FII/DII Activity", **READ_ONLY})
async def moneycontrol_fii_dii(params: FiiDiiInput) -> str:
    """Get FII (foreign) and DII (domestic) institutional net activity, in INR crore.

    The cash market is the headline figure most analysts watch: positive FII cash =
    foreign buying. F&O segments (index/stock futures & options) are also available.
    A positive number = net buying; negative = net selling.

    Args:
        params (FiiDiiInput):
            - days (int): Recent trading days to return (1-60, default 10).
            - segment (str): 'cash' (default), 'fno', or 'all'.
            - response_format (ResponseFormat): 'markdown' or 'json'.

    Returns:
        str: A list of daily rows (most recent first). Each row (JSON mode):
        {
            "date": str, "fii_cash": float, "dii_cash": float, "net_cash": float,
            # when segment is 'fno' or 'all':
            "fii_index_fut": float, "fii_index_opt": float,
            "fii_stock_fut": float, "fii_stock_opt": float,
            "nifty_close": str, "nifty_change_pct": str
        }
        All cash/F&O figures are net values in INR crore. Returns "Error: ..." on failure.
    """
    try:
        rows = await client.get_fii_dii(params.days)
        out: list[dict] = []
        for r in rows:
            fii_cash, dii_cash = _f(r.get("fiiCM")), _f(r.get("diiCM"))
            rec: dict[str, Any] = {
                "date": r.get("date", ""),
                "fii_cash": fii_cash,
                "dii_cash": dii_cash,
                "net_cash": round(fii_cash + dii_cash, 2),
            }
            if params.segment in {"fno", "all"}:
                rec.update(
                    {
                        "fii_index_fut": _f(r.get("fiiIdxFut")),
                        "fii_index_opt": _f(r.get("fiiIdxOpt")),
                        "fii_stock_fut": _f(r.get("fiiStkFut")),
                        "fii_stock_opt": _f(r.get("fiiStkOpt")),
                    }
                )
            if params.segment == "all":
                rec["nifty_close"] = r.get("niftyClose", "")
                rec["nifty_change_pct"] = r.get("niftyChangePer", "")
            out.append(rec)

        if params.response_format == ResponseFormat.JSON:
            return json.dumps({"segment": params.segment, "count": len(out), "unit": "INR crore", "data": out}, indent=2, ensure_ascii=False)

        lines = [f"# FII/DII activity — {params.segment} (₹ crore, net; +buy / −sell)", ""]
        if params.segment == "cash":
            lines.append("| Date | FII cash | DII cash | Net |")
            lines.append("|------|---------:|---------:|----:|")
            for r in out:
                lines.append(f"| {r['date']} | {r['fii_cash']:,.0f} | {r['dii_cash']:,.0f} | {r['net_cash']:,.0f} |")
        else:
            lines.append("| Date | FII cash | DII cash | FII idx fut | FII idx opt | FII stk fut | FII stk opt |")
            lines.append("|------|---------:|---------:|------------:|------------:|------------:|------------:|")
            for r in out:
                lines.append(
                    f"| {r['date']} | {r['fii_cash']:,.0f} | {r['dii_cash']:,.0f} | "
                    f"{r.get('fii_index_fut', 0):,.0f} | {r.get('fii_index_opt', 0):,.0f} | "
                    f"{r.get('fii_stock_fut', 0):,.0f} | {r.get('fii_stock_opt', 0):,.0f} |"
                )
        return "\n".join(lines)
    except MoneycontrolError as e:
        return f"Error: {e}"


# --------------------------------------------------------------------------- #
# Tool: news
# --------------------------------------------------------------------------- #

class NewsInput(_Base):
    query: Optional[str] = Field(default=None, description="Topic, stock, or keyword to search news for (e.g. 'Reliance', 'RBI policy', 'crude oil'). If omitted, uses the category default.", max_length=120)
    category: str = Field(default="markets", description="Used only when query is omitted: markets, latest, business, economy, results, stocks, ipo, mutual-funds.")
    limit: int = Field(default=15, description="Maximum headlines to return.", ge=1, le=50)
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN, description="'markdown' or 'json'.")

    @field_validator("category")
    @classmethod
    def _cat(cls, v: str) -> str:
        v = v.lower()
        if v not in client.NEWS_CATEGORY_QUERY:
            raise ValueError(f"category must be one of {sorted(client.NEWS_CATEGORY_QUERY)}")
        return v


@mcp.tool(name="moneycontrol_get_news", annotations={"title": "Get Market / Stock News", **READ_ONLY})
async def moneycontrol_get_news(params: NewsInput) -> str:
    """Get current Moneycontrol news headlines for a topic, stock, or category.

    Pass a ``query`` for targeted news (a company name gives stock-specific news; a
    topic like 'rbi policy' or 'crude oil' gives that theme). With no query, a
    ``category`` supplies a sensible default search: markets, latest, business,
    economy, results, stocks, ipo, mutual-funds.

    Args:
        params (NewsInput):
            - query (Optional[str]): Topic/stock/keyword. Overrides category.
            - category (str): Default category when query is omitted ('markets').
            - limit (int): Max headlines (1-50, default 15).
            - response_format (ResponseFormat): 'markdown' or 'json'.

    Returns:
        str: A list of current articles. Each (JSON mode):
        {"title": str, "link": str}
        Returns "No news found..." when nothing matches.
    """
    try:
        effective = params.query or client.NEWS_CATEGORY_QUERY[params.category]
        items = await client.search_news(effective, params.limit)
        label = params.query or params.category
        if not items:
            return f"No news found for '{label}'."
        if params.response_format == ResponseFormat.JSON:
            return json.dumps({"query": effective, "count": len(items), "articles": items}, indent=2, ensure_ascii=False)
        lines = [f"# Moneycontrol news — {label}", ""]
        for it in items:
            lines.append(f"- [{it['title']}]({it['link']})")
        return "\n".join(lines)
    except MoneycontrolError as e:
        return f"Error: {e}"


# --------------------------------------------------------------------------- #
# Tool: technicals
# --------------------------------------------------------------------------- #

class TechnicalsInput(_Base):
    symbol: str = Field(..., description="Stock sc_id (e.g. 'RI') or company name.", min_length=1, max_length=120)
    exchange: str = Field(default="nse", description="Exchange: 'nse' or 'bse'.")
    period: str = Field(default="D", description="Pivot timeframe: 'D' daily, 'W' weekly, 'M' monthly.")
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN, description="'markdown' or 'json'.")

    @field_validator("exchange")
    @classmethod
    def _exch(cls, v: str) -> str:
        v = v.lower()
        if v not in client.STOCK_EXCHANGES:
            raise ValueError("exchange must be 'nse' or 'bse'")
        return v

    @field_validator("period")
    @classmethod
    def _period(cls, v: str) -> str:
        v = v.upper()
        if v not in {"D", "W", "M"}:
            raise ValueError("period must be 'D', 'W', or 'M'")
        return v


@mcp.tool(name="moneycontrol_get_technicals", annotations={"title": "Get Pivot / Support-Resistance Levels", **READ_ONLY})
async def moneycontrol_get_technicals(params: TechnicalsInput) -> str:
    """Get pivot points and support/resistance levels for a stock.

    Args:
        params (TechnicalsInput):
            - symbol (str): sc_id (preferred) or company name.
            - exchange (str): 'nse' (default) or 'bse'.
            - period (str): 'D' (daily, default), 'W', or 'M'.
            - response_format (ResponseFormat): 'markdown' or 'json'.

    Returns:
        str: OHLC for the period plus pivot tables. JSON mode returns the raw
        Moneycontrol structure including ``pivotLevels`` (Classic, Fibonacci, etc.).
        Returns "Error: ..." on failure.
    """
    try:
        sc_id, d = await _resolve_then(params.symbol, lambda i: client.get_technicals(i, params.exchange, params.period))
        if params.response_format == ResponseFormat.JSON:
            return json.dumps(d, indent=2, ensure_ascii=False)
        lines = [
            f"# Technicals — {sc_id} ({params.period})",
            "",
            f"- OHLC: O {_g(d, 'open')} · H {_g(d, 'high')} · L {_g(d, 'low')} · C {_g(d, 'close')} (prev {_g(d, 'pclose')})",
            "",
        ]
        for pv in d.get("pivotLevels", []) or []:
            lvl = pv.get("pivotLevel", {})
            lines.append(f"## {pv.get('key', 'Pivot')}")
            lines.append(f"- Pivot: {lvl.get('pivotPoint', '—')}")
            lines.append(f"- Resistance: R1 {lvl.get('r1','—')} · R2 {lvl.get('r2','—')} · R3 {lvl.get('r3','—')}")
            lines.append(f"- Support: S1 {lvl.get('s1','—')} · S2 {lvl.get('s2','—')} · S3 {lvl.get('s3','—')}")
            lines.append("")
        return "\n".join(lines)
    except MoneycontrolError as e:
        return f"Error: {e}"


# --------------------------------------------------------------------------- #
# Tool: historical OHLC
# --------------------------------------------------------------------------- #

class HistoryInput(_Base):
    symbol: str = Field(..., description="NSE trading symbol (e.g. 'RELIANCE') or company name to auto-resolve.", min_length=1, max_length=120)
    interval: str = Field(default="daily", description="Bar interval: 1m, 5m, 15m, 30m, 1h, daily, weekly, monthly.")
    count: int = Field(default=30, description="Number of most-recent bars to return.", ge=1, le=500)
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN, description="'markdown' or 'json'.")

    @field_validator("interval")
    @classmethod
    def _interval(cls, v: str) -> str:
        v = v.lower()
        if v not in client.HISTORY_RESOLUTIONS:
            raise ValueError(f"interval must be one of {sorted(client.HISTORY_RESOLUTIONS)}")
        return v


@mcp.tool(name="moneycontrol_get_history", annotations={"title": "Get Historical OHLC Prices", **READ_ONLY})
async def moneycontrol_get_history(params: HistoryInput) -> str:
    """Get historical OHLCV price bars for an NSE stock (daily/weekly/monthly or intraday).

    Args:
        params (HistoryInput):
            - symbol (str): NSE trading symbol (e.g. 'RELIANCE', 'HDFCBANK') or company name.
            - interval (str): 1m, 5m, 15m, 30m, 1h, daily (default), weekly, monthly.
            - count (int): Number of most-recent bars (1-500, default 30).
            - response_format (ResponseFormat): 'markdown' or 'json'.

    Returns:
        str: Bars ordered oldest → newest. Each bar (JSON mode):
        {"time": str, "open": float, "high": float, "low": float, "close": float, "volume": float}
        JSON mode wraps them as {"symbol", "ticker", "interval", "count", "bars": [...]}.
        Returns "Error: ..." on failure (e.g. no data at that resolution).
    """
    try:
        ticker = await client.resolve_udf_ticker(params.symbol)
        resolution = client.HISTORY_RESOLUTIONS[params.interval]
        bars = await client.get_history(ticker, resolution, params.count)
        if params.response_format == ResponseFormat.JSON:
            return json.dumps(
                {"symbol": params.symbol, "ticker": ticker, "interval": params.interval, "count": len(bars), "bars": bars},
                indent=2, ensure_ascii=False,
            )
        lines = [
            f"# {ticker} — {params.interval} history ({len(bars)} bars)",
            "",
            "| Time | Open | High | Low | Close | Volume |",
            "|------|-----:|-----:|----:|------:|-------:|",
        ]
        for b in bars:
            vol = f"{b['volume']:,.0f}" if isinstance(b["volume"], (int, float)) else "—"
            lines.append(f"| {b['time']} | {b['open']} | {b['high']} | {b['low']} | {b['close']} | {vol} |")
        return "\n".join(lines)
    except MoneycontrolError as e:
        return f"Error: {e}"


def main() -> None:
    """Console-script entry point: run the server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
