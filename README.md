# Polybot — a paper-trading bot for Polymarket

Polybot scans [Polymarket](https://polymarket.com) prediction markets, looks for
profitable trades, and **paper-trades** them — meaning it simulates every trade
with realistic prices and slippage but **never touches real money or a wallet.**
It tracks a virtual bankroll so you can see whether the strategy actually makes
money *before* risking a cent.

> ⚠️ **Reality check:** No trading bot can guarantee profit. Polymarket is a
> competitive real-money market. The point of paper trading is to find out,
> safely, whether this bot's edge is real. Treat early results as an experiment,
> not a promise.

---

## What it does

Every cycle (default: once a minute) the bot:

1. **Manages open positions** — settles markets that have resolved, and applies
   take-profit / stop-loss to open value bets.
2. **Scans markets** — pulls the most active markets and their live order books.
3. **Finds edges**, in priority order:
   - **Arbitrage (riskless):** in a Yes/No market, one Yes share + one No share
     always pay exactly \$1 at resolution. If you can buy both for *less than*
     \$1, the profit is locked in. Rare, but free money.
   - **Crypto model (the real edge):** for markets like *"Will Bitcoin be above
     \$X on <date>?"* the bot computes a genuine probability from the **live
     exchange spot price + realized volatility** (Binance/Coinbase), using a
     no-drift log-normal model. Because this estimate is *independent* of
     Polymarket's order book, it can legitimately disagree with a stale price.
     It bets only when the gap clears spread + fees + a margin.
   - **Book value (off by default):** the old order-book-imbalance + momentum
     signal. It has no proven edge and tends to bleed the spread, so it's
     disabled unless you set `enable_book_value: true`.
4. **Sizes bets** with conservative *fractional Kelly*, hard risk limits, and a
   **per-event correlation cap** so it can't pile into one news story.
5. **Paper-trades** the best opportunities and logs everything to a database.

**Optional AI layer (off by default):** when enabled, a Claude model reads each
market's question *and its resolution rules* to (a) price crypto markets the
simple parser misses and (b) **skip markets whose rules are tricky or
ambiguous** — a "gotcha" guard against bets that resolve in a way the bot
misread. See *Optional: AI market understanding* below.

---

## Setup (one time)

```bash
cd polybot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

No API key, wallet, or account is required — the bot only reads public data.

---

## Usage

### Web dashboard (recommended)

```bash
.venv/bin/python run.py web
```

Then open **http://localhost:8000** in your browser. From there you can:

- **▶ Start / ■ Stop** the trading loop
- **↻ Run one cycle** — do a single scan-and-trade pass on demand
- **🔍 Scan now** — see current opportunities without trading
- Watch the **equity curve**, **open positions**, **recent trades**, and a
  **live log** update automatically
- See the **AI panel**: what the AI reviewed, its verdict (trade / skip), its
  plain-English read of each market, and how many risky markets it skipped
- Tweak **strategy & risk settings** live (saved to `config.yaml`)

The bot starts **paused** — nothing trades until you click **Start**.

### Command line (optional)

```bash
.venv/bin/python run.py scan     # show opportunities, no trades (read-only)
.venv/bin/python run.py run      # run the trading loop in the terminal
.venv/bin/python run.py status   # print portfolio summary
.venv/bin/python run.py report   # performance + calibration on resolved trades
.venv/bin/python run.py reset    # wipe paper history and start over
```

Leave it going for days/weeks. The longer it runs, the more markets resolve and
the more you learn about whether the strategy works. State is saved to
`data/polybot.db`, so you can stop and restart without losing history.

**Want it always-on (so it keeps running when your Mac is off)?** Deploy it to
the cloud — see **[DEPLOY.md](DEPLOY.md)** for a step-by-step Render setup
(one always-on service + a persistent disk, ~$7/mo, still paper-only). This is
what you need to reach a `solid` performance report.

---

## Tuning (`config.yaml`)

Every setting is commented. The ones you'll touch most:

| Setting | What it does |
|---|---|
| `bankroll` | Starting virtual USDC. |
| `strategy.enable_crypto_model` | The real edge: price crypto markets vs live exchange data (on by default). |
| `strategy.crypto_min_edge` | How far the market must disagree with the model before betting (default 5¢). |
| `strategy.enable_llm_understanding` | Use a Claude model to read questions + resolution rules (needs `ANTHROPIC_API_KEY`; off by default). |
| `strategy.llm_model` | Which Claude model the AI layer uses (default `claude-haiku-4-5`). |
| `strategy.enable_book_value` | Turn the unproven order-book signal back on (off by default). |
| `strategy.arb_min_profit` | Minimum locked-in profit to take an arbitrage. |
| `risk.max_fraction_per_trade` | Most of the bankroll allowed in one position (default 5%). |
| `risk.max_fraction_per_theme` / `max_positions_per_theme` | Correlation cap: limits exposure to one event. |
| `risk.kelly_fraction` | How aggressive sizing is (0.25 = quarter-Kelly, conservative). |
| `risk.take_profit` / `stop_loss` | When to exit a winning / losing book-value bet. |
| `universe.min_liquidity` | Ignore thin, illiquid markets. |

---

## Judging the bot: the performance report

`python run.py report` (and the **Performance report** panel in the dashboard)
evaluate the bot on **resolved trades** — bets whose markets have actually
settled. It shows win rate (with a 95% confidence interval), realized P&L, ROI,
whether the result is **distinguishable from luck** yet, breakdowns by strategy
/ exit reason / AI-vetting, and a **calibration table** (of the bets the model
rated ~70%, did ~70% actually win?).

It also gives a **readiness verdict**. A trustworthy read needs **~100+
resolved trades, ideally 200+** — which for this bot is roughly a month or two
of continuous running. Until then, treat the numbers as a sanity check, not a
verdict. (Open positions don't count — only settled outcomes teach you
anything.)

---

## Optional: AI market understanding

By default the bot uses a simple text parser for crypto markets and reads no
fine print. You can optionally let a Claude model read each market's full
question **and resolution rules**, which:

- prices crypto markets phrased in ways the parser misses, and
- **skips markets with risky/ambiguous resolution** (e.g. "lowest price",
  "between $X and $Y", odd data sources) — avoiding bets the bot would misread.

It's **optional and safe**: with no API key the bot logs that it's off and uses
the parser — nothing breaks. Results are cached per market, and a cheap
pre-screen means it only spends tokens on plausibly-crypto markets, so cost is
tiny (it uses **Claude Haiku 4.5**, the cheapest/fastest model, by default).

To enable it:

```bash
cp .env.example .env                       # then edit .env and paste your key
# .env contains:  ANTHROPIC_API_KEY=sk-ant-...
# then in config.yaml, set:
#   strategy.enable_llm_understanding: true
.venv/bin/python run.py scan               # watch it analyze markets
```

Get a key at **https://platform.claude.com** (the account needs billing/credits).
Your `.env` is gitignored and never committed. You can point
`strategy.llm_model` at a stronger model (`claude-sonnet-4-6`, `claude-opus-4-8`)
for deeper reasoning at higher cost.

**Deploying to Render (or any host):** don't ship `.env` — set
`ANTHROPIC_API_KEY` in the host's environment/dashboard instead. Real
environment variables always take precedence over `.env`.

Once it's on, the dashboard's **AI panel** shows everything the AI does: a
tally of markets reviewed / cleared to trade / skipped as risky / crypto-
priceable, and a live feed of each market with the AI's verdict and its
one-line read of the resolution rules. Each opportunity also gets an **AI ✓**
badge (hover for the AI's summary) so you can see which trades it vetted.

---

## How it's built

```
run.py                 CLI: web / run / scan / status / reset
config.yaml            all settings
polybot/
  api/gamma.py         market catalogue (public API)
  api/clob.py          live order books (public API)
  models.py            Market / OrderBook / Opportunity + fill math
  strategy.py          arbitrage + crypto-model + (optional) book-value engine
  pricing.py           probability math (log-normal model, barrier touch)
  feeds.py             live exchange spot price + realized volatility
  crypto.py            parse crypto markets -> real fair value
  llm.py               optional Anthropic API client (graceful if no key)
  understanding.py     AI layer: read questions + resolution rules, gate risk
  report.py            performance + calibration report on resolved trades
  risk.py              fractional-Kelly sizing + limits + correlation cap
  execution.py         paper broker (realistic fills, fees, slippage)
  store.py             SQLite: positions, trades, equity curve
  bot.py               the scan → decide → trade → manage loop
  web/server.py        Flask API + BotRunner (background bot thread)
  web/index.html       the dashboard UI
tests/test_core.py     unit tests for the money math
tests/test_pricing.py  unit tests for the crypto model
```

Run the tests with:

```bash
.venv/bin/python tests/test_core.py
```

---

## Going live later (not yet enabled)

Real-money trading is intentionally **disabled** (`mode: live` refuses to run).
When you're ready and the paper results justify it, going live means adding
order *placement* via Polymarket's authenticated CLOB client (`py-clob-client`),
which needs a funded Polygon wallet and API credentials. The strategy, risk, and
accounting code is already structured so that only the `execution` layer needs a
live implementation. **Do not enable this until paper trading has proven an
edge over many resolved markets.**
```
