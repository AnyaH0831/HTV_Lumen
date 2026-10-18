"""Normalizer stage: rule pass is deterministic and invents nothing; Gemini output is guarded in code."""
import asyncio, os, tempfile
from types import SimpleNamespace

os.environ.setdefault("SIGNBRIDGE_DATA", tempfile.mkdtemp())
from server import normalize as N     # noqa: E402  (env must be set before import)


def _gemini(sentence, glosses, dropped=(), uncertain=False, note=""):
    """Stub for normalize_gemini: returns a model-shaped object without touching the network."""
    return lambda cleaned, confs, context=None: asyncio.sleep(0, SimpleNamespace(
        sentence=sentence, glosses=list(glosses), dropped=list(dropped), uncertain=uncertain, note=note))


def test_rule_pass_dedupes_and_maps_glosses_to_vocab_phrases():
    out = N.normalize_rule(["PAIN", "PAIN", "HEAD"], [0.9, 0.9, 0.9])
    assert out["glosses"] == ["PAIN", "HEAD"]
    assert out["dropped"] == ["PAIN"]
    assert out["source"] == "rule"
    assert out["sentence"] == "Pain, head."


def test_rule_pass_never_invents_words():
    out = N.normalize_rule(["ALLERGIC", "MEDICINE"], [0.9, 0.9])
    words = set(out["sentence"].lower().rstrip(".").replace(",", "").split())
    allowed = {w for g in ("ALLERGIC", "MEDICINE") for w in N.PHRASE[g].split()}
    assert words <= allowed, f"invented: {words - allowed}"


def test_rule_pass_on_empty_input_still_yields_a_sentence():
    out = N.normalize_rule([], [])
    assert out["glosses"] == [] and out["sentence"] and out["source"] == "rule"


def test_assemble_collapses_only_consecutive_repeats_and_keeps_confidences_aligned():
    kept, kept_confs, dropped = N.assemble(["PAIN", "PAIN", "HEAD", "PAIN"], [0.9, 0.4, 0.8, 0.7])
    assert kept == ["PAIN", "HEAD", "PAIN"]      # the later PAIN is a real second signing, not a bounce
    assert dropped == ["PAIN"]
    assert kept_confs == [0.9, 0.8, 0.7], "a dropped repeat must not shift later glosses onto its confidence"


def test_gemini_cannot_drop_a_high_confidence_gloss(monkeypatch):
    monkeypatch.setattr(N, "normalize_gemini", _gemini("I have pain in my head.", ["PAIN", "HEAD"], dropped=["ALLERGIC"]))
    out = asyncio.run(N.normalize(["PAIN", "HEAD", "ALLERGIC"], [0.9, 0.9, 0.95], mode="auto"))
    assert out["source"] == "gemini"
    assert "ALLERGIC" in out["glosses"], "a 0.95-confidence ALLERGIC must never be talked out of the record"
    assert out["dropped"] == []


def test_gemini_may_drop_a_low_confidence_gloss(monkeypatch):
    monkeypatch.setattr(N, "normalize_gemini", _gemini("Pain.", ["PAIN"], dropped=["COLD"]))
    out = asyncio.run(N.normalize(["PAIN", "COLD"], [0.9, 0.4], mode="auto"))
    assert "COLD" not in out["glosses"] and "COLD" in out["dropped"]


def test_gemini_cannot_add_a_gloss_that_was_not_signed(monkeypatch):
    monkeypatch.setattr(N, "normalize_gemini", _gemini("Pain. Emergency.", ["PAIN", "EMERGENCY"]))
    out = asyncio.run(N.normalize(["PAIN"], [0.9], mode="auto"))
    assert out["glosses"] == ["PAIN"]


def test_gemini_returning_no_sentence_falls_back_to_rule(monkeypatch):
    monkeypatch.setattr(N, "normalize_gemini", _gemini("   ", ["PAIN"]))
    out = asyncio.run(N.normalize(["PAIN"], [0.9], mode="auto"))
    assert out["source"] == "rule"


def test_gemini_failure_falls_back_to_rule(monkeypatch):
    async def no_wifi(cleaned, confs, context=None): raise RuntimeError("no internet")
    monkeypatch.setattr(N, "normalize_gemini", no_wifi)
    out = asyncio.run(N.normalize(["PAIN", "HEAD"], [0.9, 0.9], mode="auto"))
    assert out["source"] == "rule" and out["glosses"] == ["PAIN", "HEAD"]


def test_open_rate_limiter_stops_the_storm_and_the_phrase_is_still_normalised_locally(monkeypatch):
    """While Gemini is inside its cooldown window the app must stop calling it AND still produce a sentence."""
    from google import genai
    from server.gemini_gate import GeminiGate
    monkeypatch.setattr(N, "gate", GeminiGate())      # a private gate: this test must not poison the shared one
    monkeypatch.setenv("GEMINI_BACKOFF_S", "0.01")
    monkeypatch.setenv("GEMINI_429_TRIP", "1")
    monkeypatch.setenv("GEMINI_COOLDOWN_S", "60")
    calls = []

    class RateLimited(Exception):
        code = 429                                    # the attribute google.genai.errors.APIError carries

    class FakeModels:
        async def generate_content(self, *a, **k):
            calls.append(1); raise RateLimited("429 Too Many Requests")

    class FakeClient:
        def __init__(self, *a, **k): self.aio = SimpleNamespace(models=FakeModels())

    monkeypatch.setattr(genai, "Client", FakeClient)
    first = asyncio.run(N.normalize(["PAIN", "HEAD"], [0.9, 0.9], mode="auto"))
    assert first["source"] == "rule" and first["glosses"] == ["PAIN", "HEAD"], "the phrase is still spoken, locally"
    assert N.gate.opened, "a rate limit inside the trip window must open the breaker"

    already = len(calls)
    second = asyncio.run(N.normalize(["HEAD", "TOOTH"], [0.9, 0.9], mode="auto"))   # a different phrase, so no cache
    assert second["source"] == "rule" and second["glosses"] == ["HEAD", "TOOTH"]
    assert len(calls) == already, "no further API call while the breaker is open: the storm stops"


