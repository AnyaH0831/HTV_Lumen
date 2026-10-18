"""One gate every Gemini call passes through, so rate limits cost fluency, never correctness.

A single signed phrase costs the normalizer one call plus up to 8 planner rounds, so signing a few
words in a row bursts far past the free tier's requests-per-minute and lands HTTP 429s. Handled
naively, each 429 just fails that one call, the next phrase fires again immediately, and the app
ends up hammering the API for output it could have produced locally: a rate-limit storm rather than
a rate limit. This gate turns a burst into a polite queue instead:

  * concurrency cap -- calls are serialised (GEMINI_MAX_CONCURRENT, default 1), so a burst queues
    behind itself instead of arriving as a thundering herd;
  * backoff         -- 429/5xx and hung calls retry with exponential backoff + jitter inside a
    shared time budget, so a short rate-limit window self-heals invisibly;
  * circuit breaker -- after GEMINI_429_TRIP 429s inside GEMINI_429_WINDOW_S seconds the gate OPENS
    for GEMINI_COOLDOWN_S: calls fail fast WITHOUT touching the API, the app runs on the rule
    normalizer and the offline planner, the quota window recovers, and the gate then half-opens and
    lets one real call through before deciding again. A 429 that reports the PLAN quota is gone
    ("You exceeded your current quota") opens the gate on the first hit, for GEMINI_QUOTA_COOLDOWN_S
    -- retrying that one only wastes the wait.

Both callers already fall back when Gemini is unusable (normalize -> rule pass, plan_gemini ->
plan_offline), so raising from here is always safe. Nothing in this module invents output.
"""
import asyncio, os, random, time

RETRYABLE = {429, 500, 502, 503, 504}          # 4xx we can recover from, plus transient 5xx

# A 429 that says the PLAN quota is gone ("You exceeded your current quota, please check your plan
# and billing details") will not recover in seconds -- retrying it just burns time and quota. Those
# trip the breaker on the first hit, with a longer cooldown, while a per-minute 429 is absorbed by
# the short counter below.
QUOTA_HINTS = ("exceeded your current quota", "check your plan", "quota exceeded for quota metric")


def _f(name, default):
    try: return float(os.getenv(name, default))
    except (TypeError, ValueError): return float(default)


class GateOpen(Exception):
    """Refused at the gate: Gemini is in a cooldown window (or the retry budget is spent)."""


class GeminiGate:
    def __init__(self):
        self._sem = asyncio.Semaphore(max(1, int(_f("GEMINI_MAX_CONCURRENT", 1))))
        self._lock = asyncio.Lock()
        self._window: list[float] = []          # timestamps of recent 429s
        self._open_until = 0.0

    @property
    def opened(self) -> bool:
        return time.time() < self._open_until

    @staticmethod
    def _code(exc):
        c = getattr(exc, "code", None)
        return c if isinstance(c, int) else None

    def _retryable(self, exc):
        # google.genai.errors.APIError exposes .code; a hang surfaces as TimeoutError
        return isinstance(exc, (asyncio.TimeoutError, TimeoutError)) or self._code(exc) in RETRYABLE

    async def _note(self, exc):
        """Count one rate-limit hit; trip the breaker once the window is full (or immediately on a plan quota)."""
        if self._code(exc) != 429: return
        now = time.time()
        quota = any(h in str(exc).lower() for h in QUOTA_HINTS)   # gone for the day, not for a second
        async with self._lock:
            self._window = [t for t in self._window if now - t < _f("GEMINI_429_WINDOW_S", 30)] + [now]
            if len(self._window) >= (1 if quota else _f("GEMINI_429_TRIP", 5)):
                self._open_until = now + (_f("GEMINI_QUOTA_COOLDOWN_S", 300) if quota
                                          else _f("GEMINI_COOLDOWN_S", 30))

    async def __call__(self, fn, budget_s=None):
        """Run async callable `fn()` -- a FRESH request on every attempt -- behind the gate.

        Raises the original error once retries are exhausted, and GateOpen immediately while the
        breaker is open. Callers must always have a local fallback, which both of ours do."""
        deadline = time.time() + (_f("GEMINI_CALL_BUDGET_S", 12) if budget_s is None else budget_s)
        if self.opened:
            raise GateOpen(f"gemini breaker open, {self._open_until - time.time():.0f}s of cooldown left")
        attempts = max(1, int(_f("GEMINI_MAX_ATTEMPTS", 3)))
        last: Exception | None = None
        async with self._sem:                    # serialised: bursts queue, they do not stampede
            for attempt in range(attempts):
                if self.opened:
                    raise GateOpen("gemini breaker opened while queued")
                try:
                    # a per-attempt slice of the shared budget: a hung call must not hold the gate
                    return await asyncio.wait_for(fn(), timeout=max(0.1, deadline - time.time()))
                except Exception as e:
                    last = e
                    if not self._retryable(e): raise        # a real bug or auth error: never mask it
                    await self._note(e)
                    if self.opened:
                        raise GateOpen(f"gemini breaker opened after HTTP {self._code(e)}") from e
                    if attempt + 1 >= attempts: raise       # budget/attempts spent: let the caller fall back
                    backoff = min(deadline - time.time(), _f("GEMINI_BACKOFF_S", 1.5) * (2 ** attempt))
                    if backoff <= 0: raise
                    await asyncio.sleep(backoff + random.uniform(0, backoff / 2))
        raise last if last else GateOpen("gemini gate gave up")   # unreachable, but keeps the contract obvious


gate = GeminiGate()                          # one gate per process: it is a rate limit, not a client
