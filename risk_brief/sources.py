"""Загрузчики из бесплатных публичных источников.

Все функции возвращают pandas-структуры или dict. Сетевые ошибки бросаются
наружу — обработка в скрипте.
"""

from __future__ import annotations

import io
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import requests


HTTP_TIMEOUT = 30
USER_AGENT = "marci-risk-brief/1.0 (+local)"
DEFAULT_HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json"}


def _get_json(url: str, params: Optional[dict] = None, retries: int = 3) -> dict:
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            resp = requests.get(
                url, params=params, headers=DEFAULT_HEADERS, timeout=HTTP_TIMEOUT
            )
            if resp.status_code == 429:
                time.sleep(2 ** attempt * 2)
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            last_exc = exc
            time.sleep(2 ** attempt)
    raise RuntimeError(f"GET {url} failed after {retries} attempts: {last_exc}")


def _cache_get(cache_dir: Path, key: str, ttl_seconds: int) -> Optional[dict]:
    path = cache_dir / f"{key}.json"
    if not path.exists():
        return None
    age = time.time() - path.stat().st_mtime
    if age > ttl_seconds:
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


def _cache_put(cache_dir: Path, key: str, payload: dict) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / f"{key}.json").write_text(json.dumps(payload, default=str))


def fetch_crypto_history(cache_dir: Path, symbol: str = "BTC", days: int = 730) -> pd.DataFrame:
    """Daily история любого тикера из CryptoCompare (без ключа, до 2000 точек).

    Возвращает колонки: date (UTC, день), price (close USD), high, low, open, volume.
    """
    symbol = symbol.upper()
    cache_key = f"cryptocompare_{symbol.lower()}_{days}"
    cached = _cache_get(cache_dir, cache_key, ttl_seconds=6 * 3600)
    if cached is None:
        cached = _get_json(
            "https://min-api.cryptocompare.com/data/v2/histoday",
            params={"fsym": symbol, "tsym": "USD", "limit": str(days)},
        )
        _cache_put(cache_dir, cache_key, cached)

    rows = ((cached or {}).get("Data") or {}).get("Data") or []
    if not rows:
        raise RuntimeError(f"cryptocompare returned no rows for {symbol}: {cached}")
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["time"], unit="s", utc=True).dt.floor("D")
    df = df.rename(columns={"close": "price", "volumeto": "volume"})
    df = df[["date", "price", "high", "low", "open", "volume"]]
    df = df.sort_values("date").reset_index(drop=True)
    return df


# Backwards-совместимый alias на случай старых вызовов
def fetch_btc_history(cache_dir: Path, days: int = 730) -> pd.DataFrame:
    return fetch_crypto_history(cache_dir, "BTC", days)


