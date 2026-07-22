---
name: Market Brief
description: Produce a concise trading brief for an index or symbol using ICT structure.
---

# Market Brief

Use this when the user asks for a read on a market/symbol (e.g. "how's nifty looking",
"give me a brief on banknifty").

## Steps

1. **Identify the symbol.** Default to `nifty` if the user didn't name one. Accept
   `nifty`, `banknifty`, `sensex`, or a ticker.
2. **Pull structure.** Call `ict_scan` for the symbol on a 15m interval to get the
   current ICT read (bias, key levels, FVGs, liquidity).
3. **Summarize the state**, in this order:
   - **Bias** — bullish / bearish / ranging, in one line.
   - **Key levels** — nearest support and resistance that matter now.
   - **Setups** — any FVG or liquidity level worth watching, with the trigger.
4. **Add context only if asked** — don't dump raw indicator values unprompted.

## Rules

- This is analysis, not financial advice — say so briefly if the user asks whether to
  buy/sell.
- Keep it to a tight paragraph or a few bullets. Traders want signal, not an essay.
- If the market service is unreachable, say so and offer to retry rather than inventing levels.
