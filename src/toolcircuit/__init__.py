"""A per-tool circuit breaker, so a dead API stops being retried.

Retry logic assumes the next attempt can work. When a tool is down, forty retries do not
fix it — they spend the run's budget and its context on the same failure, and the agent
never gets to the part where it tells the user that the service is unavailable.

    >>> from toolcircuit import ToolCircuit
    >>> circuit = ToolCircuit()
    >>> circuit.state("search")
    'closed'
    >>> for _ in range(3):
    ...     state = circuit.record_failure("search")
    >>> state
    'open'
    >>> circuit.allow("search")
    False

Closed → open → half-open → closed, with an injectable clock so tests do not sleep.
Standard library only, Python 3.9+.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = ["CircuitOpen", "Policy", "ToolCircuit"]

__version__ = "0.1.0"


@dataclass(frozen=True)
class Policy:
    """How many failures open the circuit, and how long it stays open.

    ``failure_threshold`` consecutive failures open it. After ``cooldown`` seconds one
    trial call is allowed (half-open); a success closes the circuit, a failure reopens it
    for another cooldown.
    """

    failure_threshold: int = 3
    cooldown: float = 30.0

    def __post_init__(self) -> None:
        if self.failure_threshold < 1:
            raise ValueError("failure_threshold must be at least 1")
        if self.cooldown <= 0:
            raise ValueError("cooldown must be greater than zero")


class CircuitOpen(RuntimeError):
    """Raised by :meth:`ToolCircuit.call` when the circuit for a tool is open."""

    def __init__(self, tool: str, retry_after: float) -> None:
        self.tool = tool
        self.retry_after = retry_after
        super().__init__(f"{tool!r} is unavailable; retry in {retry_after:.1f}s")


@dataclass
class _State:
    failures: int = 0
    opened_at: float | None = None
    trials: int = 0


@dataclass
class ToolCircuit:
    """One circuit per tool: ``closed`` (healthy), ``open`` (refusing), ``half-open``.

    ``clock`` defaults to :func:`time.monotonic`; pass a function returning a float to
    test without sleeping.
    """

    default: Policy = field(default_factory=Policy)
    policies: Mapping[str, Policy] = field(default_factory=dict)
    clock: Callable[[], float] = time.monotonic
    _states: dict[str, _State] = field(default_factory=dict, repr=False)

    def _policy(self, tool: str) -> Policy:
        return self.policies.get(tool, self.default)

    def _state(self, tool: str) -> _State:
        return self._states.setdefault(tool, _State())

    # -- reading ---------------------------------------------------------

    def state(self, tool: str) -> str:
        """``closed``, ``open`` or ``half-open`` right now."""
        state = self._states.get(tool)
        if state is None or state.opened_at is None:
            return "closed"
        if self.clock() - state.opened_at >= self._policy(tool).cooldown:
            return "half-open"
        return "open"

    def allow(self, tool: str) -> bool:
        """Whether a call would be attempted now."""
        return self.state(tool) != "open"

    def retry_after(self, tool: str) -> float:
        """Seconds until the next trial is allowed; ``0.0`` when the circuit is closed."""
        if self.state(tool) == "closed":
            return 0.0
        state = self._state(tool)
        opened_at = state.opened_at if state.opened_at is not None else self.clock()
        elapsed = self.clock() - opened_at
        return round(max(self._policy(tool).cooldown - elapsed, 0.0), 6)

    def failures(self, tool: str) -> int:
        """Consecutive failures recorded for this tool."""
        state = self._states.get(tool)
        return state.failures if state else 0

    # -- recording -------------------------------------------------------

    def record_success(self, tool: str) -> str:
        """A call worked: close the circuit and forget the failures."""
        state = self._state(tool)
        state.failures = 0
        state.opened_at = None
        state.trials = 0
        return "closed"

    def record_failure(self, tool: str) -> str:
        """A call failed: count it, and open the circuit at the threshold."""
        policy = self._policy(tool)
        state = self._state(tool)
        if state.opened_at is not None:
            # a failed half-open trial reopens the circuit for another cooldown
            state.opened_at = self.clock()
            state.trials = 0
            return "open"
        state.failures += 1
        if state.failures >= policy.failure_threshold:
            state.opened_at = self.clock()
            return "open"
        return "closed"

    def reset(self, tool: str | None = None) -> None:
        """Forget one tool's history, or every tool's, for a new run."""
        if tool is None:
            self._states.clear()
        else:
            self._states.pop(tool, None)

    # -- using -----------------------------------------------------------

    def call(self, tool: str, function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Call ``function`` through the breaker.

        Raises :class:`CircuitOpen` without calling it while the circuit is open, and
        records the outcome otherwise. Exceptions from ``function`` are re-raised after
        being counted, so a caller's own retry logic still sees them.
        """
        if not self.allow(tool):
            raise CircuitOpen(tool, self.retry_after(tool))
        try:
            result = function(*args, **kwargs)
        except Exception:
            self.record_failure(tool)
            raise
        self.record_success(tool)
        return result

    def report(self) -> dict[str, dict[str, Any]]:
        """State per tool, for a log line at the end of a run."""
        return {
            tool: {
                "state": self.state(tool),
                "failures": state.failures,
                "retry_after": self.retry_after(tool),
            }
            for tool, state in self._states.items()
        }
