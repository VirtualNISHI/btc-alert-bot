"""X (Twitter) account monitoring via Nitter RSS.

Nitter is a privacy-preserving Twitter frontend that exposes per-user
RSS feeds. Public instances are unstable — many are rate-limited,
broken, or have removed list support. We try each configured instance
until one returns non-empty parsed entries, then move on to the next
account.

Configuration:
- ``NITTER_ACCOUNTS``: comma-separated X usernames (no @ prefix) to
  monitor. Empty / unset → this analyzer is silently disabled.
- ``NITTER_INSTANCES``: comma-separated Nitter base URLs to try.
  Defaults to a small list of currently-best-known instances. If you
  self-host Nitter or have a paid alternative, point this at it.

Caveats:
- This is a *best-effort* feature. Nitter availability shifts week to
  week. Failures are logged at WARN and degrade silently to empty.
- GitHub Actions runner IPs are sometimes blocked by Nitter providers.
  If the analyzer never returns data in CI, that's the most likely cause.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import requests

log = logging.getLogger(__name__)

# Default instances rotate as the public Nitter ecosystem changes. Keep
# the list short — long fallbacks just slow the alert path on bad days.
DEFAULT_INSTANCES = [
    "https://nitter.poast.org",
    "https://nitter.privacydev.net",
    "https://nitter.tiekoetter.com",
]

# Tight per-request timeout: optional factor, must not delay the alert.
NITTER_TIMEOUT = 5

# Hard wall-time budget for the *entire* x_monitor pass. With multiple
# accounts × multiple dead instances the naive loop could otherwise
# eat 45s+ when Nitter is down — well past gather_factors's 30s deadline.
TOTAL_BUDGET_S = 15.0

# Look back this many minutes for tweet recency.
LOOKBACK_MIN = 60

# Cap on items returned to keep the prompt small.
MAX_ITEMS = 8

# Circuit breaker: if the last N consecutive runs all returned 0 items,
# auto-skip x_monitor for COOLDOWN_MIN minutes. Public Nitter is so often
# fully dead that probing it on every spike just wastes the latency budget.
CIRCUIT_FAILURES_BEFORE_OPEN = 3
CIRCUIT_COOLDOWN_MIN = 60
CIRCUIT_STATE_PATH = Path("data/x_monitor_circuit.json")

_session = requests.Session()
_session.headers.update({"User-Agent": "btc-alert-bot/0.1"})


def _try_instance(instance: str, account: str) -> list | None:
    """Fetch one Nitter instance/account pair. Returns parsed entries or None."""
    url = f"{instance.rstrip('/')}/{account}/rss"
    try:
        resp = _session.get(url, timeout=NITTER_TIMEOUT)
        if resp.status_code != 200:
            return None
        feed = feedparser.parse(resp.text)
        if not feed.entries:
            return None
        return list(feed.entries)
    except Exception:
        return None


def _fetch_account(
    account: str,
    instances: list[str],
    deadline: float,
) -> tuple[list, str | None]:
    """Try each instance in order; first non-empty wins.

    Returns ``(entries, working_instance)``. ``working_instance`` is the
    one that produced data, so the caller can prefer it for subsequent
    accounts and avoid re-probing dead hosts. Aborts early if the total
    deadline has been crossed.
    """
    for inst in instances:
        if time.monotonic() > deadline:
            log.warning(
                "Nitter total budget exceeded before @%s on %s", account, inst
            )
            return [], None
        items = _try_instance(inst, account)
        if items:
            log.info(
                "Nitter %s OK for @%s (%d entries)", inst, account, len(items)
            )
            return items, inst
    log.warning("All Nitter instances failed for @%s", account)
    return [], None


def _is_btc_relevant(title: str) -> bool:
    """Loose relevance filter — high-signal accounts post other things too."""
    t = (title or "").lower()
    keywords = (
        "bitcoin", "btc", "crypto", "etf", "fomc", "fed", "cpi",
        "powell", "sec", "binance", "coinbase", "tether", "stable",
        "halving", "etf", "treasury", "trump", "xrp",
    )
    return any(k in t for k in keywords)


def _load_circuit() -> dict:
    if not CIRCUIT_STATE_PATH.exists():
        return {}
    try:
        return json.loads(CIRCUIT_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_circuit(state: dict) -> None:
    try:
        CIRCUIT_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CIRCUIT_STATE_PATH.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        log.warning("Failed to persist x_monitor circuit: %s", e)


def fetch_x_monitor() -> list[dict]:
    """Pull recent BTC-relevant tweets from monitored X accounts.

    Returns [] silently when NITTER_ACCOUNTS is unset (opt-in feature).
    Never raises — failures degrade to fewer items.

    A persistent circuit breaker (data/x_monitor_circuit.json) auto-opens
    when consecutive runs return zero items and stays open for
    CIRCUIT_COOLDOWN_MIN minutes — public Nitter is broken often enough
    that probing it on every spike is pure latency waste.
    """
    accounts_raw = (os.getenv("NITTER_ACCOUNTS") or "").strip()
    if not accounts_raw:
        return []
    accounts = [a.strip().lstrip("@") for a in accounts_raw.split(",") if a.strip()]
    if not accounts:
        return []

    # ---- circuit breaker ----------------------------------------------
    circuit = _load_circuit()
    fails = int(circuit.get("consecutive_empty_runs", 0))
    opened_at_s = circuit.get("opened_at")
    if fails >= CIRCUIT_FAILURES_BEFORE_OPEN and opened_at_s:
        try:
            opened_at = datetime.fromisoformat(opened_at_s)
            elapsed = (datetime.now(timezone.utc) - opened_at).total_seconds() / 60
            if elapsed < CIRCUIT_COOLDOWN_MIN:
                log.info(
                    "x_monitor circuit OPEN (%.1fmin remaining); skipping",
                    CIRCUIT_COOLDOWN_MIN - elapsed,
                )
                return []
            # Cooldown elapsed — try once. If still empty, the empty-tracking
            # block below re-arms the circuit immediately.
            log.info("x_monitor circuit half-open: probing once")
        except Exception:
            pass

    instances_raw = (os.getenv("NITTER_INSTANCES") or "").strip()
    if instances_raw:
        instances = [
            i.strip().rstrip("/") for i in instances_raw.split(",") if i.strip()
        ]
    else:
        instances = DEFAULT_INSTANCES

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=LOOKBACK_MIN)
    deadline = time.monotonic() + TOTAL_BUDGET_S
    items: list[dict] = []
    sticky: str | None = None  # last-known-working instance, tried first
    any_fetch_succeeded = False  # at least one Nitter instance returned data
    for account in accounts:
        if time.monotonic() > deadline:
            log.warning(
                "Nitter total budget hit; remaining accounts skipped: %s",
                accounts[accounts.index(account):],
            )
            break
        # Front-load the sticky instance if we have one — most likely to succeed.
        ordered = (
            [sticky] + [i for i in instances if i != sticky]
            if sticky else instances
        )
        entries, winner = _fetch_account(account, ordered, deadline)
        if winner:
            sticky = winner
            any_fetch_succeeded = True
        for entry in entries:
            try:
                pub_dt = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
            except Exception:
                continue
            if pub_dt < cutoff:
                continue
            title = entry.get("title", "")
            if not _is_btc_relevant(title):
                continue
            items.append({
                "type": "x_monitor",
                "source": f"@{account}",
                "title": title[:200],
                "url": entry.get("link", ""),
                "published": pub_dt.isoformat(),
            })

    items.sort(key=lambda x: x["published"], reverse=True)
    items = items[:MAX_ITEMS]

    # ---- update circuit breaker ----------------------------------------
    # The breaker tracks INFRASTRUCTURE failures (Nitter instances dead),
    # NOT empty filtered results. Quiet accounts with no recent BTC posts
    # should NOT trip the breaker — that would cause us to skip a working
    # monitor for an hour right before a major tweet drops.
    if any_fetch_succeeded:
        # At least one instance worked; reset failure count.
        if circuit:
            _save_circuit({})
    else:
        # Every instance × every account failed — that's a real outage.
        new_fails = fails + 1
        new_state: dict = {"consecutive_empty_runs": new_fails}
        if new_fails >= CIRCUIT_FAILURES_BEFORE_OPEN:
            new_state["opened_at"] = datetime.now(timezone.utc).isoformat()
            log.warning(
                "x_monitor circuit OPENED after %d failed runs; "
                "skipping for %d min",
                new_fails, CIRCUIT_COOLDOWN_MIN,
            )
        _save_circuit(new_state)

    return items
