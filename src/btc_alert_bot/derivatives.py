"""Derivatives-context fetchers: liquidations, positioning, funding aggregates.

These are *amplifiers* rather than *causes*: a wave of liquidation cascades
or extreme positioning explains how a fundamental trigger got magnified
into a sharp price move. The summarizer is told to phrase them that way.

All sources here are public + free + unauthenticated:
- OKX: /api/v5/public/liquidation-orders   (recent BTC-USDT-SWAP liquidations)
- Bitfinex: /v2/stats1/...                  (long/short position size)
- Bybit: /v5/market/funding/history         (current funding context)
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import requests

log = logging.getLogger(__name__)

OKX_LIQ_URL = "https://www.okx.com/api/v5/public/liquidation-orders"
BITFINEX_LONG_URL = (
    "https://api-pub.bitfinex.com/v2/stats1/"
    "pos.size:1m:tBTCF0:USTF0:long/last"
)
BITFINEX_SHORT_URL = (
    "https://api-pub.bitfinex.com/v2/stats1/"
    "pos.size:1m:tBTCF0:USTF0:short/last"
)
BYBIT_FUNDING_URL = "https://api.bybit.com/v5/market/funding/history"

DERIVATIVES_TIMEOUT = 5

# Window we treat as "this spike's liquidation context".
LIQ_LOOKBACK_MIN = 15

_session = requests.Session()
_session.headers.update({"User-Agent": "btc-alert-bot/0.2"})


# ---------------------------------------------------------------------------
# OKX liquidations
# ---------------------------------------------------------------------------

def fetch_okx_liquidations() -> dict | None:
    """Recent BTC-USDT-SWAP liquidations from OKX, aggregated.

    Returns a dict with totals (USD) on the long-side vs short-side over
    the lookback window, or None if the call fails. The numbers themselves
    aren't a *cause* of the spike — they're an amplifier signal.
    """
    try:
        resp = _session.get(
            OKX_LIQ_URL,
            params={
                "instType": "SWAP",
                "instFamily": "BTC-USDT",
                "state": "filled",
                "limit": "100",
            },
            timeout=DERIVATIVES_TIMEOUT,
        )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("code") not in ("0", 0, None):
            return None
    except Exception as e:
        log.warning("OKX liquidation fetch failed: %s", e)
        return None

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=LIQ_LOOKBACK_MIN)
    long_usd = 0.0   # long positions force-closed (= price went down)
    short_usd = 0.0  # short positions force-closed (= price went up)
    n_long = n_short = 0
    largest = 0.0

    # OKX returns nested: data[0].details = [{ts, side, sz, bkPx, ...}]
    for outer in payload.get("data", []) or []:
        for d in outer.get("details", []) or []:
            try:
                ts_ms = int(d.get("ts", 0))
                ts = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
            except Exception:
                continue
            if ts < cutoff:
                continue
            try:
                size = float(d.get("sz", 0))      # contracts
                price = float(d.get("bkPx", 0))   # bankrupt price
            except Exception:
                continue
            # OKX BTC-USDT-SWAP: 1 contract = 0.01 BTC.
            usd = size * 0.01 * price
            largest = max(largest, usd)
            side = d.get("side")
            if side == "buy":  # short being closed (price moved up)
                short_usd += usd
                n_short += 1
            elif side == "sell":  # long being closed (price moved down)
                long_usd += usd
                n_long += 1

    if not (long_usd or short_usd):
        return None
    return {
        "long_liq_usd": long_usd,
        "short_liq_usd": short_usd,
        "long_count": n_long,
        "short_count": n_short,
        "largest_usd": largest,
        "lookback_min": LIQ_LOOKBACK_MIN,
    }


# ---------------------------------------------------------------------------
# Bitfinex BTC long/short ratio
# ---------------------------------------------------------------------------

def fetch_bitfinex_positioning() -> dict | None:
    """Bitfinex futures BTC long vs short total open size (in BTC).

    A heavily skewed ratio + spike-direction often correlates with
    "crowd is offside" → mean-reversion or cascade risk.
    """
    try:
        long_resp = _session.get(BITFINEX_LONG_URL, timeout=DERIVATIVES_TIMEOUT)
        short_resp = _session.get(BITFINEX_SHORT_URL, timeout=DERIVATIVES_TIMEOUT)
        long_resp.raise_for_status()
        short_resp.raise_for_status()
        long_size = float(long_resp.json()[1])
        short_size = float(short_resp.json()[1])
    except Exception as e:
        log.warning("Bitfinex positioning fetch failed: %s", e)
        return None

    total = long_size + short_size
    if total <= 0:
        return None
    long_pct = long_size / total * 100
    return {
        "long_btc": long_size,
        "short_btc": short_size,
        "long_pct": long_pct,
        "short_pct": 100 - long_pct,
    }


# ---------------------------------------------------------------------------
# Bybit current funding
# ---------------------------------------------------------------------------

def fetch_bybit_funding() -> dict | None:
    """Latest BTC perpetual funding rate from Bybit."""
    try:
        resp = _session.get(
            BYBIT_FUNDING_URL,
            params={"category": "linear", "symbol": "BTCUSDT", "limit": 1},
            timeout=DERIVATIVES_TIMEOUT,
        )
        resp.raise_for_status()
        rates = resp.json().get("result", {}).get("list", [])
        if not rates:
            return None
        rate_pct = float(rates[0]["fundingRate"]) * 100
        annualized = rate_pct * 3 * 365
        return {"rate_pct_8h": rate_pct, "annualized_pct": annualized}
    except Exception as e:
        log.warning("Bybit funding fetch failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Aggregate factor for the analyzers pipeline
# ---------------------------------------------------------------------------

def fetch_derivatives_context() -> list[dict]:
    """Combined derivatives factor set.

    Returns up to 3 factor entries:
    - ``derivatives_liq``  : OKX liquidation summary (only when activity)
    - ``derivatives_pos``  : Bitfinex L/S positioning (only when skewed)
    - ``derivatives_fund`` : Bybit funding (only when not-flat)
    """
    out: list[dict] = []

    liq = fetch_okx_liquidations()
    if liq:
        net = liq["long_liq_usd"] - liq["short_liq_usd"]
        bigger_side = "long" if net > 0 else "short" if net < 0 else "balanced"
        title = (
            f"OKX 清算 ({liq['lookback_min']}min): "
            f"long ${liq['long_liq_usd']/1e6:.1f}M / short ${liq['short_liq_usd']/1e6:.1f}M, "
            f"largest ${liq['largest_usd']/1e6:.2f}M "
            f"(net {bigger_side})"
        )
        # Direction hint: long liquidations correlate with downside continuation.
        direction_hint = (
            "down" if net > 0 else "up" if net < 0 else None
        )
        out.append({
            "type": "derivatives_liq",
            "source": "OKX",
            "title": title,
            "url": "https://www.okx.com/trade-swap/btc-usdt-swap",
            "tags": ["liquidation", "amplifier"],
            "direction_hint": direction_hint,
            "magnitude_usd": liq["long_liq_usd"] + liq["short_liq_usd"],
        })

    pos = fetch_bitfinex_positioning()
    if pos and abs(pos["long_pct"] - 50) >= 5:  # only if visibly skewed
        skew_word = (
            "ロング過剰" if pos["long_pct"] > 55 else
            "ショート過剰" if pos["long_pct"] < 45 else
            "中立"
        )
        out.append({
            "type": "derivatives_pos",
            "source": "Bitfinex",
            "title": (
                f"Bitfinex BTC ポジション: "
                f"long {pos['long_btc']:,.0f} / short {pos['short_btc']:,.0f} "
                f"({pos['long_pct']:.0f}% long, {skew_word})"
            ),
            "url": "https://www.bitfinex.com/stats",
            "tags": ["positioning", "amplifier"],
        })

    fund = fetch_bybit_funding()
    # Funding is "interesting" only when meaningfully non-zero.
    if fund and abs(fund["rate_pct_8h"]) >= 0.005:
        out.append({
            "type": "derivatives_fund",
            "source": "Bybit",
            "title": (
                f"BTC Perp Funding: {fund['rate_pct_8h']:+.4f}%/8h "
                f"(年率換算 {fund['annualized_pct']:+.1f}%)"
            ),
            "url": "https://www.bybit.com/trade/usdt/BTCUSDT",
            "tags": ["funding"],
        })

    return out
