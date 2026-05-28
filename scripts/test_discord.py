"""Discord-only end-to-end test.

Forces a fake spike (so detection always fires) and runs the full pipeline:
real price fetch → real factor analysis → real Gemini summary → Discord post.
X posting is skipped to preserve the 500/month Free tier quota.

Usage:
    pip install -e .
    python scripts/test_discord.py
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

# Allow running from project root without install: prepend src/ to path.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

# CRITICAL: load_dotenv() MUST run before importing any module that
# captures env vars at load time (jp_translator.config reads
# GEMINI_API_KEY/XAI_API_KEY/etc. on first import via summarizer).
from dotenv import load_dotenv

load_dotenv()

from btc_alert_bot.analyzers import gather_factors  # noqa: E402
from btc_alert_bot.chart import render_chart  # noqa: E402
from btc_alert_bot.features import compute_market_features  # noqa: E402
from btc_alert_bot.history import find_similar_alerts, record_alert  # noqa: E402
from btc_alert_bot.market import fetch_market_snapshot, fetch_window_ohlcv  # noqa: E402
from btc_alert_bot.price import fetch_btc_price  # noqa: E402
from btc_alert_bot.publishers import post_discord, post_x  # noqa: E402
from btc_alert_bot.summarizer import summarize  # noqa: E402
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("test_discord")


def main() -> int:
    log.info("=== Discord Test Run ===")

    # 1. Real price.
    price_data = fetch_btc_price()
    log.info(
        "BTC ${:,.2f} | 1h {:+.2f}% | 24h {:+.2f}%".format(
            price_data["price_usd"],
            price_data["change_1h"],
            price_data["change_24h"],
        )
    )

    # 2. Real Bybit features (so the summary uses live observations, not mocks).
    log.info("Fetching Bybit market snapshot...")
    try:
        snapshot = fetch_market_snapshot()
        features = compute_market_features(snapshot)
    except Exception as e:
        log.warning("Bybit fetch failed: %s — features will be empty", e)
        features = {}

    # 3. Force a spike. Prefer the actual recent movement so the test alert
    # represents reality (especially important when ALLOW_TEST_X_POST=true).
    # Cascade: 15m → 1h → fabricated +2.5%.
    real_change = features.get("return_15m", 0.0) if features else 0.0
    if abs(real_change) < 0.5:
        # Fall back to 1h return — slow-grind sell-offs look small at 15m
        # but obvious at 1h. Better for a realistic test than fabrication.
        real_change = features.get("return_1h", 0.0) if features else price_data.get("change_1h", 0.0)
    if abs(real_change) < 0.5:
        log.info(
            "Real movement tiny (15m=%.2f%%, 1h=%.2f%%) — fabricating +2.5%% for test",
            (features or {}).get("return_15m", 0.0),
            (features or {}).get("return_1h", 0.0),
        )
        forced_change = 2.5
    else:
        forced_change = real_change
        log.info("Using real movement: %+.2f%% for test", forced_change)
    # Use a short-tier window so test runs don't appear as "15m" alerts
    # in the production Discord (15m is intentionally rare per spec).
    spike = {
        "window": "5m",
        "change": forced_change,
        "direction": "up" if forced_change > 0 else "down",
        "score": 3.1,
        "reasons": [
            "[forced for test]",
            f"15m return {forced_change:+.2f}%",
            f"ATR%={features.get('atr_pct', 0):.3f}" if features else "no ATR data",
            f"OI Δ1h={features.get('oi_change_1h_pct', 0):+.2f}%" if features else "no OI data",
        ],
        "features": features,
    }
    log.info("Forced spike: %+.2f%% / %s (score=%s)", spike["change"], spike["window"], spike["score"])

    # 3. Real factor analysis.
    log.info("Gathering factors (parallel)...")
    factors = gather_factors(spike)
    log.info("Got %d factors:", len(factors))
    for f in factors[:5]:
        log.info("  - [%s/%s] %s", f["type"], f["source"], f["title"][:80])

    # 4a. Look up similar past alerts (Phase 2.5).
    similar = find_similar_alerts(ROOT / "data" / "history.sqlite", spike, limit=3)
    if similar:
        log.info(
            "Similar past alerts: %s",
            ", ".join(f"#{s['id']}({s['change_pct']:+.2f}%)" for s in similar),
        )

    # 4. Real Gemini summary.
    log.info("Calling Gemini...")
    summary = summarize(price_data, spike, factors, similar_alerts=similar)
    log.info("Summary:\n%s", summary)

    # 5. Render chart PNG.
    log.info("Rendering chart...")
    try:
        chart_png = render_chart(spike, price_data)
        log.info("Chart: %d KB", len(chart_png) // 1024)
    except Exception as e:
        log.warning("Chart render failed: %s — text only", e)
        chart_png = None

    # 6a. Window OHLCV for the embed enrichment.
    window_ohlcv = fetch_window_ohlcv(
        spike["window"],
        anchor_ts=(features or {}).get("ts") or price_data.get("timestamp"),
    )

    # 6. Discord post.
    log.info("Posting to Discord...")
    delivered_discord = post_discord(
        summary, price_data, spike,
        chart_png=chart_png, window_ohlcv=window_ohlcv,
    )

    # 6b. X posting policy: BY DEFAULT skipped — test posts use a fabricated
    #     spike and posting that to real X followers is misleading.
    #     One-time override: set env ALLOW_TEST_X_POST=true. Each run of
    #     this script with the override consumes ~1 of the 500/month X
    #     Free-tier quota. Use sparingly and only when the user has
    #     explicitly authorized it ("Xにも投稿（特例）").
    if os.getenv("ALLOW_TEST_X_POST", "").lower() in ("1", "true", "yes"):
        log.warning(
            "ALLOW_TEST_X_POST=true — posting test to X (real audience!)"
        )
        delivered_x = post_x(summary, price_data, spike, chart_png=chart_png)
        log.info("X post result: delivered=%s", delivered_x)
    else:
        delivered_x = False
        log.info(
            "X post skipped (default Discord-only; set ALLOW_TEST_X_POST=true to override)"
        )

    # 7. Record to history DB (the test_discord pipeline mirrors main.py).
    alert_id = record_alert(
        ROOT / "data" / "history.sqlite",
        price_data=price_data,
        spike=spike,
        factors=factors,
        summary=summary,
        delivered_discord=delivered_discord,
        delivered_x=delivered_x,
    )
    log.info("Recorded test alert: id=%s", alert_id)

    log.info("=== Test Complete ===")
    log.info("Check Discord channel for the alert message.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
