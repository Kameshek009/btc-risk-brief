"""Сравнение текущего прогона с последней записью в logs/risk_brief.jsonl.

Цель — короткий человеко-читаемый «changes since previous», когда скрипт
запускается по cron / --interval, и большой смысл следить именно за дельтой,
а не за абсолютными значениями.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional


def previous_record(jsonl_path: Path, symbol: str) -> Optional[dict]:
    """Вернуть последнюю валидную запись по символу (или None)."""
    if not jsonl_path.exists():
        return None
    last: Optional[dict] = None
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
            last = rec
    return last


def _get(d: dict, path: str, default: Any = None) -> Any:
    cur: Any = d
    for part in path.split("."):
        if not isinstance(cur, dict):
            return default
        cur = cur.get(part, default)
    return cur


def _fmt_delta(prev_val: Any, cur_val: Any, pct: bool = False, digits: int = 2) -> Optional[str]:
    try:
        prev_f = float(prev_val)
        cur_f = float(cur_val)
    except (TypeError, ValueError):
        return None
    delta = cur_f - prev_f
    if abs(delta) < 10 ** (-digits):
        return None
    arrow = "↑" if delta > 0 else "↓"
    suffix = "%" if pct else ""
    return f"{prev_f:.{digits}f}{suffix} {arrow} {cur_f:.{digits}f}{suffix} (Δ {delta:+.{digits}f}{suffix})"


KEY_FIELDS = [
    # (label, path, pct-формат, digits)
    ("score", "risk_traffic_light.score_0_10", False, 0),
    ("price", "price_usd", False, 2),
    ("drawdown_from_ATH", "ath_2y.drawdown_from_ath_pct", True, 2),
    ("RSI14", "rsi14", False, 1),
    ("vola_30d_ann", "volatility_30d_annualized_pct", True, 1),
    ("F&G", "fear_greed.latest", False, 0),
    ("BTC.dom", "crypto_macro.btc_dominance_pct", True, 2),
    ("stables_30d", "crypto_macro.stablecoins.total_change_30d_pct", True, 2),
    ("Bybit_funding", "derivatives.bybit.funding_rate_8h_pct", True, 4),
    ("corr_SPX_90d", "correlations.spx.90d", False, 2),
]


def diff_block(prev: Optional[dict], cur: dict) -> dict:
    """Вернуть структурированный diff. Если prev=None, возвращает {}."""
    if not prev:
        return {}
    out: dict = {
        "previous_as_of": prev.get("as_of_utc"),
        "current_as_of": cur.get("as_of_utc"),
        "changes": [],
    }
    for label, path, pct, digits in KEY_FIELDS:
        line = _fmt_delta(_get(prev, path), _get(cur, path), pct=pct, digits=digits)
        if line:
            out["changes"].append(f"{label}: {line}")

    # уровень светофора
    prev_lvl = _get(prev, "risk_traffic_light.level")
    cur_lvl = _get(cur, "risk_traffic_light.level")
    if prev_lvl != cur_lvl and prev_lvl and cur_lvl:
        out["changes"].insert(0, f"level: {prev_lvl.upper()} → {cur_lvl.upper()}")

    # факторы: что появилось / что исчезло
    prev_factors = set(_get(prev, "risk_traffic_light.factors") or [])
    cur_factors = set(_get(cur, "risk_traffic_light.factors") or [])
    added = sorted(cur_factors - prev_factors)
    removed = sorted(prev_factors - cur_factors)
    if added:
        out["new_factors"] = added
    if removed:
        out["resolved_factors"] = removed
    return out


def render_diff_text(diff: dict) -> str:
    if not diff or not (diff.get("changes") or diff.get("new_factors") or diff.get("resolved_factors")):
        return ""
    lines = [f"Changes since previous ({diff.get('previous_as_of','?')[:19]} UTC):"]
    for c in diff.get("changes") or []:
        lines.append(f"  · {c}")
    for f in diff.get("new_factors") or []:
        lines.append(f"  + factor: {f}")
    for f in diff.get("resolved_factors") or []:
        lines.append(f"  - factor cleared: {f}")
    return "\n".join(lines)