def fetch_yahoo_daily(cache_dir: Path, ticker: str, days: int = 730) -> pd.DataFrame:
    """Daily-серия с Yahoo Finance chart API (без ключа).

    Тикеры: `^GSPC` (S&P 500), `DX-Y.NYB` (DXY), `GC=F` (gold), `^VIX`, etc.
    Возвращает: date (UTC), close.
    """
    range_arg = "2y" if days <= 730 else ("5y" if days <= 1825 else "max")
    cache_key = f"yahoo_{ticker.replace('^','').replace('=','').replace('-','').replace('.','').lower()}_{range_arg}"
    cached = _cache_get(cache_dir, cache_key, ttl_seconds=12 * 3600)
    if cached is None:
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        }
        try:
            resp = requests.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
                params={"range": range_arg, "interval": "1d"},
                headers=headers,
                timeout=HTTP_TIMEOUT,
            )
            resp.raise_for_status()
            cached = resp.json()
        except (requests.RequestException, ValueError) as exc:
            raise RuntimeError(f"yahoo {ticker} fetch failed: {exc}")
        _cache_put(cache_dir, cache_key, cached)

    results = ((cached or {}).get("chart") or {}).get("result") or []
    if not results:
        return pd.DataFrame(columns=["date", "close"])
    r0 = results[0]
    timestamps = r0.get("timestamp") or []
    quote = ((r0.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []
    if not timestamps or not closes:
        return pd.DataFrame(columns=["date", "close"])
    df = pd.DataFrame({"date": pd.to_datetime(timestamps, unit="s", utc=True), "close": closes})
    df = df.dropna(subset=["close"]).sort_values("date")
    df["date"] = df["date"].dt.floor("D")
    return df.tail(days + 1).reset_index(drop=True)


def fetch_fear_greed(cache_dir: Path, limit: int = 730) -> pd.DataFrame:
    cached = _cache_get(cache_dir, "fng", ttl_seconds=6 * 3600)
    if cached is None:
        cached = _get_json(
            "https://api.alternative.me/fng/", params={"limit": str(limit)}
        )
        _cache_put(cache_dir, "fng", cached)
    rows = []
    for r in cached.get("data", []):
        rows.append(
            {
                "date": datetime.fromtimestamp(int(r["timestamp"]), tz=timezone.utc).date(),
                "fng_value": int(r["value"]),
                "fng_class": r.get("value_classification", ""),
            }
        )
    df = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
    df["date"] = pd.to_datetime(df["date"], utc=True)
    return df


def fetch_blockchain_chart(cache_dir: Path, chart: str, timespan: str = "2years") -> pd.DataFrame:
    """`chart` ∈ {hash-rate, difficulty, n-transactions, miners-revenue, ...}."""
    cached = _cache_get(cache_dir, f"bc_{chart}", ttl_seconds=12 * 3600)
    if cached is None:
        cached = _get_json(
            f"https://api.blockchain.info/charts/{chart}",
            params={"timespan": timespan, "format": "json", "sampled": "true"},
        )
        _cache_put(cache_dir, f"bc_{chart}", cached)
    values = cached.get("values", [])
    if not values:
        return pd.DataFrame(columns=["date", "value"])
    df = pd.DataFrame(values).rename(columns={"x": "ts", "y": "value"})
    df["date"] = pd.to_datetime(df["ts"], unit="s", utc=True)
    return df[["date", "value"]].sort_values("date").reset_index(drop=True)


def fetch_mempool_fees() -> dict:
    """Текущие рекомендуемые комиссии BTC (sat/vB). Не кэшируем — это «сейчас»."""
    return _get_json("https://mempool.space/api/v1/fees/recommended")


def fetch_mempool_size() -> dict:
    """Размер мемпула сейчас: count, vsize, total_fee."""
    return _get_json("https://mempool.space/api/mempool")


def fetch_coingecko_global(cache_dir: Path) -> dict:
    """`/api/v3/global` — снимок крипто-макро (dominance, total mcap)."""
    cached = _cache_get(cache_dir, "cg_global", ttl_seconds=2 * 3600)
    if cached is None:
        cached = _get_json("https://api.coingecko.com/api/v3/global")
        _cache_put(cache_dir, "cg_global", cached)
    return cached.get("data") or {}


def fetch_coingecko_market_chart(cache_dir: Path, coin_id: str, days: int = 365) -> pd.DataFrame:
    """CoinGecko market_chart для конкретной монеты (анонимно макс ~365 дней).

    Возвращает: date (UTC, day), price, market_cap, volume.
    """
    days_arg = min(days, 365)
    cache_key = f"cg_chart_{coin_id}_{days_arg}"
    cached = _cache_get(cache_dir, cache_key, ttl_seconds=12 * 3600)
    if cached is None:
        cached = _get_json(
            f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart",
            params={"vs_currency": "usd", "days": str(days_arg), "interval": "daily"},
        )
        _cache_put(cache_dir, cache_key, cached)
    prices = pd.DataFrame(cached.get("prices") or [], columns=["ts_ms", "price"])
    caps = pd.DataFrame(cached.get("market_caps") or [], columns=["ts_ms", "market_cap"])
    vols = pd.DataFrame(cached.get("total_volumes") or [], columns=["ts_ms", "volume"])
    if prices.empty:
        return pd.DataFrame(columns=["date", "price", "market_cap", "volume"])
    df = prices.merge(caps, on="ts_ms", how="left").merge(vols, on="ts_ms", how="left")
    df["date"] = pd.to_datetime(df["ts_ms"], unit="ms", utc=True).dt.floor("D")
    return df.drop(columns=["ts_ms"]).sort_values("date").reset_index(drop=True)


def fetch_bybit_funding(symbol: str = "BTCUSDT", limit: int = 20) -> list[dict]:
    """Публичный Bybit v5 funding-history. Без ключей."""
    try:
        payload = _get_json(
            "https://api.bybit.com/v5/market/funding/history",
            params={"category": "linear", "symbol": symbol, "limit": str(limit)},
        )
    except RuntimeError:
        return []
    return ((payload or {}).get("result") or {}).get("list") or []


def fetch_bybit_oi(symbol: str = "BTCUSDT", interval: str = "1h", limit: int = 24) -> list[dict]:
    try:
        payload = _get_json(
            "https://api.bybit.com/v5/market/open-interest",
            params={"category": "linear", "symbol": symbol, "intervalTime": interval, "limit": str(limit)},
        )
    except RuntimeError:
        return []
    return ((payload or {}).get("result") or {}).get("list") or []


def fetch_okx_funding(inst_id: str = "BTC-USDT-SWAP") -> dict:
    try:
        payload = _get_json(
            "https://www.okx.com/api/v5/public/funding-rate",
            params={"instId": inst_id},
        )
    except RuntimeError:
        return {}
    data = (payload or {}).get("data") or []
    return data[0] if data else {}


def fetch_okx_oi(inst_id: str = "BTC-USDT-SWAP") -> dict:
    try:
        payload = _get_json(
            "https://www.okx.com/api/v5/public/open-interest",
            params={"instType": "SWAP", "instId": inst_id},
        )
    except RuntimeError:
        return {}
    data = (payload or {}).get("data") or []
    return data[0] if data else {}


def fetch_cryptopanic_recent(token: str, symbol: str = "BTC", limit: int = 30) -> list[dict]:
    """Опционально: свежие новости по тикеру. Без токена возвращает пустой список."""
    if not token:
        return []
    try:
        payload = _get_json(
            "https://cryptopanic.com/api/free/v1/posts/",
            params={"auth_token": token, "currencies": symbol.upper(), "public": "true"},
        )
    except RuntimeError:
        return []
    results = payload.get("results", [])[:limit]
    out = []
    for r in results:
        out.append(
            {
                "published_at": r.get("published_at"),
                "title": (r.get("title") or "").strip(),
                "domain": r.get("domain", ""),
                "url": r.get("url", ""),
            }
        )
    return out
