"""Year-to-date-low milestone badge for alerts.

When BTC prints a new year-to-date low, the FIRST alert that detects it
gets a prominent badge line ("🔴 年初来最安値を更新（$XX,XXX）"). Subsequent
alerts during the same downtrend do NOT repeat it (per the user's
"一回目のみ"). The flag resets at the turn of the calendar year.

State persisted in ``state.json``:
  ytd_low_year      : int   — calendar year the low belongs to
  ytd_low           : float — lowest USD price seen so far this year
  ytd_low_announced : bool  — whether the badge has already fired this year
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable

log = logging.getLogger(__name__)


def ytd_low_badge(
    state: dict,
    price_usd: float,
    *,
    now: datetime | None = None,
    seed_year_low: Callable[[], float | None] | None = None,
) -> str:
    """Return the YTD-low badge if this alert is the FIRST new YTD low, else "".

    Mutates ``state`` to track the running low + announced flag (caller is
    expected to persist ``state`` afterwards).

    Behavior:
    - First run of a calendar year (or missing baseline): seed ``ytd_low``
      from ``seed_year_low()`` (historical low) min the current price, and
      return "" — there's no prior reference to "break" yet.
    - Later: if price < ``ytd_low`` it's a new YTD low; update the low and,
      if not yet announced this year, set the flag and return the badge.
    """
    try:
        price = float(price_usd)
    except (TypeError, ValueError):
        return ""
    if price <= 0:
        return ""

    now = now or datetime.now(timezone.utc)
    year = now.year

    # (Re)seed the baseline at a year boundary or on first ever run.
    if state.get("ytd_low_year") != year or state.get("ytd_low") is None:
        seed: float | None = None
        if seed_year_low is not None:
            try:
                seed = seed_year_low()
            except Exception as e:  # pragma: no cover - network/parse guard
                log.warning("YTD-low seed fetch failed: %s", e)
        baseline = min(seed, price) if seed else price
        state["ytd_low_year"] = year
        state["ytd_low"] = baseline
        state["ytd_low_announced"] = False
        log.info("YTD-low baseline seeded: $%,.0f (year %d)", baseline, year)
        return ""

    try:
        ytd_low = float(state["ytd_low"])
    except (TypeError, ValueError):
        state["ytd_low"] = price
        return ""

    if price < ytd_low:
        state["ytd_low"] = price  # always track the running low
        if not state.get("ytd_low_announced"):
            state["ytd_low_announced"] = True
            log.info("YTD-low break — badging once: $%,.0f", price)
            return f"🔴 年初来最安値を更新（${price:,.0f}）"
    return ""
