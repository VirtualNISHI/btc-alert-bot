"""Phase 3: WebSocket-based realtime alert engine.

Replaces the 5min GitHub Actions cron with a long-running asyncio
process. The detection / analysis / posting pipeline used by ``main.py``
is reused unchanged — only the *trigger* differs:

    main.py     : GitHub cron fires every 5min, fetches everything, decides.
    realtime.py : OKX WebSocket pushes each candle close, decision happens
                  in-process within seconds of the bar closing.

Designed to run on Oracle Cloud Always Free (Ampere ARM, 4 cores / 24GB,
permanent free tier) as a docker-compose service. See DEPLOYMENT.md.

Why we still use REST for snapshots even in WS mode:
- WS gives us *one* timely trigger (the 5m close) but features.py also
  needs the rolling kline history, OI, and funding for its z-scores.
  Re-fetching via REST keeps the code path identical to main.py and
  avoids maintaining a parallel in-memory orderbook / OI tracker.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from pathlib import Path

import websockets
from dotenv import load_dotenv

from .analyzers import gather_factors
from .chart import render_chart
from .detector import (
    SpikeDetector,
    append_feature_history,
    load_state,
    save_state,
)
from .features import compute_market_features
from .history import find_similar_alerts, record_alert
from .market import fetch_market_snapshot
from .price import fetch_btc_price
from .publishers import post_discord, post_x
from .summarizer import summarize

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("btc_alert_bot.realtime")

# OKX has separate WS domains: /public for tickers/funding/mark-price,
# /business for candles + trades. We need candle5m → /business.
OKX_WS_URL = "wss://ws.okx.com:8443/ws/v5/business"
INST_ID = "BTC-USDT-SWAP"

STATE_PATH = Path("data/state.json")
HISTORY_DB_PATH = Path("data/history.sqlite")

# Reconnection backoff bounds.
RECONNECT_INITIAL_DELAY = 1.0
RECONNECT_MAX_DELAY = 60.0

# OKX docs say the server cuts idle connections after 30s. We send a
# plain "ping" string (NOT JSON) every PING_INTERVAL of silence.
PING_INTERVAL = 25.0

# Hard upper bound for one detection pass. If a downstream call (Gemini,
# Discord, RSS, etc.) hangs longer than this, we cancel and let the next
# candle have a fresh attempt.
DETECTION_TIMEOUT_S = 120.0

# Watchdog: if the WS loop hasn't received *any* frame (including pongs)
# for this long, we self-exit so the container's restart policy can heal
# the process. Docker-compose's healthcheck alone won't restart on
# unhealthy state with `restart: unless-stopped`.
WATCHDOG_STALL_S = 1800.0
WATCHDOG_POLL_S = 60.0


class RealtimeBot:
    """Long-running OKX WS listener that triggers the alert pipeline."""

    def __init__(self) -> None:
        self.shutdown = asyncio.Event()
        # Single in-flight detection at a time — fire-and-forget so we
        # never block the WS receive/keepalive loop.
        self._detection_task: asyncio.Task | None = None
        # Watchdog liveness counter (monotonic seconds).
        self.last_activity = time.monotonic()

    # ------------------------------------------------------------------
    # Reconnect loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        watchdog_task = asyncio.create_task(self._watchdog())
        try:
            delay = RECONNECT_INITIAL_DELAY
            while not self.shutdown.is_set():
                try:
                    async with websockets.connect(
                        OKX_WS_URL,
                        ping_interval=None,  # we manage keepalive manually
                        close_timeout=5,
                    ) as ws:
                        log.info("WS connected to %s", OKX_WS_URL)
                        delay = RECONNECT_INITIAL_DELAY  # reset on success
                        self.last_activity = time.monotonic()
                        await ws.send(json.dumps({
                            "op": "subscribe",
                            "args": [
                                {"channel": "candle5m", "instId": INST_ID},
                            ],
                        }))
                        await self._listen(ws)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    log.warning(
                        "WS error: %s — reconnecting in %.1fs", e, delay
                    )
                    try:
                        await asyncio.wait_for(
                            self.shutdown.wait(), timeout=delay
                        )
                        break  # shutdown flagged during sleep
                    except asyncio.TimeoutError:
                        pass
                    delay = min(delay * 2, RECONNECT_MAX_DELAY)
        finally:
            watchdog_task.cancel()
            # Allow the in-flight detection (if any) to finish before exit.
            if self._detection_task and not self._detection_task.done():
                try:
                    await asyncio.wait_for(self._detection_task, timeout=10)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
        log.info("Shutdown complete.")

    async def _watchdog(self) -> None:
        """Self-exit if the WS loop has been silent too long.

        Compose's `restart: unless-stopped` only acts on process exit, not
        on healthcheck failure. By exiting the process when stuck, we let
        the restart policy bring us back automatically.
        """
        while not self.shutdown.is_set():
            try:
                await asyncio.sleep(WATCHDOG_POLL_S)
            except asyncio.CancelledError:
                return
            elapsed = time.monotonic() - self.last_activity
            if elapsed > WATCHDOG_STALL_S:
                log.error(
                    "Watchdog: no WS activity for %.0fs (>%.0fs); exiting "
                    "so the container restart policy can recover.",
                    elapsed, WATCHDOG_STALL_S,
                )
                # exit code 2 makes intent obvious in container logs.
                os._exit(2)

    # ------------------------------------------------------------------
    # Per-connection message loop
    # ------------------------------------------------------------------

    async def _listen(self, ws) -> None:
        while not self.shutdown.is_set():
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=PING_INTERVAL)
            except asyncio.TimeoutError:
                await ws.send("ping")
                log.debug("Sent keepalive ping")
                self.last_activity = time.monotonic()  # send-side activity
                continue
            self.last_activity = time.monotonic()
            await self._handle_message(msg)

    async def _handle_message(self, msg: str) -> None:
        if msg == "pong":
            return
        try:
            data = json.loads(msg)
        except json.JSONDecodeError:
            log.warning("Non-JSON WS frame: %r", msg[:120])
            return

        # Subscription ack / errors arrive with an "event" field.
        if "event" in data:
            log.info("WS event: %s", data)
            return

        arg = data.get("arg") or {}
        if arg.get("channel") != "candle5m":
            return
        rows = data.get("data") or []
        if not rows:
            return
        latest = rows[0]  # [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
        confirmed = len(latest) > 8 and latest[8] == "1"
        if not confirmed:
            return  # mid-bar update — wait for the close

        log.info("5m candle closed: ts=%s close=%s", latest[0], latest[4])
        # Fire-and-forget so the WS loop keeps receiving frames and
        # sending keepalives even while detection is in flight. If a
        # previous detection is still running, skip this candle — the
        # next close gets fresh state anyway.
        if self._detection_task and not self._detection_task.done():
            log.warning(
                "Previous detection still running; skipping this candle"
            )
            return
        self._detection_task = asyncio.create_task(self._run_detection_async())

    # ------------------------------------------------------------------
    # Detection pipeline (mirrors main.py, sync)
    # ------------------------------------------------------------------

    async def _run_detection_async(self) -> None:
        """Run the sync pipeline in a thread, bounded by DETECTION_TIMEOUT_S."""
        try:
            await asyncio.wait_for(
                asyncio.to_thread(self._run_detection),
                timeout=DETECTION_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            log.error(
                "Detection exceeded %.0fs deadline; will retry on next candle",
                DETECTION_TIMEOUT_S,
            )
        except Exception:
            log.exception("Detection task crashed unexpectedly")

    def _run_detection(self) -> None:
        try:
            price_data = fetch_btc_price()
            log.info(
                "BTC $%.2f | 1h %+.2f%% | 24h %+.2f%%",
                price_data["price_usd"],
                price_data["change_1h"],
                price_data["change_24h"],
            )

            state = load_state(STATE_PATH)

            features: dict = {}
            try:
                snapshot = fetch_market_snapshot()
                features = compute_market_features(snapshot, state=state)
            except Exception as e:
                log.warning(
                    "OKX REST fetch failed: %s — falling back to legacy detector", e
                )

            append_feature_history(state, features)

            detector = SpikeDetector(state)
            spike = (
                detector.check_composite(price_data, features)
                if features else
                detector.check_legacy(price_data)
            )

            if spike is None:
                save_state(STATE_PATH, state)
                return

            log.info(
                "SPIKE: %s %+.2f%% (%s) score=%s",
                spike["window"], spike["change"], spike["direction"],
                spike.get("score"),
            )

            factors = gather_factors(spike)
            similar = find_similar_alerts(HISTORY_DB_PATH, spike, limit=3)
            summary = summarize(price_data, spike, factors, similar_alerts=similar)

            chart_png: bytes | None = None
            try:
                chart_png = render_chart(spike, price_data)
            except Exception as e:
                log.warning("Chart render failed: %s", e)

            dry_run = os.getenv("DRY_RUN", "false").lower() == "true"
            enable_x = os.getenv("ENABLE_X_POST", "false").lower() == "true"
            d_disc = d_x = False
            if dry_run:
                log.info("[DRY_RUN] Skipping actual posts")
                d_disc = True
            else:
                d_disc = post_discord(
                    summary, price_data, spike, chart_png=chart_png
                )
                if enable_x:
                    d_x = post_x(
                        summary, price_data, spike, chart_png=chart_png
                    )

            record_alert(
                HISTORY_DB_PATH,
                price_data=price_data,
                spike=spike,
                factors=factors,
                summary=summary,
                delivered_discord=d_disc,
                delivered_x=d_x,
            )

            if d_disc or d_x:
                state.update({
                    "last_alert_time": price_data["timestamp"],
                    "last_alert_price": price_data["price_usd"],
                    "last_alert_direction": spike["direction"],
                    "last_spike_window": spike["window"],
                    "last_spike_change": spike["change"],
                    "last_spike_score": spike.get("score"),
                })
            save_state(STATE_PATH, state)
        except Exception as e:
            # Log but never crash the WS loop — the next candle close gets a fresh try.
            log.exception("Detection pipeline failed: %s", e)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def _main() -> None:
    bot = RealtimeBot()
    loop = asyncio.get_running_loop()
    for sig_name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, bot.shutdown.set)
        except NotImplementedError:
            # Windows: signal handlers in asyncio aren't supported; the
            # process will still exit on Ctrl-C via KeyboardInterrupt.
            pass
    log.info("Realtime bot starting...")
    await bot.run()


def main() -> int:
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        log.info("Interrupted by user")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
