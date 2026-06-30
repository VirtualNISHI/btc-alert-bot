"""Unit tests for the one-shot YTD-low emergency latch (YTD_ONESHOT).

Runs standalone:  python tests/test_ytd_oneshot.py
Also pytest-discoverable.

Guarantee under test:
  - YTD_ONESHOT unset/false  -> existing recurring behavior; the latch flag
    is never set and is ignored even if present (off-by-default, no change).
  - YTD_ONESHOT=true          -> the badge fires once, mark_ytd_badged latches
    `ytd_emergency_fired`, and every later call returns "" (never re-arms).
"""
from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc_alert_bot.milestones import mark_ytd_badged, ytd_low_badge  # noqa: E402

_VARS = ("YTD_ONESHOT",)
NOW = datetime(2026, 6, 30, 13, 0, tzinfo=timezone.utc)  # June → past the March gate


@contextmanager
def env(**overrides):
    saved = {k: os.environ.get(k) for k in _VARS}
    try:
        for k in _VARS:
            os.environ.pop(k, None)
        for k, v in overrides.items():
            if v is not None:
                os.environ[k] = v
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _seeded_state(low=60000.0):
    # Pre-seeded so ytd_low_badge skips the seeding branch and evaluates the low.
    return {"ytd_low_year": 2026, "ytd_low": low}


def test_oneshot_fires_once_then_latches():
    with env(YTD_ONESHOT="true"):
        state = _seeded_state(60000.0)
        b1 = ytd_low_badge(state, 59000.0, now=NOW)
        assert "年初来最安値" in b1, f"expected a badge, got {b1!r}"

        mark_ytd_badged(state, 59000.0, now=NOW)
        assert state.get("ytd_emergency_fired") is True

        # A further new low must NOT badge again — latched for good.
        b2 = ytd_low_badge(state, 58000.0, now=NOW)
        assert b2 == "", f"expected latched empty, got {b2!r}"


def test_latch_ignored_when_oneshot_off():
    # Even with the flag already present, default mode ignores it and fires.
    with env():  # YTD_ONESHOT unset
        state = _seeded_state(60000.0)
        state["ytd_emergency_fired"] = True
        b = ytd_low_badge(state, 59000.0, now=NOW)
        assert "年初来最安値" in b, f"latch should be ignored when off, got {b!r}"


def test_mark_does_not_latch_when_off():
    with env():  # YTD_ONESHOT unset
        state = _seeded_state(60000.0)
        mark_ytd_badged(state, 59000.0, now=NOW)
        assert state.get("ytd_emergency_fired") is None  # no latch in default mode


def test_no_badge_when_not_a_new_low():
    # Sanity: price above the running low never badges (one-shot irrelevant).
    with env(YTD_ONESHOT="true"):
        state = _seeded_state(60000.0)
        assert ytd_low_badge(state, 61000.0, now=NOW) == ""


def _run_all():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e!r}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {t.__name__}: {e!r}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
