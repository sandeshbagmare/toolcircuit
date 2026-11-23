"""Tests for toolcircuit."""

import pytest

from toolcircuit import CircuitOpen, Policy, ToolCircuit


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make(threshold=3, cooldown=30.0, **policies):
    clock = FakeClock()
    circuit = ToolCircuit(
        default=Policy(failure_threshold=threshold, cooldown=cooldown),
        policies={
            name: Policy(failure_threshold=threshold_, cooldown=cooldown_)
            for name, (threshold_, cooldown_) in policies.items()
        },
        clock=clock,
    )
    return circuit, clock


class TestPolicy:
    def test_a_zero_threshold_is_rejected(self):
        with pytest.raises(ValueError, match="failure_threshold"):
            Policy(failure_threshold=0)

    def test_a_zero_cooldown_is_rejected(self):
        with pytest.raises(ValueError, match="cooldown"):
            Policy(cooldown=0)

    def test_the_defaults(self):
        policy = Policy()
        assert policy.failure_threshold == 3
        assert policy.cooldown == 30.0


class TestStates:
    def test_a_fresh_circuit_is_closed(self):
        circuit, _ = make()
        assert circuit.state("t") == "closed"
        assert circuit.allow("t") is True

    def test_failures_below_the_threshold_keep_it_closed(self):
        circuit, _ = make(threshold=3)
        assert circuit.record_failure("t") == "closed"
        assert circuit.record_failure("t") == "closed"
        assert circuit.state("t") == "closed"

    def test_the_threshold_opens_it(self):
        circuit, _ = make(threshold=3)
        circuit.record_failure("t")
        circuit.record_failure("t")
        assert circuit.record_failure("t") == "open"
        assert circuit.allow("t") is False

    def test_a_success_closes_it_and_resets(self):
        circuit, _ = make(threshold=2)
        circuit.record_failure("t")
        circuit.record_failure("t")
        assert circuit.record_success("t") == "closed"
        assert circuit.failures("t") == 0
        assert circuit.allow("t") is True

    def test_the_cooldown_makes_it_half_open(self):
        circuit, clock = make(threshold=1, cooldown=10)
        circuit.record_failure("t")
        clock.advance(9.9)
        assert circuit.state("t") == "open"
        clock.advance(0.1)
        assert circuit.state("t") == "half-open"
        assert circuit.allow("t") is True

    def test_a_successful_trial_closes_it(self):
        circuit, clock = make(threshold=1, cooldown=10)
        circuit.record_failure("t")
        clock.advance(10)
        assert circuit.record_success("t") == "closed"

    def test_a_failed_trial_reopens_it_for_another_cooldown(self):
        circuit, clock = make(threshold=1, cooldown=10)
        circuit.record_failure("t")
        clock.advance(10)
        assert circuit.record_failure("t") == "open"
        assert circuit.state("t") == "open"
        clock.advance(9)
        assert circuit.state("t") == "open"
        clock.advance(1)
        assert circuit.state("t") == "half-open"

    def test_tools_are_independent(self):
        circuit, _ = make(threshold=1)
        circuit.record_failure("a")
        assert circuit.state("a") == "open"
        assert circuit.state("b") == "closed"

    def test_a_per_tool_policy(self):
        circuit, _ = make(threshold=5, search=(1, 60))
        circuit.record_failure("search")
        circuit.record_failure("other")
        assert circuit.state("search") == "open"
        assert circuit.state("other") == "closed"


class TestRetryAfter:
    def test_a_closed_circuit_needs_no_wait(self):
        circuit, _ = make()
        assert circuit.retry_after("t") == 0.0

    def test_after_opening_it_is_the_cooldown(self):
        circuit, _ = make(threshold=1, cooldown=30)
        circuit.record_failure("t")
        assert circuit.retry_after("t") == 30.0

    def test_it_counts_down(self):
        circuit, clock = make(threshold=1, cooldown=30)
        circuit.record_failure("t")
        clock.advance(10)
        assert circuit.retry_after("t") == 20.0

    def test_it_never_goes_negative(self):
        circuit, clock = make(threshold=1, cooldown=30)
        circuit.record_failure("t")
        clock.advance(100)
        assert circuit.retry_after("t") == 0.0


class TestCall:
    def test_a_successful_call_passes_through(self):
        circuit, _ = make()
        assert circuit.call("t", lambda a, b: a + b, 1, 2) == 3

    def test_kwargs_are_passed(self):
        circuit, _ = make()
        assert circuit.call("t", lambda value=0: value, value=7) == 7

    def test_a_failure_is_counted_and_reraised(self):
        circuit, _ = make(threshold=2)

        def boom():
            raise ValueError("nope")

        with pytest.raises(ValueError, match="nope"):
            circuit.call("t", boom)
        assert circuit.failures("t") == 1

    def test_the_second_failure_opens_it(self):
        circuit, _ = make(threshold=1)

        def boom():
            raise ValueError("nope")

        with pytest.raises(ValueError):
            circuit.call("t", boom)
        with pytest.raises(CircuitOpen) as caught:
            circuit.call("t", boom)
        assert caught.value.tool == "t"
        assert caught.value.retry_after == 30.0
        assert "unavailable" in str(caught.value)

    def test_the_function_is_not_called_while_open(self):
        circuit, _ = make(threshold=1)
        circuit.record_failure("t")
        called = []
        with pytest.raises(CircuitOpen):
            circuit.call("t", lambda: called.append(1))
        assert called == []

    def test_a_success_after_a_cooldown_recovers(self):
        circuit, clock = make(threshold=1, cooldown=5)
        circuit.record_failure("t")
        clock.advance(5)
        assert circuit.call("t", lambda: "ok") == "ok"
        assert circuit.state("t") == "closed"

    def test_it_is_a_runtime_error(self):
        assert issubclass(CircuitOpen, RuntimeError)


class TestResetAndReport:
    def test_reset_one_tool(self):
        circuit, _ = make(threshold=1)
        circuit.record_failure("a")
        circuit.reset("a")
        assert circuit.state("a") == "closed"

    def test_reset_everything(self):
        circuit, _ = make(threshold=1)
        circuit.record_failure("a")
        circuit.record_failure("b")
        circuit.reset()
        assert circuit.report() == {}

    def test_report(self):
        circuit, _ = make(threshold=1, cooldown=12)
        circuit.record_failure("t")
        assert circuit.report() == {"t": {"state": "open", "failures": 1, "retry_after": 12.0}}

    def test_the_report_of_an_untouched_circuit(self):
        circuit, _ = make()
        assert circuit.report() == {}

    def test_failures_of_an_untouched_tool(self):
        circuit, _ = make()
        assert circuit.failures("never") == 0


class TestMiddlewareStyleUse:
    def test_an_agent_that_stops_calling_a_dead_tool(self):
        circuit, _ = make(threshold=2, cooldown=60)
        attempts = []

        def search():
            attempts.append(1)
            raise ConnectionError("service down")

        messages = []
        for _ in range(5):
            try:
                circuit.call("search", search)
            except CircuitOpen as error:
                messages.append(f"search unavailable, retry in {error.retry_after:.0f}s")
            except ConnectionError:
                messages.append("search failed")
        assert len(attempts) == 2
        assert messages[-1] == "search unavailable, retry in 60s"
        assert messages.count("search failed") == 2
