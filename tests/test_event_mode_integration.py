"""Integration check: event-mode actually lowers the real detector's fire
thresholds end-to-end.

Scenario: a +1.5% / 1h move with quiet shorter windows and flat history
(so the composite-score path can't fire on its own). It sits BELOW the
normal 1h hard floor (2.0%) but ABOVE the event-mode floor (2.0 × 0.6 =
1.2%). So:
  - event-mode OFF  -> check_composite returns None (nothing fires)
  - event-mode ON   -> fires as a "1h" spike

Also asserts the OFF path is unchanged regardless of the clock.

Runs standalone:  python tests/test_event_mode_integration.py
"""
from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc_alert_bot.detector import (  # noqa: E402
    SpikeDetector,
    is_counter_trend_bounce,
    record_alert_in_state,
)

_VARS = (
    "EVENT_MODE_WINDOWS",
    "EVENT_MODE_THRESHOLD_MULT",
    "EVENT_MODE_COOLDOWN_MULT",
    "EVENT_MODE_OPP_DIR_MULT",
)
WIN = "2026-06-17T17:45:00Z/2026-06-17T21:00:00Z"
TS_INSIDE = "2026-06-17T18:00:00+00:00"
TS_OUTSIDE = "2026-06-17T12:00:00+00:00"


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


def _flat_history(n=40):
    """n near-identical feature snapshots → z-scores ≈ 0 (composite can't fire)."""
    hist = []
    for i in range(n):
        jitter = (i % 3) * 1e-4  # tiny, keeps MAD nonzero but z≈0
        hist.append({
            "ts": f"hist-{i}",
            "atr_pct": 0.10 + jitter,
            "volume_5bar": 1.00 + jitter,
            "oi_change_1h_pct": 0.0 + jitter,
            "funding_rate": 0.01 + jitter,
            "move_per_atr": 0.50 + jitter,
            "return_5m": 0.0,
            "return_15m": 0.0,
            "return_1h": 0.0,
            "return_2h": 0.0,
            "return_12h": 0.0,
        })
    return hist


def _borderline_features(ts):
    """+1.5% 1h move; everything else quiet. Mirrors history's baseline so
    the z-scores stay low and only the 1h hard floor is in play."""
    return {
        "ts": ts,
        "atr_pct": 0.10,
        "volume_5bar": 1.00,
        "oi_change_1h_pct": 0.0,
        "funding_rate": 0.01,
        "move_per_atr": 0.50,
        "return_5m": 0.10,
        "return_15m": 0.30,
        "return_1h": 1.50,   # < 2.0 normal floor, > 1.2 event floor
        "return_2h": 0.50,
        "return_12h": 0.50,
    }


def _detect(now_ts):
    state = {"feature_history": _flat_history()}
    det = SpikeDetector(state)
    price_data = {
        "timestamp": now_ts,
        "price_usd": 66000.0,
        "change_1h": 1.50,
        "change_24h": 0.50,
    }
    return det.check_composite(price_data, _borderline_features(now_ts))


def test_off_does_not_fire_borderline_move():
    with env():  # event mode disarmed
        assert _detect(TS_INSIDE) is None
        assert _detect(TS_OUTSIDE) is None


def test_on_fires_borderline_move_inside_window():
    with env(EVENT_MODE_WINDOWS=WIN):
        spike = _detect(TS_INSIDE)
        assert spike is not None, "event-mode should have fired the 1.5%/1h move"
        assert spike["window"] == "1h"
        assert spike["direction"] == "up"


def test_on_but_outside_window_does_not_fire():
    with env(EVENT_MODE_WINDOWS=WIN):
        assert _detect(TS_OUTSIDE) is None  # armed, but clock outside the window


# --- Whipsaw reversal leg: the FOMC pattern the feature exists to catch ----

def _detect_reversal(now_ts, prior_up_ts):
    """Record a prior UP long-tier (1h) alert, then evaluate a -1.5% / 1h
    DOWN reversal at ``now_ts`` and return the resulting spike (or None)."""
    state = {"feature_history": _flat_history()}
    det = SpikeDetector(state)
    record_alert_in_state(
        state,
        {"window": "1h", "change": 2.0, "direction": "up"},
        {"timestamp": prior_up_ts, "price_usd": 66000.0},
    )
    feats = _borderline_features(now_ts)
    feats.update(return_5m=-0.10, return_15m=-0.30, return_1h=-1.50,
                 return_2h=-0.50, return_12h=-0.50)
    price_data = {
        "timestamp": now_ts, "price_usd": 65000.0,
        "change_1h": -1.50, "change_24h": 0.50,
    }
    return det.check_composite(price_data, feats)


def test_reversal_within_reversal_cooldown_suppressed():
    # 3 min after the up leg: inside the event-mode reversal cooldown
    # (60 × 0.1 = 6 min) → suppressed.
    with env(EVENT_MODE_WINDOWS=WIN):
        assert _detect_reversal("2026-06-17T18:03:00+00:00",
                                "2026-06-17T18:00:00+00:00") is None


def test_reversal_after_reversal_cooldown_fires():
    # 10 min after the up leg: past the 6-min reversal cooldown → the DOWN
    # leg fires. This is the whipsaw leg the feature exists to catch.
    with env(EVENT_MODE_WINDOWS=WIN):
        spike = _detect_reversal("2026-06-17T18:10:00+00:00",
                                 "2026-06-17T18:00:00+00:00")
        assert spike is not None, "reversal leg should fire after the short cooldown"
        assert spike["window"] == "1h"
        assert spike["direction"] == "down"


def test_reversal_swallowed_without_aggressive_opp_dir():
    # Regression guard: with the reversal cooldown NOT specially shortened
    # (EVENT_MODE_OPP_DIR_MULT=1.0 → full 60 min), the +10min reversal is
    # swallowed. This is exactly the bug the 0.1 default fixes.
    with env(EVENT_MODE_WINDOWS=WIN, EVENT_MODE_OPP_DIR_MULT="1.0"):
        assert _detect_reversal("2026-06-17T18:10:00+00:00",
                                "2026-06-17T18:00:00+00:00") is None


def test_counter_trend_override_scales_with_event_mode():
    # Established uptrend (1h=+1.5%); a -1.0% down reversal.
    # Normal: 1.0 < 1.5 override AND 1h hasn't flipped negative → suppressed.
    assert is_counter_trend_bounce("down", -1.0, 1.5, 0.5) is True
    # Event-mode threshold 0.6 → override 0.9; 1.0 ≥ 0.9 → admitted (fires).
    assert is_counter_trend_bounce("down", -1.0, 1.5, 0.5, override_mult=0.6) is False


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
