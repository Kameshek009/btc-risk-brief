"""Расчёт риск-метрик по 2-летним daily-данным крипты + корреляции с традами."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd


def _rsi(series: pd.Series, length: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(length).mean()
    loss = -delta.clip(upper=0).rolling(length).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def _max_drawdown(prices: pd.Series) -> tuple[float, int]:
    """Возвращает MDD (%, отрицательное) и длину просадки в днях до new high."""
    if prices.empty:
        return 0.0, 0
    running_max = prices.cummax()
    dd = (prices - running_max) / running_max
    trough_idx = dd.idxmin()
    mdd = float(dd.min() * 100.0)
    # длительность: от пика до восстановления (или до конца ряда, если ещё не восстановилось)
    peak_idx = prices.loc[:trough_idx].idxmax()
    after = prices.loc[trough_idx:]
    recovery = after[after >= prices.loc[peak_idx]]
    end_idx = recovery.index[0] if not recovery.empty else prices.index[-1]
    duration_days = (end_idx - peak_idx).days if hasattr(end_idx - peak_idx, "days") else 0
    return mdd, duration_days


def _position_in_range(price: float, window: pd.Series) -> float:
    if window.empty:
        return 0.5
    lo, hi = float(window.min()), float(window.max())
    if hi <= lo:
        return 0.5
    return max(0.0, min(1.0, (price - lo) / (hi - lo)))


def _pct_change(latest: float, past: Optional[float]) -> Optional[float]:
    if past is None or past == 0:
        return None
    return (latest - past) / past * 100.0


def _var_es(returns: pd.Series, q: float) -> tuple[Optional[float], Optional[float]]:
    """Historical 1d VaR и ES (CVaR) на уровне q (например 0.95).
    Возвращает в процентах (отрицательные значения = потери)."""
    r = returns.dropna()
    if r.empty or len(r) < 30:
        return None, None
    cutoff = float(np.quantile(r, 1.0 - q))
    var_pct = cutoff * 100.0
    tail = r[r <= cutoff]
    es_pct = float(tail.mean() * 100.0) if not tail.empty else var_pct
    return var_pct, es_pct


def _vola_window(log_returns: pd.Series, days: int) -> Optional[float]:
    tail = log_returns.tail(days).dropna()
    if len(tail) < max(5, days // 4):
        return None
    return float(tail.std() * np.sqrt(365) * 100.0)


def _corr_windows(daily_returns: pd.Series, other_close: pd.DataFrame) -> dict:
    """Корреляции daily-логдоходностей с close-серией другого актива (Stooq, daily)."""
    if other_close.empty or daily_returns.empty:
        return {"30d": None, "90d": None, "n_overlap": 0}
    s = other_close.copy().set_index("date")["close"]
    s = s.tz_convert("UTC") if s.index.tz is not None else s.tz_localize("UTC")
    other_ret = np.log(s / s.shift(1))
    # Align по датам (только пересечение торговых дней)
    df = pd.concat([daily_returns.rename("crypto"), other_ret.rename("other")], axis=1).dropna()
    if df.empty:
        return {"30d": None, "90d": None, "n_overlap": 0}
    c30 = float(df.tail(30)["crypto"].corr(df.tail(30)["other"])) if len(df) >= 5 else None
    c90 = float(df.tail(90)["crypto"].corr(df.tail(90)["other"])) if len(df) >= 10 else None
    return {"30d": c30, "90d": c90, "n_overlap": int(len(df))}


def _mcap_trend(df: pd.DataFrame, days: int) -> Optional[float]:
    if df is None or df.empty:
        return None
    df = df.set_index("date").sort_index()
    latest = float(df["market_cap"].iloc[-1])
    target = df.index[-1] - pd.Timedelta(days=days)
    prev = df[df.index <= target]
    if prev.empty:
        return None
    return _pct_change(latest, float(prev["market_cap"].iloc[-1]))


def compute_metrics(
    crypto: pd.DataFrame,
    fng: pd.DataFrame,
    hashrate: pd.DataFrame,
    difficulty: pd.DataFrame,
    mempool_fees: dict,
    mempool_size: dict,
    news: list[dict],
    symbol: str = "BTC",
    spx: Optional[pd.DataFrame] = None,
    dxy: Optional[pd.DataFrame] = None,
    cg_global: Optional[dict] = None,
    usdt_chart: Optional[pd.DataFrame] = None,
    usdc_chart: Optional[pd.DataFrame] = None,
    bybit_funding: Optional[list[dict]] = None,
    bybit_oi: Optional[list[dict]] = None,
    okx_funding: Optional[dict] = None,
    okx_oi: Optional[dict] = None,
) -> dict:
    btc = crypto.copy()
    btc = btc.set_index("date")
    price = btc["price"]
    latest_date = price.index[-1]
    latest = float(price.iloc[-1])

    def _at_offset(days: int) -> Optional[float]:
        target = latest_date - pd.Timedelta(days=days)
        prev = price[price.index <= target]
        return float(prev.iloc[-1]) if not prev.empty else None

    # Returns
    ret_24h = _pct_change(latest, _at_offset(1))
    ret_7d = _pct_change(latest, _at_offset(7))
    ret_30d = _pct_change(latest, _at_offset(30))
    ret_90d = _pct_change(latest, _at_offset(90))
    ret_365d = _pct_change(latest, _at_offset(365))

    # Volatility 30d + терм-структура
    log_ret = np.log(price / price.shift(1))
    vol_30d_ann = float(log_ret.tail(30).std() * np.sqrt(365) * 100.0)
    vola_term = {
        "7d_ann_pct": _vola_window(log_ret, 7),
        "30d_ann_pct": vol_30d_ann,
        "90d_ann_pct": _vola_window(log_ret, 90),
        "180d_ann_pct": _vola_window(log_ret, 180),
    }

    # Historical VaR / ES (1d, по daily returns за весь период)
    daily_pct = price.pct_change().dropna()
    var95, es95 = _var_es(daily_pct, 0.95)
    var99, es99 = _var_es(daily_pct, 0.99)
    risk_quantiles = {
        "var_1d_95_pct": var95, "es_1d_95_pct": es95,
        "var_1d_99_pct": var99, "es_1d_99_pct": es99,
        "worst_1d_pct": float(daily_pct.min() * 100.0) if not daily_pct.empty else None,
    }

    # SMA/RSI
    sma50 = price.rolling(50).mean().iloc[-1]
    sma200 = price.rolling(200).mean().iloc[-1]
    rsi14 = float(_rsi(price, 14).iloc[-1])

    # Range positions
    pos_30d = _position_in_range(latest, price.tail(30))
    pos_90d = _position_in_range(latest, price.tail(90))
    pos_365d = _position_in_range(latest, price.tail(365))

    # ATH (за 2 года)
    ath = float(price.max())
    ath_date = price.idxmax()
    drawdown_from_ath = (latest - ath) / ath * 100.0

    # Max drawdown за весь период
    mdd_pct, mdd_days = _max_drawdown(price)

    # Volume
    vol_latest = float(btc["volume"].iloc[-1])
    vol_avg_30d = float(btc["volume"].tail(30).mean())
    vol_ratio_30d = vol_latest / vol_avg_30d if vol_avg_30d > 0 else None

    # Fear & Greed
    fng_block = {}
    if not fng.empty:
        fng = fng.set_index("date").sort_index()
        fng_latest_row = fng.iloc[-1]
        fng_block = {
            "latest": int(fng_latest_row["fng_value"]),
            "latest_class": fng_latest_row["fng_class"],
            "avg_7d": float(fng["fng_value"].tail(7).mean()),
            "avg_30d": float(fng["fng_value"].tail(30).mean()),
            "extreme_days_30d": int(((fng["fng_value"].tail(30) >= 75) | (fng["fng_value"].tail(30) <= 25)).sum()),
        }

    # On-chain trend
    def _trend_pct(df: pd.DataFrame, days: int) -> Optional[float]:
        if df.empty:
            return None
        df_s = df.set_index("date").sort_index()
        latest_v = float(df_s["value"].iloc[-1])
        target = df_s.index[-1] - pd.Timedelta(days=days)
        prev = df_s[df_s.index <= target]
        if prev.empty:
            return None
        return _pct_change(latest_v, float(prev["value"].iloc[-1]))

    onchain = {
        "hashrate_latest": float(hashrate["value"].iloc[-1]) if not hashrate.empty else None,
        "hashrate_change_30d_pct": _trend_pct(hashrate, 30),
        "hashrate_change_90d_pct": _trend_pct(hashrate, 90),
        "difficulty_latest": float(difficulty["value"].iloc[-1]) if not difficulty.empty else None,
        "difficulty_change_30d_pct": _trend_pct(difficulty, 30),
    }

    mempool = {
        "fees_sat_vb": mempool_fees,
        "size": {
            "count": mempool_size.get("count"),
            "vsize": mempool_size.get("vsize"),
            "total_fee_sat": mempool_size.get("total_fee"),
        },
    }

    # Заголовки — берём только title/source/published
    news_block = [
        {"ts": n.get("published_at"), "title": n.get("title"), "src": n.get("domain")}
        for n in news[:15]
    ]

    # Crypto-macro (dominance, total mcap, stablecoins trend)
    crypto_macro: dict = {}
    if cg_global:
        mcp = cg_global.get("market_cap_percentage") or {}
        tot = cg_global.get("total_market_cap") or {}
        vol = cg_global.get("total_volume") or {}
        crypto_macro = {
            "btc_dominance_pct": float(mcp.get("btc")) if mcp.get("btc") is not None else None,
            "eth_dominance_pct": float(mcp.get("eth")) if mcp.get("eth") is not None else None,
            "total_mcap_usd": float(tot.get("usd")) if tot.get("usd") is not None else None,
            "total_volume_usd_24h": float(vol.get("usd")) if vol.get("usd") is not None else None,
            "mcap_change_24h_pct": (
                float(cg_global.get("market_cap_change_percentage_24h_usd"))
                if cg_global.get("market_cap_change_percentage_24h_usd") is not None else None
            ),
        }
    stables = {}
    usdt_now = None
    usdc_now = None
    if usdt_chart is not None and not usdt_chart.empty:
        usdt_now = float(usdt_chart["market_cap"].iloc[-1])
    if usdc_chart is not None and not usdc_chart.empty:
        usdc_now = float(usdc_chart["market_cap"].iloc[-1])
    if usdt_chart is not None or usdc_chart is not None:
        total_now = (usdt_now or 0.0) + (usdc_now or 0.0)
        stables = {
            "usdt_mcap_usd": usdt_now,
            "usdt_change_30d_pct": _mcap_trend(usdt_chart, 30),
            "usdt_change_90d_pct": _mcap_trend(usdt_chart, 90),
            "usdc_mcap_usd": usdc_now,
            "usdc_change_30d_pct": _mcap_trend(usdc_chart, 30),
            "usdc_change_90d_pct": _mcap_trend(usdc_chart, 90),
            "total_usd": total_now if total_now > 0 else None,
        }
        # Совокупный 30d тренд: ручной расчёт
        if usdt_chart is not None and usdc_chart is not None and not usdt_chart.empty and not usdc_chart.empty:
            both = (
                usdt_chart.set_index("date")["market_cap"].rename("usdt").to_frame()
                .join(usdc_chart.set_index("date")["market_cap"].rename("usdc"), how="inner")
            )
            both["total"] = both["usdt"] + both["usdc"]
            latest_total = float(both["total"].iloc[-1])
            target = both.index[-1] - pd.Timedelta(days=30)
            prev = both[both.index <= target]
            stables["total_change_30d_pct"] = (
                _pct_change(latest_total, float(prev["total"].iloc[-1])) if not prev.empty else None
            )
    if stables:
        crypto_macro["stablecoins"] = stables

    # Деривативы (без Binance: Bybit + OKX, BTC-perp)
    derivatives = {}
    if bybit_funding:
        # bybit отдаёт fundingRate как строку с десятичной долей (например "0.0001" = 0.01% за 8h)
        latest_fr = float(bybit_funding[0].get("fundingRate", 0))
        prev_fr = float(bybit_funding[1].get("fundingRate", 0)) if len(bybit_funding) > 1 else latest_fr
        derivatives["bybit"] = {
            "funding_rate_8h_pct": latest_fr * 100.0,
            "funding_rate_delta_pct": (latest_fr - prev_fr) * 100.0,
            "funding_history_count": len(bybit_funding),
        }
    if bybit_oi:
        latest_oi = bybit_oi[-1]
        first_oi = bybit_oi[0]
        oi_latest = float(latest_oi.get("openInterest", 0))
        oi_prev = float(first_oi.get("openInterest", 0)) if first_oi else oi_latest
        change = ((oi_latest - oi_prev) / oi_prev * 100.0) if oi_prev else None
        derivatives.setdefault("bybit", {}).update(
            {"open_interest_btc": oi_latest, "oi_change_window_pct": change}
        )
    if okx_funding:
        try:
            fr = float(okx_funding.get("fundingRate", 0))
            derivatives["okx"] = {"funding_rate_8h_pct": fr * 100.0}
        except (TypeError, ValueError):
            pass
    if okx_oi:
        try:
            derivatives.setdefault("okx", {})["open_interest_usd"] = float(okx_oi.get("oiUsd", 0))
            derivatives["okx"]["open_interest_ccy"] = float(okx_oi.get("oiCcy", 0))
        except (TypeError, ValueError):
            pass

    # Корреляции с традиционными рынками
    crypto_log_ret = log_ret.copy()
    correlations = {
        "spx": _corr_windows(crypto_log_ret, spx) if spx is not None else {"30d": None, "90d": None, "n_overlap": 0},
        "dxy": _corr_windows(crypto_log_ret, dxy) if dxy is not None else {"30d": None, "90d": None, "n_overlap": 0},
    }

    # Светофор-эвристика (балльная схема). 0..10, 0 = низкий риск, 10 = максимум.
    score = 0
    factors: list[str] = []

    if rsi14 >= 75:
        score += 2; factors.append(f"RSI14={rsi14:.0f} перегрев сверху")
    elif rsi14 <= 25:
        score += 2; factors.append(f"RSI14={rsi14:.0f} перегрев снизу (oversold)")

    if fng_block:
        fv = fng_block["latest"]
        if fv >= 80:
            score += 2; factors.append(f"F&G={fv} extreme greed")
        elif fv <= 15:
            score += 2; factors.append(f"F&G={fv} extreme fear")
        elif fv >= 70 or fv <= 25:
            score += 1; factors.append(f"F&G={fv} {'greed' if fv >= 70 else 'fear'}")

    if vol_30d_ann > 80:
        score += 2; factors.append(f"vola 30d {vol_30d_ann:.0f}% (очень высокая)")
    elif vol_30d_ann > 55:
        score += 1; factors.append(f"vola 30d {vol_30d_ann:.0f}% (повышенная)")

    if drawdown_from_ath <= -30:
        score += 2; factors.append(f"drawdown от ATH {drawdown_from_ath:.0f}%")
    elif drawdown_from_ath <= -15:
        score += 1; factors.append(f"drawdown от ATH {drawdown_from_ath:.0f}%")

    if pd.notna(sma50) and pd.notna(sma200):
        if sma50 < sma200:
            score += 1; factors.append("SMA50<SMA200 (медвежий режим)")
    if pd.notna(sma200) and latest < float(sma200):
        score += 1; factors.append("цена < SMA200")

    if pos_365d >= 0.9:
        score += 1; factors.append(f"в верху годового диапазона ({pos_365d:.2f})")
    elif pos_365d <= 0.1:
        score += 1; factors.append(f"в дне годового диапазона ({pos_365d:.2f})")

    # Стейблкоины сжимаются — ликвидность уходит
    stables_block = crypto_macro.get("stablecoins") or {}
    tot_30d = stables_block.get("total_change_30d_pct")
    if tot_30d is not None and tot_30d <= -3.0:
        score += 2; factors.append(f"stablecoins total {tot_30d:+.1f}% за 30d (отток ликвидности)")
    elif tot_30d is not None and tot_30d <= -1.0:
        score += 1; factors.append(f"stablecoins total {tot_30d:+.1f}% за 30d")

    # Funding-перегрев (любая сторона)
    bybit_block = derivatives.get("bybit") or {}
    fr = bybit_block.get("funding_rate_8h_pct")
    if fr is not None:
        if fr >= 0.05:
            score += 2; factors.append(f"Bybit funding {fr:+.3f}%/8h (extreme greed на perp)")
        elif fr <= -0.05:
            score += 2; factors.append(f"Bybit funding {fr:+.3f}%/8h (extreme short)")

    score = min(score, 10)
    if score <= 2:
        verdict = "low"
    elif score <= 5:
        verdict = "medium"
    else:
        verdict = "high"

    traffic_light = {
        "level": verdict,
        "score_0_10": score,
        "factors": factors,
        "note": "Эвристика на основе технических метрик. Не учитывает события/новости/макро — это работа человека или LLM-чата с этим JSON.",
    }

    return {
        "as_of_utc": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        "symbol": symbol,
        "data_through_utc": latest_date.isoformat() if hasattr(latest_date, "isoformat") else str(latest_date),
        "price_usd": latest,
        "risk_traffic_light": traffic_light,
        "risk_quantiles": risk_quantiles,
        "volatility_term": vola_term,
        "returns_pct": {
            "1d": ret_24h, "7d": ret_7d, "30d": ret_30d, "90d": ret_90d, "365d": ret_365d,
        },
        "volatility_30d_annualized_pct": vol_30d_ann,
        "rsi14": rsi14,
        "sma": {
            "sma50": float(sma50) if pd.notna(sma50) else None,
            "sma200": float(sma200) if pd.notna(sma200) else None,
            "price_above_sma50": bool(latest > sma50) if pd.notna(sma50) else None,
            "price_above_sma200": bool(latest > sma200) if pd.notna(sma200) else None,
            "sma50_above_sma200": bool(sma50 > sma200) if pd.notna(sma50) and pd.notna(sma200) else None,
        },
        "range_position": {
            "30d": pos_30d, "90d": pos_90d, "365d": pos_365d,
        },
        "ath_2y": {
            "price": ath,
            "date": ath_date.isoformat() if hasattr(ath_date, "isoformat") else str(ath_date),
            "drawdown_from_ath_pct": drawdown_from_ath,
        },
        "max_drawdown_2y": {"pct": mdd_pct, "duration_days": mdd_days},
        "volume": {
            "latest_usd": vol_latest,
            "avg_30d_usd": vol_avg_30d,
            "ratio_to_30d": vol_ratio_30d,
        },
        "fear_greed": fng_block,
        "onchain": onchain,
        "mempool": mempool,
        "crypto_macro": crypto_macro,
        "derivatives": derivatives,
        "correlations": correlations,
        "news_recent": news_block,
    }
