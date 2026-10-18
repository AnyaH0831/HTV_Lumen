import random
import yaml
from server.policy import assess, candidates, grounded, CRITICAL
from server.state import ROOT

GL = [s["gloss"] for s in yaml.safe_load(open(ROOT / "vocab" / "vocab.yaml"))["signs"]]
def T(g, c, *alts, tentative=False): return {"gloss": g, "conf": c, "alts": [{"gloss": a, "conf": p} for a, p in alts], **({"tentative": True} if tentative else {})}
def N(*glosses, uncertain=False, note=""): return {"glosses": list(glosses), "uncertain": uncertain, "note": note}


def test_clear_noncritical_phrase_is_spoken_without_asking():
    r = assess([T("PAIN", .93), T("HEAD", .9)], N("PAIN", "HEAD")); assert (r["action"], r["reasons"], r["hedge"]) == ("speak", [], "")


def test_a_clear_emergency_is_spoken_at_once_but_texting_is_flagged_for_its_own_approval():
    r = assess([T("EMERGENCY", .95)], N("EMERGENCY")); assert r["action"] == "speak" and r["needs_sms"] is True


def test_tiered_interpreter_doubt_critical_close_calls_weak_critical_words_and_contradictions_all_ask_first():
    assert assess([T("PAIN", .9)], N("PAIN", uncertain=True, note="unclear"), autonomy="tiered")["action"] == "ask"
    r = assess([T("PAIN", .9), T("FALL", .5, ("HEAD", .45))], N("PAIN", "FALL"), autonomy="tiered"); assert r["action"] == "ask" and "fall" in r["reasons"][0] and "head" in r["reasons"][0]
    assert assess([T("EMERGENCY", .6)], N("EMERGENCY"), autonomy="tiered")["action"] == "ask"
    assert assess([T("ALLERGIC", .79)], N("ALLERGIC"), autonomy="tiered")["action"] == "ask" and assess([T("ALLERGIC", .81)], N("ALLERGIC"), autonomy="tiered")["action"] == "speak"
    assert assess([T("HOT", .9), T("COLD", .9)], N("HOT", "COLD"), autonomy="tiered")["action"] == "ask"
    assert assess([T("YES", .9), T("NO", .9)], N("YES", "NO"), autonomy="tiered")["action"] == "ask"


def test_fast_autonomy_speaks_interpreter_doubt_and_noncritical_contradictions_but_records_them():
    """The demo default: only CRITICAL doubt still asks. Non-critical doubt is spoken, listed in `advisories`."""
    r = assess([T("PAIN", .9)], N("PAIN", uncertain=True, note="unclear")); assert r["action"] == "speak" and r["advisories"] and not r["reasons"]
    r = assess([T("HOT", .9), T("COLD", .9)], N("HOT", "COLD")); assert r["action"] == "speak" and any("hot" in a for a in r["advisories"])
    # a critical word keeps the gate even in fast mode
    assert assess([T("EMERGENCY", .6)], N("EMERGENCY"))["action"] == "ask"
    assert assess([T("PAIN", .9), T("FALL", .5, ("HEAD", .45))], N("PAIN", "FALL"))["action"] == "ask"
    # interpreter doubt on a phrase that ALSO has a critical word still asks in fast mode
    assert assess([T("ALLERGIC", .95)], N("ALLERGIC", uncertain=True))["action"] == "ask"


def test_low_confidence_or_nothing_recognised_asks_the_signer_to_sign_again():
    for r in (assess([T("PAIN", .3), T("HEAD", .3)], N("PAIN", "HEAD")), assess([], N())):
        assert r["action"] == "repeat" and "sign again" in r["say_again"].lower()


def test_a_noncritical_close_call_is_spoken_with_an_honest_hedge():
    r = assess([T("PAIN", .9), T("EAR", .5, ("EYE", .47), tentative=True)], N("PAIN", "EAR"))
    assert r["action"] == "speak" and r["hedge"] == "Not sure if that was ear or eye."


def test_a_runner_up_far_below_the_top_is_not_a_candidate():
    assert candidates(T("PAIN", .9, ("EAR", .2))) == [("PAIN", .9)]


def test_grounding_allows_natural_wording_and_blocks_invented_facts():
    assert grounded("I have pain in my head.", ["PAIN", "HEAD"])[0] and grounded("The patient has a headache.", ["PAIN", "HEAD"])[0]
    assert grounded("Pain, head.", ["PAIN", "HEAD"])[0]
    for lie in ("I have severe pain in my head.", "I have pain in my head for 3 days.", "I do not have pain in my head.",
                "I have pain in my head and arm.", "The patient has a headache.", "I have a fever."):
        glosses = ["PAIN", "HEAD"] if "headache" not in lie else ["PAIN"]
        assert not grounded(lie, glosses)[0], lie
    assert grounded("I have an allergy (substance not specified).", ["ALLERGIC"])[0] and not grounded("I do not have an allergy.", ["ALLERGIC"])[0]


def test_fuzz_never_speaks_automatically_on_a_risky_phrase():
    """Both autonomy levels must never auto-speak a WEAK CRITICAL word or a CRITICAL close call. Tiered is stricter
    (it also never auto-speaks interpreter doubt or a non-critical contradiction)."""
    rng = random.Random(3)
    for _ in range(2000):
        toks = []
        for _ in range(rng.randint(1, 6)):
            g = rng.choice(GL); alts = [(a, round(rng.uniform(.05, .6), 2)) for a in rng.sample(GL, rng.randint(0, 3)) if a != g]
            toks.append(T(g, round(rng.uniform(.2, 1), 2), *alts))
        words = list(dict.fromkeys(t["gloss"] for t in toks))
        for mode in ("tiered", "fast"):
            r = assess(toks, N(*words), autonomy=mode)
            if r["action"] != "speak": continue
            for w in words:
                if w in CRITICAL: assert max(t["conf"] for t in toks if t["gloss"] == w) >= .8, (mode, toks, w)
            for t in toks:
                c = candidates(t); assert not (len(c) > 1 and c[1][1] >= .5 * c[0][1] and {c[0][0], c[1][0]} & CRITICAL), (mode, toks)
            if mode == "tiered":
                for s in ({"YES", "NO"}, {"HOT", "COLD"}, {"BAD", "BETTER"}): assert not s <= set(words), toks
