"""gemini_gate: a burst of signing must degrade to local output, not into a rate-limit storm.

Each test builds a FRESH gate so env knobs (trip count, cooldown, concurrency) apply, and every
"API error" is a stub carrying the HTTP code the real google-genai error would carry.
"""
import asyncio, time
import pytest
from server import gemini_gate as G


class FakeAPIError(Exception):
    def __init__(self, code, msg="fake"): super().__init__(msg); self.code = code


def run(coro): return asyncio.run(coro)


def ok_fn():
    async def go(): return "sentence"
    return go


def test_transient_429s_are_retried_transparently_and_the_caller_never_sees_them(monkeypatch):
    monkeypatch.setenv("GEMINI_BACKOFF_S", "0.01")
    gate = G.GeminiGate(); calls = []

    async def flaky():
        calls.append(1)
        if len(calls) < 3: raise FakeAPIError(429)
        return "Pain in my head."

    assert run(gate(flaky)) == "Pain in my head."
    assert len(calls) == 3, "two rate limits then success: the phrase is still normalised by Gemini"


def test_each_retry_is_a_fresh_request_not_a_replayed_one(monkeypatch):
    monkeypatch.setenv("GEMINI_BACKOFF_S", "0.01")
    gate = G.GeminiGate(); seen = []

    async def fresh():
        seen.append(object())                     # a new call object every attempt
        if len(seen) < 2: raise FakeAPIError(429)
        return seen[-1]

    got = run(gate(fresh))
    assert got is seen[-1] and len(seen) == 2 and seen[0] is not seen[1]


def test_four_29s_in_the_window_open_the_breaker_and_the_next_call_fails_fast_without_an_api_call(monkeypatch):
    monkeypatch.setenv("GEMINI_BACKOFF_S", "0.01")
    monkeypatch.setenv("GEMINI_429_TRIP", "3")
    monkeypatch.setenv("GEMINI_COOLDOWN_S", "60")
    gate = G.GeminiGate(); calls = []

    async def limited():
        calls.append(1)
        raise FakeAPIError(429)

    with pytest.raises(G.GateOpen):
        run(gate(limited))                          # trip: attempts then breaker opens
    before = len(calls)
    with pytest.raises(G.GateOpen):
        run(gate(ok_fn()))                          # refused WITHOUT touching the API
    assert len(calls) == before, "a tripped breaker must stop the storm, not add to it"
    assert gate.opened


def test_breaker_half_opens_after_the_cooldown_and_lets_one_real_call_through(monkeypatch):
    monkeypatch.setenv("GEMINI_BACKOFF_S", "0.01")
    monkeypatch.setenv("GEMINI_429_TRIP", "1")
    monkeypatch.setenv("GEMINI_COOLDOWN_S", "0.05")
    gate = G.GeminiGate(); calls = []

    async def limited():
        calls.append(1)
        raise FakeAPIError(429)

    with pytest.raises(G.GateOpen):                # TRIP=1: the very first rate limit opens the breaker
        run(gate(limited))
    time.sleep(0.1)                                 # the rate-limit window has recovered
    assert run(gate(ok_fn())) == "sentence"
    assert gate.opened is False


def test_a_quota_exhaustion_trips_the_breaker_on_the_first_hit_without_wasting_retries(monkeypatch):
    """'You exceeded your current quota' is gone for the day: do not retry it, go local at once."""
    monkeypatch.setenv("GEMINI_BACKOFF_S", "0.01")
    gate = G.GeminiGate(); calls = []

    async def exhausted():
        calls.append(1)
        raise FakeAPIError(429, "429 RESOURCE_EXHAUSTED. You exceeded your current quota, "
                                "please check your plan and billing details.")

    with pytest.raises(G.GateOpen):
        run(gate(exhausted))
    assert len(calls) == 1, "a gone-for-the-day quota must not be retried"
    assert gate.opened


def test_a_real_error_is_never_retried_and_never_swallowed(monkeypatch):
    """An auth/bug error must surface immediately -- retrying it would only hide the fault."""
    gate = G.GeminiGate(); calls = []

    async def broken():
        calls.append(1)
        raise FakeAPIError(401, "API key not valid")

    with pytest.raises(FakeAPIError, match="API key not valid"):
        run(gate(broken))
    assert len(calls) == 1


def test_a_hung_call_is_bounded_by_the_budget_instead_of_holding_the_gate(monkeypatch):
    monkeypatch.setenv("GEMINI_CALL_BUDGET_S", "0.2")
    gate = G.GeminiGate()

    async def hang(): await asyncio.sleep(30)

    t0 = time.time()
    with pytest.raises((asyncio.TimeoutError, TimeoutError)):
        run(gate(hang))
    assert time.time() - t0 < 2, "a hung Gemini call must not freeze the phrase"


def test_budget_is_shared_across_attempts_so_retries_cannot_run_away(monkeypatch):
    monkeypatch.setenv("GEMINI_CALL_BUDGET_S", "0.3")
    monkeypatch.setenv("GEMINI_BACKOFF_S", "0.5")   # backoff longer than the budget: stop, do not wait it out
    gate = G.GeminiGate(); calls = []

    async def limited():
        calls.append(1)
        raise FakeAPIError(429)

    t0 = time.time()
    with pytest.raises(FakeAPIError):
        run(gate(limited))
    assert time.time() - t0 < 2
    assert len(calls) <= 3


def test_bursts_are_serialised_instead_of_arriving_as_a_thundering_herd(monkeypatch):
    monkeypatch.setenv("GEMINI_MAX_CONCURRENT", "1")
    gate = G.GeminiGate(); live = []; peak = []

    async def one():
        live.append(1); peak.append(len(live)); await asyncio.sleep(0.02); live.pop()

    async def burst(): await asyncio.gather(*(gate(one) for _ in range(5)))   # five PHRASES, not one

    run(burst())
    assert max(peak) == 1, "five simultaneous phrases must not fire five simultaneous Gemini calls"


def test_gate_open_is_distinct_from_the_api_error_so_the_trace_can_say_why(monkeypatch):
    monkeypatch.setenv("GEMINI_429_TRIP", "1")
    monkeypatch.setenv("GEMINI_COOLDOWN_S", "60")
    gate = G.GeminiGate()

    async def limited(): raise FakeAPIError(429)

    with pytest.raises(G.GateOpen):                # TRIP=1 opens the breaker on the first rate limit
        run(gate(limited))
    with pytest.raises(G.GateOpen) as exc:
        run(gate(ok_fn()))
    assert "breaker open" in str(exc.value)
