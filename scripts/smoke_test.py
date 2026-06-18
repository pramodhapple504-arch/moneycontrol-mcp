#!/usr/bin/env python3
"""Live smoke test: call every Moneycontrol MCP tool and print results.

Run with:  uv run python scripts/smoke_test.py
Hits the real Moneycontrol endpoints, so it requires network access and the
results depend on current market data.
"""

import asyncio

from moneycontrol_mcp import server as s


async def main() -> None:
    fmt = s.ResponseFormat.MARKDOWN

    def banner(title: str) -> None:
        print("\n" + "=" * 70 + f"\n{title}\n" + "=" * 70)

    banner("search: Reliance")
    print(await s.moneycontrol_search(s.SearchInput(query="Reliance", kind="stock")))

    banner("quote: RI")
    print(await s.moneycontrol_get_quote(s.QuoteInput(symbol="RI")))

    banner("fundamentals: RI")
    print(await s.moneycontrol_get_fundamentals(s.FundamentalsInput(symbol="RI")))

    banner("quote by name auto-resolve: HDFC Bank")
    print(await s.moneycontrol_get_quote(s.QuoteInput(symbol="HDFC Bank")))

    banner("index: Nifty 50")
    print(await s.moneycontrol_get_index(s.IndexInput(index="Nifty 50")))

    banner("index runtime-resolve: Nifty Next 50")
    print(await s.moneycontrol_get_index(s.IndexInput(index="Nifty Next 50")))

    banner("FII/DII cash, 5 days")
    print(await s.moneycontrol_fii_dii(s.FiiDiiInput(days=5, segment="cash")))

    banner("FII/DII fno, 3 days")
    print(await s.moneycontrol_fii_dii(s.FiiDiiInput(days=3, segment="fno")))

    banner("news: markets category")
    print(await s.moneycontrol_get_news(s.NewsInput(category="markets", limit=5)))

    banner("news: stock-specific query 'Reliance'")
    print(await s.moneycontrol_get_news(s.NewsInput(query="Reliance", limit=5)))

    banner("technicals: RI daily")
    print(await s.moneycontrol_get_technicals(s.TechnicalsInput(symbol="RI", period="D")))

    banner("history: RELIANCE daily x5")
    print(await s.moneycontrol_get_history(s.HistoryInput(symbol="RELIANCE", interval="daily", count=5)))

    banner("history by name: Infosys weekly x4")
    print(await s.moneycontrol_get_history(s.HistoryInput(symbol="Infosys", interval="weekly", count=4)))


if __name__ == "__main__":
    asyncio.run(main())
