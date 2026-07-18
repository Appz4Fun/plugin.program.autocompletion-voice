# -*- coding: utf8 -*-
"""
Rate limiter for Gemini API calls.

Enforces free-tier limits: 15 RPM (per-minute) and 1000 RPD (per-day).
Thread-safe. Daily counter persists to a JSON file so it survives Kodi restarts.
"""

import datetime
import json
import os
import threading
import time


def _today_str():
    # type: () -> str
    return datetime.date.today().isoformat()


class RateLimiter(object):
    """Tracks Gemini API call rate against free-tier limits.

    Interface:
        can_call() -> bool       — True if a call is permitted right now
        record_call()            — Record that a call was made
        get_limit_reason() -> str — "rpm", "rpd", or "" (call after can_call() returns False)
        get_status() -> dict     — Diagnostic snapshot
    """

    RPM_LIMIT = 15
    RPD_LIMIT = 1000

    def __init__(self, storage_path=None):
        # type: (object) -> None
        self._lock = threading.Lock()
        self._minute_calls = []  # type: list  # timestamps of calls in the last 60s
        self._daily_count = 0
        self._daily_date = ""
        self._last_rejection = ""

        if storage_path is None:
            storage_path = self._default_storage_path()
        self._storage_path = storage_path

        self._load()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def can_call(self):
        # type: () -> bool
        """Return True if a Gemini API call is permitted right now."""
        with self._lock:
            self._prune_minute_window()
            self._reset_daily_if_needed()

            if len(self._minute_calls) >= self.RPM_LIMIT:
                self._last_rejection = "rpm"
                return False

            if self._daily_count >= self.RPD_LIMIT:
                self._last_rejection = "rpd"
                return False

            self._last_rejection = ""
            return True

    def record_call(self):
        # type: () -> None
        """Record that a Gemini API call was made."""
        with self._lock:
            self._reset_daily_if_needed()
            now = time.time()
            self._minute_calls.append(now)
            self._daily_count += 1
            self._daily_date = _today_str()
        self._save()

    def get_limit_reason(self):
        # type: () -> str
        """Return "rpm", "rpd", or "" — the reason the last can_call() returned False."""
        with self._lock:
            return self._last_rejection

    def get_status(self):
        # type: () -> dict
        """Return a diagnostic snapshot of current rate limit state."""
        with self._lock:
            self._prune_minute_window()
            self._reset_daily_if_needed()
            return {
                "rpm_used": len(self._minute_calls),
                "rpm_limit": self.RPM_LIMIT,
                "rpd_used": self._daily_count,
                "rpd_limit": self.RPD_LIMIT,
                "date": self._daily_date,
            }

    # ------------------------------------------------------------------
    # Internal helpers (must be called with self._lock held)
    # ------------------------------------------------------------------

    def _prune_minute_window(self):
        cutoff = time.time() - 60.0
        self._minute_calls = [t for t in self._minute_calls if t > cutoff]

    def _reset_daily_if_needed(self):
        today = _today_str()
        if self._daily_date != today:
            self._daily_count = 0
            self._daily_date = today

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _default_storage_path(self):
        # type: () -> object
        """Try to derive storage path from Kodi addon profile. Returns None on failure."""
        try:
            import xbmcaddon  # type: ignore

            profile = xbmcaddon.Addon().getAddonInfo("profile")
            if not isinstance(profile, str):
                return None
            return os.path.join(profile, "rate_limit_state.json")
        except Exception:
            return None

    def _load(self):
        if not self._storage_path:
            return
        try:
            if os.path.exists(self._storage_path):
                with open(self._storage_path, "r") as f:
                    data = json.load(f)
                today = _today_str()
                if data.get("date") == today:
                    self._daily_count = int(data.get("count", 0))
                    self._daily_date = today
        except Exception:
            pass

    def _save(self):
        if not self._storage_path:
            return
        try:
            dir_path = os.path.dirname(self._storage_path)
            if dir_path and not os.path.exists(dir_path):
                os.makedirs(dir_path)
            with open(self._storage_path, "w") as f:
                json.dump({"date": self._daily_date, "count": self._daily_count}, f)
        except Exception:
            pass
