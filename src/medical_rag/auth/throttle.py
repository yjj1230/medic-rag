"""Brute-force protection for the password login endpoint.

``/v1/auth/login`` is the only credential entry point in the system, and it
authenticates on a phone number plus a password — a low-entropy pair that is
practical to guess at machine speed.  It previously accepted unlimited attempts
with no delay and no record.

The defence here is deliberately small and self-contained:

* a per-identifier **and** per-source-address sliding failure window, so neither
  a single account nor a single host can be hammered;
* a constant-cost failure path (see ``auth.routes`` — an unknown account still
  performs a bcrypt comparison) so response content and timing do not disclose
  whether an account exists;
* bounded memory: the table is pruned on write and hard-capped, so an attacker
  cannot turn the throttle itself into an unbounded-memory DoS.

Scope note: this is per-process state.  It stops the common single-node
credential-stuffing pattern and is honest about its limit — a multi-replica
deployment should back this with the shared cache (Redis), which the project
already runs.
"""
from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass, field


@dataclass
class _AttemptWindow:
    """Recent failure timestamps for one throttle key."""

    failures: list[float] = field(default_factory=list)

    def prune(self, *, now: float, window_seconds: float) -> None:
        cutoff = now - window_seconds
        if self.failures and self.failures[0] < cutoff:
            self.failures = [stamp for stamp in self.failures if stamp >= cutoff]


class LoginThrottle:
    """Sliding-window failure counter keyed by identifier and by client address.

    Both keys must be under the limit for a login to be attempted.  Counting the
    client address as well as the identifier is what stops one host from walking
    a list of phone numbers, which a per-identifier limit alone allows.
    """

    # Hard cap on tracked keys.  Reaching it evicts the oldest entry, so memory
    # is bounded regardless of how many distinct identifiers an attacker sends.
    MAX_TRACKED_KEYS = 10_000

    def __init__(
        self,
        *,
        max_failures: int = 5,
        window_seconds: float = 300.0,
        clock=time.monotonic,
    ) -> None:
        if max_failures < 1:
            raise ValueError("max_failures must be positive")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self._clock = clock
        self._windows: OrderedDict[str, _AttemptWindow] = OrderedDict()

    def retry_after(self, *keys: str) -> float:
        """Seconds until the most-blocked key is allowed again (0 when clear)."""
        now = self._clock()
        longest = 0.0
        for key in keys:
            window = self._windows.get(key)
            if window is None:
                continue
            window.prune(now=now, window_seconds=self.window_seconds)
            if len(window.failures) < self.max_failures:
                continue
            oldest = window.failures[-self.max_failures]
            longest = max(longest, self.window_seconds - (now - oldest))
        return max(0.0, longest)

    def record_failure(self, *keys: str) -> None:
        now = self._clock()
        for key in keys:
            window = self._windows.get(key)
            if window is None:
                window = _AttemptWindow()
                self._windows[key] = window
            window.prune(now=now, window_seconds=self.window_seconds)
            window.failures.append(now)
            self._windows.move_to_end(key)
        self._evict_overflow()

    def record_success(self, *keys: str) -> None:
        """A correct password clears the counters for the keys it proves control of."""
        for key in keys:
            self._windows.pop(key, None)

    def _evict_overflow(self) -> None:
        while len(self._windows) > self.MAX_TRACKED_KEYS:
            self._windows.popitem(last=False)


def throttle_key_pair(identifier: str, client_host: str | None) -> tuple[str, str]:
    """Throttle keys for one attempt: the account and the calling host.

    The identifier is normalised so ``13800000000`` and `` 13800000000 `` share
    a counter instead of giving an attacker free attempts through whitespace.
    """
    return (f"id:{identifier.strip().lower()}", f"ip:{client_host or 'unknown'}")
