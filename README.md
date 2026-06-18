# Moneycontrol MCP Server

An [MCP](https://modelcontextprotocol.io) server that exposes **Moneycontrol** market
data as tools an AI agent can call: symbol search, live equity quotes, fundamentals &
ratios, index levels, **FII/DII** institutional activity (cash market + F&O), market
news, and technical pivot levels.

> Moneycontrol has no official public API. This server talks to the same public
> endpoints the Moneycontrol website and app use (`priceapi.moneycontrol.com`,
> the autosuggest service, the FII/DII page's embedded data, and RSS feeds). It is
> intended for personal/informational use; respect Moneycontrol's terms of service and
> avoid hammering the endpoints. All data is **read-only**.

## Tools

| Tool | What it does |
|------|--------------|
| `moneycontrol_search` | Search a stock or index by name → resolve its `sc_id`, symbol, ISIN, sector. **Call this first** to get the `sc_id` other tools need. |
| `moneycontrol_get_quote` | Live equity quote: price, change, OHLC, 52-week range, volume, market cap. |
| `moneycontrol_get_fundamentals` | Valuation ratios (P/E standalone & consolidated, industry P/E, P/B), book value, cash EPS, face value, dividend yield, sector, and 1w–5y returns. |
| `moneycontrol_get_index` | Current level/movement for an index (Nifty 50, Sensex, Nifty Bank, sectoral indices, …). |
| `moneycontrol_fii_dii` | FII & DII net activity in ₹ crore — **cash market** headline plus F&O segments, by day. |
| `moneycontrol_get_news` | Current news headlines for a topic/stock (`query`) or a category (markets, latest, business, economy, results, stocks, ipo, mutual-funds). Pass a company name for stock-specific news. |
| `moneycontrol_get_technicals` | Pivot points and support/resistance levels (daily/weekly/monthly). |

Every tool accepts `response_format: "markdown"` (default, human-readable) or `"json"`
(structured, for programmatic use).

## Install

Requires Python 3.10+. Uses [uv](https://docs.astral.sh/uv/) (or plain `pip`).

```bash
uv venv --python 3.11
uv pip install -e .
```

## Run

```bash
# stdio transport (for local MCP clients)
uv run moneycontrol-mcp
# or
uv run python -m moneycontrol_mcp
```

### Use with Claude Code / Claude Desktop

Add to your MCP client config (e.g. `claude_desktop_config.json`), using absolute paths:

```json
{
  "mcpServers": {
    "moneycontrol": {
      "command": "uv",
      "args": ["--directory", "/ABSOLUTE/PATH/TO/Money control mcp", "run", "moneycontrol-mcp"]
    }
  }
}
```

In Claude Code:

```bash
claude mcp add moneycontrol -- uv --directory "/ABSOLUTE/PATH/TO/Money control mcp" run moneycontrol-mcp
```

## Quick test

```bash
uv run python scripts/smoke_test.py
```

This calls every tool against the live endpoints and prints the results.

## Example agent flow

1. `moneycontrol_search(query="HDFC Bank")` → `sc_id: "HDF01"`
2. `moneycontrol_get_quote(symbol="HDF01")` → live price
3. `moneycontrol_get_fundamentals(symbol="HDF01")` → P/E, P/B, dividend yield
4. `moneycontrol_fii_dii(days=5, segment="cash")` → were foreigners buying this week?

(The quote/fundamentals/technicals tools also accept a plain company name and will
auto-resolve it via search, but passing the `sc_id` is faster and unambiguous.)

## Notes & limitations

- **Cash market** FII/DII figures (`fiiCM`/`diiCM`) are the headline numbers; F&O
  segments are also exposed via `segment="fno"` or `"all"`.
- Index codes for the most common indices are built in; any other index name is
  resolved at runtime via Moneycontrol search.
- Commodity/forex quotes are **not** included: Moneycontrol's public price feed
  requires contract/expiry-specific codes for those and is not reliably accessible.
- Data reflects whatever Moneycontrol publishes (often delayed during market hours;
  provisional FII/DII data updates after market close).
