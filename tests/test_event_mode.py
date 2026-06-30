"""Unit tests for btc_alert_bot.event_mode.

Runs standalone (no pytest needed):  python tests/test_event_mode.py
Also discoverable by pytest (test_* functions).

Key guarantee under test: OFF BY DEFAULT — with EVENT_MODE_WINDOWS unset,
every factor is exactly 1.0 so detection is byte-for-byte unchanged.
"""
from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

# Make the package importable when run directly (src/ layout).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc_alert_bot import event_mode  # noqa: E402

_VARS = (
    "EVENT_MODE_WINDOWS",
    "EVENT_MODE_THRESHOLD_MULT",
    "EVENT_MODE_COOLDOWN_MULT",
    "EVENT_MODE_OPP_DIR_MULT",
)


@contextmanager
def env(**overrides):
    """Temporarily set/clear the event-mode env vars, then restore."""
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


# A canonical FOMC-style window and instants inside/outside it.
WIN = "2026-06-17T17:45:00Z/2026-06-17T21:00:00Z"
INSIDE = datetime(2026, 6, 17, 18, 0, tzinfo=timezone.utc)        # 18:00Z decision
OUTSIDE_BEFORE = datetime(2026, 6, 17, 17, 0, tzinfo=timezone.utc)
OUTSIDE_AFTER = datetime(2026, 6, 17, 22, 0, tzinfo=timezone.utc)


def test_off_by_default():
    """Unset env => inactive and every factor is exactly 1.0."""
    with env():
        assert event_mode.is_active(INSIDE) is False
        assert event_mode.threshold_factor(INSIDE) == 1.0
        assert event_mode.cooldown_factor(INSIDE) == 1.0
        assert event_mode.opp_dir_cooldown_factor(INSIDE) == 1.0
        assert event_mode.threshold_factor() == 1.0  # wall-clock path too
        assert event_mode.active_window_label(INSIDE) is None
        assert "disarmed" in event_mode.describe()


def test_active_inside_window_uses_defaults():
    with env(EVENT_MODE_WINDOWS=WIN):
        assert event_mode.is_active(INSIDE) is True
        assert event_mode.threshold_factor(INSIDE) == 0.6        # default
        assert event_mode.cooldown_factor(INSIDE) == 0.5         # default
        assert event_mode.opp_dir_cooldown_factor(INSIDE) == 0.1  # default
        assert event_mode.active_window_label(INSIDE) is not None
        assert "armed" in event_mode.describe()


def test_inactive_outside_window():
    with env(EVENT_MODE_WINDOWS=WIN):
        for ts in (OUTSIDE_BEFORE, OUTSIDE_AFTER):
            assert event_mode.is_active(ts) is False
            assert event_mode.threshold_factor(ts) == 1.0
            assert event_mode.cooldown_factor(ts) == 1.0


def test_boundaries_start_inclusive_end_exclusive():
    with env(EVENT_MODE_WINDOWS=WIN):
        start = datetime(2026, 6, 17, 17, 45, tzinfo=timezone.utc)
        end = datetime(2026, 6, 17, 21, 0, tzinfo=timezone.utc)
        assert event_mode.is_active(start) is True       # inclusive
        assert event_mode.is_active(end) is False        # exclusive


def test_custom_multipliers():
    with env(EVENT_MODE_WINDOWS=WIN,
             EVENT_MODE_THRESHOLD_MULT="0.7",
             EVENT_MODE_COOLDOWN_MULT="0.25",
             EVENT_MODE_OPP_DIR_MULT="0.05"):
        assert event_mode.threshold_factor(INSIDE) == 0.7
        assert event_mode.cooldown_factor(INSIDE) == 0.25
        assert event_mode.opp_dir_cooldown_factor(INSIDE) == 0.05


def test_opp_dir_factor_inactive_outside_window():
    with env(EVENT_MODE_WINDOWS=WIN):
        assert event_mode.opp_dir_cooldown_factor(OUTSIDE_AFTER) == 1.0


def test_mult_out_of_range_is_noop():
    """A multiplier outside (0, 1] falls back to 1.0 (safe no-op)."""
    with env(EVENT_MODE_WINDOWS=WIN,
             EVENT_MODE_THRESHOLD_MULT="1.5",   # >1 => clamp to no-op
             EVENT_MODE_COOLDOWN_MULT="0"):      # <=0 => clamp to no-op
        assert event_mode.threshold_factor(INSIDE) == 1.0
        assert event_mode.cooldown_factor(INSIDE) == 1.0


def test_invalid_mult_falls_back_to_default():
    with env(EVENT_MODE_WINDOWS=WIN, EVENT_MODE_THRESHOLD_MULT="abc"):
        assert event_mode.threshold_factor(INSIDE) == 0.6  # default


def test_iso_string_and_epoch_inputs():
    with env(EVENT_MODE_WINDOWS=WIN):
        # ISO string with Z
        assert event_mode.is_active("2026-06-17T18:00:00Z") is True
        # ISO string with explicit offset
        assert event_mode.is_active("2026-06-17T18:00:00+00:00") is True
        # epoch milliseconds (OKX candle ts style) for 18:00:00Z
        ms = int(INSIDE.timestamp() * 1000)
        assert event_mode.is_active(str(ms)) is True
        # outside, as ISO
        assert event_mode.is_active("2026-06-17T22:00:00Z") is False


def test_malformed_windows_ignored_no_raise():
    # Garbage entries are skipped; the one valid entry still works.
    with env(EVENT_MODE_WINDOWS="garbage,2026-13-99/nonsense," + WIN):
        assert event_mode.is_active(INSIDE) is True
    # Entirely malformed => inactive, never raises.
    with env(EVENT_MODE_WINDOWS="not-a-window"):
        assert event_mode.is_active(INSIDE) is False
        assert event_mode.threshold_factor(INSIDE) == 1.0


def test_reversed_window_rejected():
    """END before START is invalid and ignored (no accidental activation)."""
    with env(EVENT_MODE_WINDOWS="2026-06-17T21:00:00Z/2026-06-17T17:45:00Z"):
        assert event_mode.is_active(INSIDE) is False


def test_multiple_windows():
    two = (
        "2026-06-17T17:45:00Z/2026-06-17T21:00:00Z,"
        "2026-07-29T17:45:00Z/2026-07-29T21:00:00Z"
    )
    with env(EVENT_MODE_WINDOWS=two):
        assert event_mode.is_active(INSIDE) is True
        assert event_mode.is_active("2026-07-29T18:30:00Z") is True
        assert event_mode.is_active("2026-07-01T18:00:00Z") is False


def test_naive_datetime_treated_as_utc():
    with env(EVENT_MODE_WINDOWS=WIN):
        naive = datetime(2026, 6, 17, 18, 0)  # no tzinfo
        assert event_mode.is_active(naive) is True


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
