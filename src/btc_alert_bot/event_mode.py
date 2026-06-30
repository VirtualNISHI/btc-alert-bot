"""Event-mode: temporarily heighten alert sensitivity during scheduled
high-impact macro windows (e.g. an FOMC rate decision), then auto-revert.

OFF BY DEFAULT. With ``EVENT_MODE_WINDOWS`` unset or empty, every factor
returned here is exactly ``1.0`` and detection behaves identically to
normal operation — this module adds no behaviour unless explicitly armed.

Why an explicit time-window knob (not the macro calendar): the calendar
feed (ForexFactory) is best-effort and only *enriches* the alert text;
firing must stay deterministic. An explicit UTC window is controllable,
testable, and reverts on its own the moment the window closes.

Config (env), all optional:
  EVENT_MODE_WINDOWS
      Comma-separated ``START/END`` pairs, each an ISO-8601 UTC instant.
      Example — FOMC 2026-06-17 decision (18:00Z) + presser + digestion:
          EVENT_MODE_WINDOWS=2026-06-17T17:45:00Z/2026-06-17T21:00:00Z
      A timestamp inside ANY pair (start inclusive, end exclusive) arms
      event mode. ISO strings never contain ``/`` or ``,`` so the
      separators are unambiguous.
  EVENT_MODE_THRESHOLD_MULT   (default 0.6) multiplier on FIRE thresholds
      while active — a 2.0% floor becomes 1.2%. Clamped to (0, 1];
      set to 1.0 to leave thresholds unchanged.
  EVENT_MODE_COOLDOWN_MULT    (default 0.5) multiplier on SAME-direction
      cooldowns + the global debounce while active — 60min becomes 30min.
      Clamped to (0, 1]; set to 1.0 to leave them unchanged.
  EVENT_MODE_OPP_DIR_MULT     (default 0.1) multiplier on the OPPOSITE-
      direction (reversal) cooldown while active — 60min becomes 6min. This
      is deliberately far more aggressive than EVENT_MODE_COOLDOWN_MULT: the
      whole point of an FOMC window is to catch the sharp REVERSAL leg that
      lands minutes after the first spike, which the normal 60-min reversal
      cooldown would otherwise swallow. Clamped to (0, 1]; set to 1.0 to
      leave the reversal cooldown unchanged.

Every public function is defensive: any parse/clock error degrades to the
safe default (inactive / factor 1.0) and never raises into the hot path.
Env is read on each call, so updating .env + restarting the container is
enough to (dis)arm — no code change needed.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

log = logging.getLogger(__name__)

_DEFAULT_THRESHOLD_MULT = 0.6
_DEFAULT_COOLDOWN_MULT = 0.5
_DEFAULT_OPP_DIR_MULT = 0.1


def _coerce_dt(value: object | None) -> datetime | None:
    """Convert datetime | ISO-8601 str | epoch (s/ms) to a tz-aware UTC dt.

    ``None`` means "now" (wall clock). Returns ``None`` on any parse
    failure — callers treat that as "inactive", the fail-safe default.
    """
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    s = str(value).strip()
    if not s:
        return None
    # ISO-8601 first (accept a trailing 'Z').
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    # Epoch seconds, or milliseconds (OKX candle ts) if implausibly large.
    try:
        num = float(s)
        if num > 1e11:
            num /= 1000.0
        return datetime.fromtimestamp(num, tz=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def _parse_windows() -> list[tuple[datetime, datetime]]:
    raw = os.getenv("EVENT_MODE_WINDOWS", "").strip()
    if not raw:
        return []
    windows: list[tuple[datetime, datetime]] = []
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or "/" not in pair:
            continue
        start_s, _, end_s = pair.partition("/")
        start = _coerce_dt(start_s.strip())
        end = _coerce_dt(end_s.strip())
        if start and end and start < end:
            windows.append((start, end))
        else:
            log.warning("Ignoring malformed EVENT_MODE_WINDOWS entry: %r", pair)
    return windows


def _mult(env_name: str, default: float) -> float:
    raw = os.getenv(env_name)
    if not raw or not raw.strip():
        val = default
    else:
        try:
            val = float(raw)
        except ValueError:
            log.warning("Invalid %s=%r — using default %.2f", env_name, raw, default)
            val = default
    # Event mode only RAISES sensitivity (lowers thresholds): clamp to (0, 1].
    # Anything out of range falls back to 1.0 (a safe no-op) rather than
    # silently over-loosening.
    if not (0.0 < val <= 1.0):
        log.warning("%s=%s out of (0, 1] — ignoring (no change)", env_name, val)
        return 1.0
    return val


def is_active(now: object | None = None) -> bool:
    """True iff ``now`` falls inside any configured event window."""
    try:
        dt = _coerce_dt(now)
        if dt is None:
            return False
        return any(start <= dt < end for start, end in _parse_windows())
    except Exception:  # pragma: no cover - hot-path guard, must never raise
        return False


def active_window_label(now: object | None = None) -> str | None:
    """``"<start> → <end>"`` for the active window, else ``None``."""
    try:
        dt = _coerce_dt(now)
        if dt is None:
            return None
        for start, end in _parse_windows():
            if start <= dt < end:
                return f"{start.isoformat()} → {end.isoformat()}"
    except Exception:  # pragma: no cover - hot-path guard
        return None
    return None


def threshold_factor(now: object | None = None) -> float:
    """Multiplier for FIRE thresholds: <1 while a window is active, else 1.0."""
    try:
        if not is_active(now):
            return 1.0
        return _mult("EVENT_MODE_THRESHOLD_MULT", _DEFAULT_THRESHOLD_MULT)
    except Exception:  # pragma: no cover - hot-path guard
        return 1.0


def cooldown_factor(now: object | None = None) -> float:
    """Multiplier for SAME-dir cooldowns + debounce: <1 while active, else 1.0."""
    try:
        if not is_active(now):
            return 1.0
        return _mult("EVENT_MODE_COOLDOWN_MULT", _DEFAULT_COOLDOWN_MULT)
    except Exception:  # pragma: no cover - hot-path guard
        return 1.0


def opp_dir_cooldown_factor(now: object | None = None) -> float:
    """Multiplier for the OPPOSITE-dir (reversal) cooldown: <1 while active,
    else 1.0. More aggressive than cooldown_factor so the FOMC reversal leg
    isn't swallowed by the normal reversal cooldown."""
    try:
        if not is_active(now):
            return 1.0
        return _mult("EVENT_MODE_OPP_DIR_MULT", _DEFAULT_OPP_DIR_MULT)
    except Exception:  # pragma: no cover - hot-path guard
        return 1.0


def describe() -> str:
    """One-line summary for startup logging."""
    try:
        windows = _parse_windows()
        if not windows:
            return "event-mode: disarmed (EVENT_MODE_WINDOWS unset)"
        tm = _mult("EVENT_MODE_THRESHOLD_MULT", _DEFAULT_THRESHOLD_MULT)
        cm = _mult("EVENT_MODE_COOLDOWN_MULT", _DEFAULT_COOLDOWN_MULT)
        om = _mult("EVENT_MODE_OPP_DIR_MULT", _DEFAULT_OPP_DIR_MULT)
        wins = "; ".join(f"{s.isoformat()}→{e.isoformat()}" for s, e in windows)
        active = " [ACTIVE NOW]" if is_active() else ""
        return (
            f"event-mode: armed, thresholds ×{tm:g}, cooldowns ×{cm:g}, "
            f"reversal-cooldown ×{om:g}, windows=[{wins}]{active}"
        )
    except Exception:  # pragma: no cover - logging helper, must never raise
        return "event-mode: state unknown (describe failed)"
