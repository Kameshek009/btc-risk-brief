"""Собрать риск-данные по криптоактиву за 2 года из бесплатных публичных API.

Не подключается к Binance, не зовёт Anthropic API. Просто скачивает данные,
считает метрики и печатает в терминал JSON / текст / Markdown.
Дальше человек (или LLM в чате) интерпретирует.

Запуск:
  python scripts/risk_brief.py                              # BTC, JSON + текст
  python scripts/risk_brief.py --symbol ETH                 # любой тикер CryptoCompare
  python scripts/risk_brief.py --json-only / --text-only / --markdown
  python scripts/risk_brief.py --chart reports/btc.png      # сохранить PNG-чарт
  python scripts/risk_brief.py --interval 3600              # цикл каждые N секунд
  python scripts/risk_brief.py --history 10                 # последние 10 записей из jsonl и выход
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass  # .env опционален

from risk_brief import diff as rdiff  # noqa: E402
from risk_brief import metrics as rm  # noqa: E402
from risk_brief import sources as rs  # noqa: E402


CACHE_DIR = PROJECT_ROOT / "risk_brief" / "cache"
LOGS_DIR = PROJECT_ROOT / "logs"
JSONL_PATH = LOGS_DIR / "risk_brief.jsonl"
BTC_ONLY_ONCHAIN = {"BTC"}


def _fmt_pct(x) -> str:
    if x is None:
        return "—"
    return f"{x:+.2f}%"


def _fmt_num(x, digits: int = 2) -> str:
    if x is None:
        return "—"
    return f"{x:,.{digits}f}"


def render_text(m: dict) -> str:
    sym = m.get("symbol", "BTC")
    price = m["price_usd"]
    r = m["returns_pct"]
    sma = m["sma"]
    rng = m["range_position"]
    ath = m["ath_2y"]
    mdd = m["max_drawdown_2y"]
    fng = m.get("fear_greed") or {}
    onch = m.get("onchain") or {}
    fees = (m.get("mempool") or {}).get("fees_sat_vb") or {}
    corr = m.get("correlations") or {}
    tl = m.get("risk_traffic_light") or {}
    rq = m.get("risk_quantiles") or {}
    vt = m.get("volatility_term") or {}
    macro = m.get("crypto_macro") or {}
    stbs = macro.get("stablecoins") or {}
    deriv = m.get("derivatives") or {}

    lines = []
    lines.append(f"{sym} risk brief — as of {m['as_of_utc']}  (data through {m['data_through_utc'][:10]})")
    if tl:
        lines.append(f"RISK: {tl.get('level','?').upper()}   score {tl.get('score_0_10','?')}/10")
        for f in (tl.get("factors") or [])[:8]:
            lines.append(f"  - {f}")
    lines.append("")
    lines.append(
        f"Price:   ${_fmt_num(price)}    1d {_fmt_pct(r['1d'])}   7d {_fmt_pct(r['7d'])}   30d {_fmt_pct(r['30d'])}   90d {_fmt_pct(r['90d'])}   365d {_fmt_pct(r['365d'])}"
    )
    lines.append(
        f"Vola ann.:  7d {_fmt_num(vt.get('7d_ann_pct'),1)}%   30d {_fmt_num(vt.get('30d_ann_pct'),1)}%   90d {_fmt_num(vt.get('90d_ann_pct'),1)}%   180d {_fmt_num(vt.get('180d_ann_pct'),1)}%"
    )
    lines.append(
        f"VaR 1d:   95% {_fmt_num(rq.get('var_1d_95_pct'),2)}%   99% {_fmt_num(rq.get('var_1d_99_pct'),2)}%   "
        f"ES 95% {_fmt_num(rq.get('es_1d_95_pct'),2)}%   worst {_fmt_num(rq.get('worst_1d_pct'),2)}%"
    )
    lines.append(f"RSI14: {_fmt_num(m['rsi14'], 1)}")
    lines.append(
        f"SMA50 {_fmt_num(sma['sma50'])}  SMA200 {_fmt_num(sma['sma200'])}  "
        f"price>SMA50={sma['price_above_sma50']}  price>SMA200={sma['price_above_sma200']}  "
        f"golden({'+' if sma['sma50_above_sma200'] else '-' if sma['sma50_above_sma200'] is not None else '?'})"
    )
    lines.append(f"Range pos: 30d {rng['30d']:.2f}   90d {rng['90d']:.2f}   365d {rng['365d']:.2f}")
    lines.append(
        f"ATH(2y): ${_fmt_num(ath['price'])} on {ath['date'][:10]}   drawdown {ath['drawdown_from_ath_pct']:+.2f}%"
    )
    lines.append(f"MDD(2y): {mdd['pct']:+.2f}% over {mdd['duration_days']} d")
    if fng:
        lines.append(
            f"Fear&Greed: {fng['latest']} ({fng['latest_class']})   7d avg {fng['avg_7d']:.1f}   30d avg {fng['avg_30d']:.1f}   extreme days/30d {fng['extreme_days_30d']}"
        )
    if macro:
        bits = []
        if macro.get("btc_dominance_pct") is not None:
            bits.append(f"BTC.D {macro['btc_dominance_pct']:.2f}%")
        if macro.get("eth_dominance_pct") is not None:
            bits.append(f"ETH.D {macro['eth_dominance_pct']:.2f}%")
        if macro.get("total_mcap_usd") is not None:
            bits.append(f"total mcap ${macro['total_mcap_usd']/1e12:.2f}T")
        if macro.get("mcap_change_24h_pct") is not None:
            bits.append(f"24h Δ {macro['mcap_change_24h_pct']:+.2f}%")
        if bits:
            lines.append("Crypto macro: " + "   ".join(bits))
    if stbs:
        bits = []
        if stbs.get("usdt_mcap_usd") is not None:
            bits.append(f"USDT ${stbs['usdt_mcap_usd']/1e9:.1f}B")
        if stbs.get("usdc_mcap_usd") is not None:
            bits.append(f"USDC ${stbs['usdc_mcap_usd']/1e9:.1f}B")
        if stbs.get("total_change_30d_pct") is not None:
            bits.append(f"total Δ30d {stbs['total_change_30d_pct']:+.2f}%")
        elif stbs.get("usdt_change_30d_pct") is not None:
            bits.append(f"USDT Δ30d {stbs['usdt_change_30d_pct']:+.2f}%")
        if bits:
            lines.append("Stablecoins: " + "   ".join(bits))
    if deriv:
        bb = deriv.get("bybit") or {}
        ok = deriv.get("okx") or {}
        bits = []
        if bb.get("funding_rate_8h_pct") is not None:
            bits.append(f"Bybit funding {bb['funding_rate_8h_pct']:+.4f}%/8h")
        if bb.get("open_interest_btc") is not None:
            bits.append(f"Bybit OI {bb['open_interest_btc']:,.0f} BTC")
        if ok.get("funding_rate_8h_pct") is not None:
            bits.append(f"OKX funding {ok['funding_rate_8h_pct']:+.4f}%/8h")
        if ok.get("open_interest_usd") is not None:
            bits.append(f"OKX OI ${ok['open_interest_usd']/1e9:.2f}B")
        if bits:
            lines.append("Derivatives: " + "   ".join(bits))
    if onch.get("hashrate_latest") is not None:
        lines.append(
            f"Hashrate: {_fmt_num(onch['hashrate_latest'])}  Δ30d {_fmt_pct(onch['hashrate_change_30d_pct'])}  Δ90d {_fmt_pct(onch['hashrate_change_90d_pct'])}    Difficulty Δ30d {_fmt_pct(onch['difficulty_change_30d_pct'])}"
        )
    if fees:
        lines.append(
            f"Mempool fees (sat/vB): fast {fees.get('fastestFee')}  30m {fees.get('halfHourFee')}  1h {fees.get('hourFee')}  econ {fees.get('economyFee')}"
        )
    if corr:
        spx = corr.get("spx") or {}
        dxy = corr.get("dxy") or {}
        lines.append(
            f"Corr  SPX: 30d {_fmt_num(spx.get('30d'), 2)} / 90d {_fmt_num(spx.get('90d'), 2)}    "
            f"DXY: 30d {_fmt_num(dxy.get('30d'), 2)} / 90d {_fmt_num(dxy.get('90d'), 2)}"
        )
    news = m.get("news_recent") or []
    if news:
        lines.append("")
        lines.append(f"Recent {sym} headlines:")
        for n in news[:8]:
            ts = (n.get("ts") or "")[:16]
            lines.append(f"  [{ts}] {n.get('title','')[:120]}  ({n.get('src','')})")
    return "\n".join(lines)


def render_markdown(m: dict, diff: dict | None = None) -> str:
    """Markdown-вариант — удобно копировать в чат-LLM."""
    sym = m.get("symbol", "BTC")
    tl = m.get("risk_traffic_light") or {}
    r = m["returns_pct"]
    sma = m["sma"]
    rng = m["range_position"]
    rq = m.get("risk_quantiles") or {}
    vt = m.get("volatility_term") or {}
    macro = m.get("crypto_macro") or {}
    stbs = macro.get("stablecoins") or {}
    deriv = m.get("derivatives") or {}
    fng = m.get("fear_greed") or {}
    onch = m.get("onchain") or {}
    corr = m.get("correlations") or {}
    ath = m.get("ath_2y") or {}
    mdd = m.get("max_drawdown_2y") or {}

    lines: list[str] = []
    lines.append(f"# {sym} risk brief")
    lines.append(f"_as of {m['as_of_utc']} (data through {m['data_through_utc'][:10]})_")
    lines.append("")
    lines.append(f"**Risk verdict:** `{tl.get('level','?').upper()}` — score **{tl.get('score_0_10','?')}/10**")
    if tl.get("factors"):
        lines.append("")
        lines.append("**Why:**")
        for f in tl["factors"]:
            lines.append(f"- {f}")

    if diff and (diff.get("changes") or diff.get("new_factors") or diff.get("resolved_factors")):
        lines.append("")
        lines.append("## Changes since previous run")
        for c in diff.get("changes") or []:
            lines.append(f"- {c}")
        for f in diff.get("new_factors") or []:
            lines.append(f"- **new factor:** {f}")
        for f in diff.get("resolved_factors") or []:
            lines.append(f"- _resolved:_ {f}")

    lines.append("")
    lines.append("## Price & returns")
    lines.append("| metric | value |")
    lines.append("|---|---|")
    lines.append(f"| Price (USD) | ${_fmt_num(m['price_usd'])} |")
    for k in ("1d", "7d", "30d", "90d", "365d"):
        lines.append(f"| Return {k} | {_fmt_pct(r.get(k))} |")
    lines.append(f"| ATH 2y | ${_fmt_num(ath.get('price'))} on {(ath.get('date') or '')[:10]} (Δ {_fmt_pct(ath.get('drawdown_from_ath_pct'))}) |")
    lines.append(f"| MDD 2y | {_fmt_pct(mdd.get('pct'))} over {mdd.get('duration_days','?')} d |")

    lines.append("")
    lines.append("## Volatility & quantile risk")
    lines.append("| window | annualized vola |   | quantile | 1d loss |")
    lines.append("|---|---|---|---|---|")
    lines.append(
        f"| 7d  | {_fmt_num(vt.get('7d_ann_pct'),1)}% |   | VaR 95% | {_fmt_num(rq.get('var_1d_95_pct'),2)}% |"
    )
    lines.append(
        f"| 30d | {_fmt_num(vt.get('30d_ann_pct'),1)}% |   | VaR 99% | {_fmt_num(rq.get('var_1d_99_pct'),2)}% |"
    )
    lines.append(
        f"| 90d | {_fmt_num(vt.get('90d_ann_pct'),1)}% |   | ES 95%  | {_fmt_num(rq.get('es_1d_95_pct'),2)}% |"
    )
    lines.append(
        f"| 180d| {_fmt_num(vt.get('180d_ann_pct'),1)}% |   | worst 1d| {_fmt_num(rq.get('worst_1d_pct'),2)}% |"
    )

    lines.append("")
    lines.append("## Trend")
    lines.append("| metric | value |")
    lines.append("|---|---|")
    lines.append(f"| RSI14 | {_fmt_num(m['rsi14'],1)} |")
    lines.append(f"| SMA50 / SMA200 | {_fmt_num(sma.get('sma50'))} / {_fmt_num(sma.get('sma200'))} |")
    lines.append(f"| price > SMA50 / SMA200 | {sma.get('price_above_sma50')} / {sma.get('price_above_sma200')} |")
    lines.append(f"| SMA50 > SMA200 | {sma.get('sma50_above_sma200')} |")
    lines.append(f"| Range pos 30 / 90 / 365 d | {rng['30d']:.2f} / {rng['90d']:.2f} / {rng['365d']:.2f} |")

    if fng:
        lines.append("")
        lines.append("## Sentiment")
        lines.append(f"- Fear&Greed: **{fng['latest']}** ({fng['latest_class']})")
        lines.append(f"- 7d avg {fng['avg_7d']:.1f} · 30d avg {fng['avg_30d']:.1f} · extreme days /30d: {fng['extreme_days_30d']}")

    if macro:
        lines.append("")
        lines.append("## Crypto macro")
        if macro.get("btc_dominance_pct") is not None:
            lines.append(f"- BTC dominance: {macro['btc_dominance_pct']:.2f}%   |   ETH dominance: {macro.get('eth_dominance_pct',0):.2f}%")
        if macro.get("total_mcap_usd") is not None:
            lines.append(f"- Total crypto mcap: ${macro['total_mcap_usd']/1e12:.2f}T   |   24h Δ {macro.get('mcap_change_24h_pct',0):+.2f}%")
        if stbs:
            usdt = stbs.get('usdt_mcap_usd') or 0
            usdc = stbs.get('usdc_mcap_usd') or 0
            lines.append(f"- USDT mcap ${usdt/1e9:.1f}B (Δ30d {_fmt_pct(stbs.get('usdt_change_30d_pct'))}, Δ90d {_fmt_pct(stbs.get('usdt_change_90d_pct'))})")
            lines.append(f"- USDC mcap ${usdc/1e9:.1f}B (Δ30d {_fmt_pct(stbs.get('usdc_change_30d_pct'))}, Δ90d {_fmt_pct(stbs.get('usdc_change_90d_pct'))})")
            if stbs.get("total_change_30d_pct") is not None:
                lines.append(f"- Stablecoins total Δ30d: **{stbs['total_change_30d_pct']:+.2f}%**")

    if deriv:
        bb = deriv.get("bybit") or {}
        ok = deriv.get("okx") or {}
        lines.append("")
        lines.append("## Derivatives (non-Binance)")
        if bb:
            lines.append(
                f"- Bybit BTC-perp funding: {_fmt_num(bb.get('funding_rate_8h_pct'),4)}%/8h   "
                f"OI: {_fmt_num(bb.get('open_interest_btc'),0)} BTC"
            )
        if ok:
            lines.append(
                f"- OKX BTC-perp funding: {_fmt_num(ok.get('funding_rate_8h_pct'),4)}%/8h   "
                f"OI: ${(ok.get('open_interest_usd') or 0)/1e9:.2f}B"
            )

    if onch.get("hashrate_latest") is not None:
        lines.append("")
        lines.append("## On-chain (BTC)")
        lines.append(
            f"- Hashrate Δ30d {_fmt_pct(onch.get('hashrate_change_30d_pct'))}, Δ90d {_fmt_pct(onch.get('hashrate_change_90d_pct'))}"
        )
        lines.append(f"- Difficulty Δ30d {_fmt_pct(onch.get('difficulty_change_30d_pct'))}")
        fees = (m.get("mempool") or {}).get("fees_sat_vb") or {}
        if fees:
            lines.append(
                f"- Mempool fees sat/vB: fast {fees.get('fastestFee')} · 30m {fees.get('halfHourFee')} · 1h {fees.get('hourFee')} · econ {fees.get('economyFee')}"
            )

    if corr:
        spx = corr.get("spx") or {}
        dxy = corr.get("dxy") or {}
        lines.append("")
        lines.append("## Correlations with TradFi")
        lines.append("| asset | 30d | 90d |")
        lines.append("|---|---|---|")
        lines.append(f"| SPX | {_fmt_num(spx.get('30d'),2)} | {_fmt_num(spx.get('90d'),2)} |")
        lines.append(f"| DXY | {_fmt_num(dxy.get('30d'),2)} | {_fmt_num(dxy.get('90d'),2)} |")

    news = m.get("news_recent") or []
    if news:
        lines.append("")
        lines.append(f"## Recent {sym} headlines")
        for n in news[:8]:
            ts = (n.get("ts") or "")[:16]
            lines.append(f"- `{ts}` {n.get('title','')[:140]} _({n.get('src','')})_")

    return "\n".join(lines)


def render_chart(m: dict, crypto_df, chart_path: Path) -> None:
    """PNG: цена + SMA50/200 + просадка от running-max."""
    import matplotlib  # noqa: WPS433
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    df = crypto_df.copy().set_index("date")
    price = df["price"]
    sma50 = price.rolling(50).mean()
    sma200 = price.rolling(200).mean()
    running_max = price.cummax()
    dd = (price - running_max) / running_max * 100.0

    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(12, 7), sharex=True, gridspec_kw={"height_ratios": [3, 1]}
    )
    ax1.plot(price.index, price.values, label=f"{m.get('symbol','BTC')} close", linewidth=1.3)
    ax1.plot(sma50.index, sma50.values, label="SMA50", linewidth=1.0, alpha=0.8)
    ax1.plot(sma200.index, sma200.values, label="SMA200", linewidth=1.0, alpha=0.8)
    ax1.set_ylabel("Price, USD")
    ax1.grid(True, alpha=0.3)
    ax1.legend(loc="upper left")
    tl = m.get("risk_traffic_light") or {}
    ax1.set_title(
        f"{m.get('symbol','BTC')}   risk={tl.get('level','?').upper()}   score={tl.get('score_0_10','?')}/10"
    )
    ax2.fill_between(dd.index, dd.values, 0, color="crimson", alpha=0.4)
    ax2.set_ylabel("Drawdown, %")
    ax2.grid(True, alpha=0.3)
    ax2.set_ylim(min(-1.0, float(np.nanmin(dd.values))) - 5, 2)
    chart_path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(chart_path, dpi=120)
    plt.close(fig)


def print_history(jsonl_path: Path, symbol: str, tail: int) -> int:
    if not jsonl_path.exists():
        print(f"no history file: {jsonl_path}", file=sys.stderr)
        return 1
    rows: list[dict] = []
    with jsonl_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("symbol", "BTC") != symbol:
                continue
            rows.append(rec)
    rows = rows[-tail:]
    if not rows:
        print(f"no records for {symbol} in {jsonl_path}", file=sys.stderr)
        return 1
    header = f"{'as_of (UTC)':<20} {'lvl':<8} {'score':>5} {'price':>12} {'dd_ATH%':>9} {'F&G':>5} {'vola30%':>8}"
    print(header)
    print("-" * len(header))
    for rec in rows:
        as_of = (rec.get("as_of_utc") or "")[:19]
        tl = rec.get("risk_traffic_light") or {}
        price = rec.get("price_usd") or 0
        ath = rec.get("ath_2y") or {}
        fng = rec.get("fear_greed") or {}
        vola = rec.get("volatility_30d_annualized_pct")
        print(
            f"{as_of:<20} {str(tl.get('level','?')).upper():<8} "
            f"{str(tl.get('score_0_10','?')):>5} "
            f"{price:>12,.2f} "
            f"{(ath.get('drawdown_from_ath_pct') or 0):>+9.2f} "
            f"{str(fng.get('latest','?')):>5} "
            f"{(vola or 0):>8.1f}"
        )
    return 0


def run_once(args, log: logging.Logger) -> int:
    sym = args.symbol.upper()
    log.info("fetching %s daily history (%d days) from CryptoCompare", sym, args.days)
    crypto = rs.fetch_crypto_history(CACHE_DIR, symbol=sym, days=args.days)

    if sym in BTC_ONLY_ONCHAIN:
        log.info("fetching Fear&Greed history")
        fng = rs.fetch_fear_greed(CACHE_DIR, limit=args.days)
        log.info("fetching hashrate/difficulty from blockchain.com")
        hashrate = rs.fetch_blockchain_chart(CACHE_DIR, "hash-rate", timespan="2years")
        difficulty = rs.fetch_blockchain_chart(CACHE_DIR, "difficulty", timespan="2years")
        log.info("fetching mempool snapshot")
        try:
            mempool_fees = rs.fetch_mempool_fees()
        except Exception as exc:  # noqa: BLE001
            log.warning("mempool fees fetch failed: %s", exc)
            mempool_fees = {}
        try:
            mempool_size = rs.fetch_mempool_size()
        except Exception as exc:  # noqa: BLE001
            log.warning("mempool size fetch failed: %s", exc)
            mempool_size = {}
    else:
        import pandas as pd
        fng = pd.DataFrame()
        hashrate = pd.DataFrame()
        difficulty = pd.DataFrame()
        mempool_fees = {}
        mempool_size = {}

    log.info("fetching SPX and DXY from Yahoo Finance")
    try:
        spx = rs.fetch_yahoo_daily(CACHE_DIR, "^GSPC", days=args.days)
    except Exception as exc:  # noqa: BLE001
        log.warning("yahoo SPX failed: %s", exc); spx = None
    try:
        dxy = rs.fetch_yahoo_daily(CACHE_DIR, "DX-Y.NYB", days=args.days)
    except Exception as exc:  # noqa: BLE001
        log.warning("yahoo DXY failed: %s", exc); dxy = None

    log.info("fetching CoinGecko global + stablecoin charts")
    try:
        cg_global = rs.fetch_coingecko_global(CACHE_DIR)
    except Exception as exc:  # noqa: BLE001
        log.warning("coingecko global failed: %s", exc); cg_global = {}
    try:
        usdt_chart = rs.fetch_coingecko_market_chart(CACHE_DIR, "tether", days=365)
    except Exception as exc:  # noqa: BLE001
        log.warning("coingecko tether failed: %s", exc); usdt_chart = None
    try:
        usdc_chart = rs.fetch_coingecko_market_chart(CACHE_DIR, "usd-coin", days=365)
    except Exception as exc:  # noqa: BLE001
        log.warning("coingecko usdc failed: %s", exc); usdc_chart = None

    log.info("fetching Bybit/OKX derivatives (BTC-perp)")
    bybit_funding = rs.fetch_bybit_funding("BTCUSDT", limit=20)
    bybit_oi = rs.fetch_bybit_oi("BTCUSDT", interval="1h", limit=24)
    okx_funding = rs.fetch_okx_funding("BTC-USDT-SWAP")
    okx_oi = rs.fetch_okx_oi("BTC-USDT-SWAP")

    cp_token = os.getenv("CRYPTOPANIC_TOKEN", "")
    log.info("fetching CryptoPanic headlines (token=%s)", "yes" if cp_token else "no")
    news = rs.fetch_cryptopanic_recent(cp_token, symbol=sym, limit=30)

    m = rm.compute_metrics(
        crypto, fng, hashrate, difficulty, mempool_fees, mempool_size, news,
        symbol=sym, spx=spx, dxy=dxy,
        cg_global=cg_global, usdt_chart=usdt_chart, usdc_chart=usdc_chart,
        bybit_funding=bybit_funding, bybit_oi=bybit_oi,
        okx_funding=okx_funding, okx_oi=okx_oi,
    )

    # Diff с предыдущей записью (если есть)
    prev = rdiff.previous_record(JSONL_PATH, sym)
    diff = rdiff.diff_block(prev, m)

    payload = json.dumps(m, ensure_ascii=False, indent=2, default=str)
    text = render_text(m)
    markdown = render_markdown(m, diff=diff)
    diff_text = rdiff.render_diff_text(diff)

    if args.markdown:
        print(markdown)
    elif args.text_only:
        print(text)
        if diff_text:
            print()
            print(diff_text)
    elif args.json_only:
        out = dict(m)
        if diff:
            out["_diff_vs_previous"] = diff
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    else:
        print(text)
        if diff_text:
            print()
            print(diff_text)
        print()
        print("---- JSON ----")
        print(payload)

    if args.chart:
        chart_path = Path(args.chart).expanduser()
        if not chart_path.is_absolute():
            chart_path = PROJECT_ROOT / chart_path
        try:
            render_chart(m, crypto, chart_path)
            log.info("chart saved: %s", chart_path)
        except Exception as exc:  # noqa: BLE001
            log.warning("chart render failed: %s", exc)

    if args.report_dir:
        report_dir = Path(args.report_dir).expanduser()
        try:
            save_report_bundle(report_dir, sym, m, crypto, markdown, text, payload, log)
        except Exception as exc:  # noqa: BLE001
            log.warning("report bundle save failed: %s", exc)

    if not args.no_log:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        with JSONL_PATH.open("a") as f:
            f.write(json.dumps(m, ensure_ascii=False, default=str) + "\n")
    return 0


def save_report_bundle(
    report_dir: Path,
    symbol: str,
    m: dict,
    crypto,
    markdown: str,
    plain_text: str,
    json_payload: str,
    log: logging.Logger,
) -> None:
    """Сохранить в report_dir пару `latest_<SYM>.{txt,md,json,png}` плюс
    датированную копию в подпапке archive/."""
    report_dir.mkdir(parents=True, exist_ok=True)
    archive_dir = report_dir / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)

    stamp = (m.get("as_of_utc") or "").replace(":", "-").replace("+00-00", "Z")[:19]
    base = f"{symbol}_{stamp}"

    latest_txt = report_dir / f"latest_{symbol}.txt"
    latest_md = report_dir / f"latest_{symbol}.md"
    latest_json = report_dir / f"latest_{symbol}.json"
    latest_png = report_dir / f"latest_{symbol}.png"
    arch_txt = archive_dir / f"{base}.txt"
    arch_md = archive_dir / f"{base}.md"
    arch_json = archive_dir / f"{base}.json"
    arch_png = archive_dir / f"{base}.png"

    latest_txt.write_text(plain_text + "\n", encoding="utf-8")
    arch_txt.write_text(plain_text + "\n", encoding="utf-8")
    latest_md.write_text(markdown, encoding="utf-8")
    arch_md.write_text(markdown, encoding="utf-8")
    latest_json.write_text(json_payload, encoding="utf-8")
    arch_json.write_text(json_payload, encoding="utf-8")
    try:
        render_chart(m, crypto, latest_png)
        arch_png.write_bytes(latest_png.read_bytes())
    except Exception as exc:  # noqa: BLE001
        log.warning("chart for report bundle failed: %s", exc)

    log.info("report bundle saved to %s (latest + archive/%s.*)", report_dir, base)


_stop = False


def _on_sigint(signum, frame) -> None:  # noqa: ARG001
    global _stop
    _stop = True


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--symbol", default="BTC", help="Тикер CryptoCompare (BTC/ETH/SOL/...)")
    p.add_argument("--json-only", action="store_true")
    p.add_argument("--text-only", action="store_true")
    p.add_argument("--markdown", action="store_true", help="Markdown-вывод для копипасты в LLM-чат")
    p.add_argument("--days", type=int, default=730, help="История, дни (до 2000)")
    p.add_argument("--no-log", action="store_true", help="Не писать в logs/risk_brief.jsonl")
    p.add_argument("--chart", default=None, help="Путь для PNG-чарта")
    p.add_argument(
        "--report-dir", default=None,
        help="Папка, куда сохранять полный отчёт каждого прогона (md/json/png + archive/). "
             "Пример: ~/Desktop/report",
    )
    p.add_argument("--interval", type=int, default=0, help="Сек между прогонами; 0 = одноразово")
    p.add_argument("--history", type=int, default=0, help="Показать N последних записей и выйти")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    log = logging.getLogger("risk_brief")

    if args.history > 0:
        return print_history(JSONL_PATH, args.symbol.upper(), args.history)

    signal.signal(signal.SIGINT, _on_sigint)
    signal.signal(signal.SIGTERM, _on_sigint)

    if args.interval <= 0:
        return run_once(args, log)

    log.info("loop mode: every %d seconds (Ctrl+C to stop)", args.interval)
    while not _stop:
        try:
            run_once(args, log)
        except Exception as exc:  # noqa: BLE001
            log.error("run_once failed: %s", exc)
        for _ in range(args.interval):
            if _stop:
                break
            time.sleep(1)
    log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