def test_gemini_sees_confidences_aligned_after_dedupe(monkeypatch):
    seen = {}

    async def spy(cleaned, confs, context=None):
        seen["pairs"] = list(zip(cleaned, confs))
        seen["context"] = context
        return SimpleNamespace(sentence="Pain, head.", glosses=["PAIN", "HEAD"], dropped=[], uncertain=False, note="")

    monkeypatch.setattr(N, "normalize_gemini", spy)
    asyncio.run(N.normalize(["PAIN", "PAIN", "HEAD", "PAIN"], [0.9, 0.4, 0.8, 0.7], mode="auto"))
    assert seen["pairs"] == [("PAIN", 0.9), ("HEAD", 0.8), ("PAIN", 0.7)]


def test_hung_normalizer_times_out_to_the_rule_pass(monkeypatch):
    async def hang(cleaned, confs, context=None): await asyncio.sleep(30)
    monkeypatch.setattr(N, "normalize_gemini", hang)
    monkeypatch.setenv("NORMALIZER_TIMEOUT", "0.1")
    out = asyncio.run(N.normalize(["PAIN", "HEAD"], [0.9, 0.9], mode="auto"))
    assert out["source"] == "rule", "a hung API must degrade, never freeze the phrase"


def test_uncertainty_and_note_pass_through(monkeypatch):
    monkeypatch.setattr(N, "normalize_gemini", _gemini("Head or tooth?", ["HEAD"], uncertain=True, note="two signs fit"))
    out = asyncio.run(N.normalize(["HEAD"], [0.9], mode="auto"))
    assert out["uncertain"] is True and out["note"] == "two signs fit"


def test_recent_clinician_speech_is_handed_to_the_model_as_context(monkeypatch):
    """The doctor's lines reach Gemini so it can disambiguate -- they are never signed content."""
    seen = {}

    async def spy(cleaned, confs, context=None):
        seen["context"] = context
        return SimpleNamespace(sentence="Pain in my head.", glosses=["PAIN", "HEAD"], dropped=[], uncertain=False, note="")

    monkeypatch.setattr(N, "normalize_gemini", spy)
    out = asyncio.run(N.normalize(["PAIN", "HEAD"], [0.9, 0.9], mode="auto",
                                  context=["Is it the head or the tooth?"]))
    assert seen["context"] == ["Is it the head or the tooth?"]
    assert out["source"] == "gemini"


def test_context_is_never_prompted_into_the_rule_pass(monkeypatch):
    """No model in the loop: context must not change a deterministic sentence."""
    with_ctx = N.normalize_rule(["PAIN", "HEAD"], [0.9, 0.9], context=["Say where it hurts"])
    without = N.normalize_rule(["PAIN", "HEAD"], [0.9, 0.9])
    assert with_ctx == without, "the fallback stays reproducible regardless of clinician speech"


def test_context_is_not_stored_in_the_result(monkeypatch):
    """Nothing the clinician said may leak into glosses/sentence of a degraded (rule) run."""
    out = asyncio.run(N.normalize(["PAIN"], [0.9], mode="rule", context=["Are you allergic to anything?"]))
    assert "allergic" not in out["sentence"].lower()
    assert out["glosses"] == ["PAIN"]


def test_real_prompt_carries_the_clinician_context_and_the_context_only_rule(monkeypatch):
    """The actual request Gemini would get: clinician lines present, marked context-only."""
    from google import genai
    seen = {}

    class FakeModels:
        async def generate_content(self, *a, **k):
            seen["prompt"] = k["contents"]
            seen["system"] = k["config"].system_instruction
            return SimpleNamespace(parsed=SimpleNamespace(
                sentence="Pain in my head.", glosses=["PAIN", "HEAD"], dropped=[], uncertain=False, note=""))

    class FakeClient:
        def __init__(self, *a, **k): self.aio = SimpleNamespace(models=FakeModels())

    monkeypatch.setattr(genai, "Client", FakeClient)
    asyncio.run(N.normalize_gemini(["PAIN", "HEAD"], [0.9, 0.9], context=["Is it the head or the tooth?"]))
    assert "Is it the head or the tooth?" in seen["prompt"]
    assert "CONTEXT ONLY" in seen["prompt"]
    assert "did NOT sign this" in seen["prompt"]
    assert "never copy the clinician" in seen["system"], "the safety rule must be in every normalizer call"

    # without context, no clinician block appears at all
    seen.clear()
    asyncio.run(N.normalize_gemini(["PAIN"], [0.9]))
    assert "clinician" not in seen["prompt"].lower()
