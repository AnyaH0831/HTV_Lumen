"""Offline evaluation harness for the safe auto-speak policy (see docs/AUTONOMY.md).

It runs a set of SCRIPTED scenarios with a known intended meaning through three strategies and
reports comparable numbers:

  raw      speak the detected glosses verbatim, always, with no gate (the naive baseline)
  cleanup  speak the normalizer's sentence (Gemini when configured, the rule pass otherwise), always
  tiered   the real policy (server/policy.py): speak a clear phrase, ask for a risky one, repeat when
           unsure, and never say a word the signs did not support (policy.grounded)

Metrics (lower is better unless noted):
  spoken_on_risky         risky phrases spoken with no read-back at all  (the safety number)
  critical_detail_changed spoken sentences that add an unsupported fact or drop a critical sign
  false_speech_per_idle_min secrets spoken per idle minute (idle scenarios that produce speech)
  needless_asks           clear phrases that were held for approval anyway
  mean_delay_s            modelled delay from the end of the last sign to speech (countdown + ack)
  cancel_rate             fraction of speak-eligible phrases the signer cancelled (input flag)

This is a MODEL of the pipeline, not a measurement of real people: "cleanup" uses the rule normalizer
unless you pass a real one, idle time is synthetic, and cancels are scripted. Use it to compare
strategies honestly, then confirm with real signers.

CLI:  python -m server.eval_policy            (prints a table)
      python -m server.eval_policy --json out.json
"""
import json, re, sys
from . import policy
from .normalize import normalize_rule


def T(g, c, *alts):
    return {"gloss": g, "conf": c, "alts": [{"gloss": a, "conf": p} for a, p in alts]}


# Each scenario:
#   tokens      what the recogniser emitted
#   truth       the intended meaning: "speak" (clear), "ask" (risky), "repeat" (unsure), or "idle" (no sign)
#   intended    the glosses the signer actually meant (for the critical-detail check)
#   critical    critical facts that must not change (subset of intended, or empty)
#   seconds     how long the scenario lasts (for the idle false-speech rate)
#   cancel      the signer cancels during the countdown (scripted)
#   uncertain   the interpreter flagged the phrase as unclear
SCENARIOS = [
    dict(name="clear pain", tokens=[T("PAIN", .95), T("HEAD", .93)], truth="speak", intended=["PAIN", "HEAD"], critical=[], seconds=8),
    dict(name="clear emergency", tokens=[T("EMERGENCY", .95)], truth="speak", intended=["EMERGENCY"], critical=["EMERGENCY"], seconds=8),
    dict(name="clear help", tokens=[T("HELP", .9)], truth="speak", intended=["HELP"], critical=["HELP"], seconds=8),
    dict(name="weak emergency", tokens=[T("EMERGENCY", .6)], truth="ask", intended=["EMERGENCY"], critical=["EMERGENCY"], seconds=8),
    dict(name="weak allergy", tokens=[T("ALLERGIC", .5)], truth="ask", intended=["ALLERGIC"], critical=["ALLERGIC"], seconds=8),
    dict(name="close call fall/head", tokens=[T("PAIN", .9), T("FALL", .5, ("HEAD", .45))], truth="ask", intended=["PAIN", "FALL"], critical=["FALL"], seconds=8),
    dict(name="hot and cold", tokens=[T("HOT", .9), T("COLD", .9)], truth="ask", intended=["HOT", "COLD"], critical=[], seconds=8),
    dict(name="interpreter unsure", tokens=[T("PAIN", .9)], truth="ask", intended=["PAIN"], critical=[], uncertain=True, seconds=8),
    dict(name="too unsure", tokens=[T("PAIN", .3), T("HEAD", .3)], truth="repeat", intended=["PAIN", "HEAD"], critical=[], seconds=8),
    dict(name="nothing recognised", tokens=[], truth="repeat", intended=[], critical=[], seconds=8),
    dict(name="cancelled clear phrase", tokens=[T("WATER", .9)], truth="speak", intended=["WATER"], critical=[], seconds=8, cancel=True),
    dict(name="idle (no sign)", tokens=[], truth="idle", intended=[], critical=[], seconds=60),
    dict(name="idle (faint noise)", tokens=[T("SICK", .3)], truth="idle", intended=["SICK"], critical=[], seconds=60),
]


def _norm(tokens, normalizer, uncertain=False):
    glosses = [t["gloss"] for t in tokens]
    confs = [float(t.get("conf", 1.0)) for t in tokens]
    n = normalizer(glosses, confs) if glosses else dict(sentence="", glosses=[], dropped=[], source="rule", uncertain=False, note="", complete=True)
    if uncertain:
        n = {**n, "uncertain": True, "note": "scripted doubt"}
    return n


def _safe_sentence(sentence, intended, fallback):
    """The tiered strategy's grounding guard: keep the sentence only if every word is sign-supported."""
    if not sentence.strip():
        return ""
    ok, _bad = policy.grounded(sentence, intended)
    return sentence if ok else fallback


def _critical_changed(sentence, intended) -> list[str]:
    """Words that add an unsupported fact, or intended critical signs the sentence dropped."""
    if not sentence.strip():
        return []
    ok, bad = policy.grounded(sentence, intended)              # bad = words no sign supports
    if not ok:
        return bad
    words = set(re.findall(r"[a-z]+", sentence.lower()))
    return [g for g in intended if g in policy.CRITICAL
            and not (set(policy.LEX.get(g, "").split()) & words)]


def evaluate(scenarios=None, normalizer=normalize_rule, speak_delay_s=3.0, ack_s=2.0, autonomy="tiered"):
    """Run the scenarios through the three strategies and return a comparable report.

    `autonomy` defaults to "tiered": this harness measures the SAFE gated policy (see test_eval_policy). Pass "fast" to
    model the demo default, where interpreter doubt and non-critical contradictions are spoken anyway."""
    scenarios = scenarios if scenarios is not None else SCENARIOS
    strategies = ("raw", "cleanup", "tiered")
    blank = lambda: dict(spoken_on_risky=0, critical_detail_changed=0, spoken=0, needless_asks=0,
                         clear=0, idle_speech=0, idle_seconds=0.0, cancel_eligible=0, cancelled=0, delays=[])
    acc = {s: blank() for s in strategies}

    for sc in scenarios:
        tokens, intended = sc["tokens"], sc.get("intended", [])
        norm = _norm(tokens, normalizer, sc.get("uncertain", False))
        sentence = norm["sentence"]
        fallback = normalize_rule(intended, [1.0] * len(intended))["sentence"] if intended else ""
        truth = sc["truth"]
        risky = truth in ("ask", "repeat")
        cancelled = bool(sc.get("cancel"))
        idle = truth == "idle"

        def record(strategy, spoken: bool, text: str, asked: bool):
            a = acc[strategy]
            if idle:
                if spoken and text.strip():
                    a["idle_speech"] += 1
                return
            if truth == "speak":
                a["cancel_eligible"] += 1
            if not spoken or not text.strip():
                return
            a["spoken"] += 1
            if _critical_changed(text, intended):
                a["critical_detail_changed"] += 1
            if risky and not asked:
                a["spoken_on_risky"] += 1
            if cancelled:
                a["cancelled"] += 1
                return                                              # cancelled: nothing was actually heard
            a["delays"].append(speak_delay_s + (ack_s if asked else 0.0))

        # raw and cleanup: always speak (no gate)
        record("raw", True, ", ".join(t["gloss"] for t in tokens), asked=False)
        record("cleanup", True, sentence, asked=False)

        # tiered: the real policy decides, then the grounding guard applies
        pol = policy.assess(tokens, norm, autonomy=autonomy)
        if idle:
            record("tiered", pol["action"] == "speak", sentence, asked=False)
        elif pol["action"] == "speak":
            record("tiered", True, _safe_sentence(sentence, intended, fallback), asked=False)
        elif pol["action"] == "ask":
            if truth == "speak":
                acc["tiered"]["needless_asks"] += 1
            record("tiered", True, _safe_sentence(sentence, intended, fallback), asked=True)
        # repeat: nothing is said; a clear phrase that was repeated is a needless ask too
        elif truth == "speak":
            acc["tiered"]["needless_asks"] += 1

        for s in strategies:
            if idle:
                acc[s]["idle_seconds"] += sc.get("seconds", 0)

    report = {"assumptions": dict(n=len(scenarios), speak_delay_s=speak_delay_s, ack_s=ack_s,
                                  note="Model of the pipeline: cleanup uses the passed normalizer, idle time and cancels are scripted."),
              "strategies": {}}
    for s in strategies:
        a = acc[s]
        idle_min = a["idle_seconds"] / 60.0 or 1.0
        eligible = a["cancel_eligible"] or 1
        report["strategies"][s] = {
            "spoken_on_risky": a["spoken_on_risky"],
            "critical_detail_changed": a["critical_detail_changed"],
            "false_speech_per_idle_minute": round(a["idle_speech"] / idle_min, 3),
            "needless_asks": a["needless_asks"],
            "mean_delay_s": round(sum(a["delays"]) / len(a["delays"]), 2) if a["delays"] else None,
            "cancel_rate": round(a["cancelled"] / eligible, 2),
        }
    return report


def _table(report):
    cols = ["spoken_on_risky", "critical_detail_changed", "false_speech_per_idle_minute", "needless_asks", "mean_delay_s", "cancel_rate"]
    lines = [f"{'strategy':<10} " + "  ".join(f"{c:>10}" for c in cols)]
    for s, m in report["strategies"].items():
        lines.append(f"{s:<10} " + "  ".join(f"{m[c]!s:>10}" for c in cols))
    return "\n".join(lines)


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    report = evaluate()
    print(_table(report))
    print("\n" + report["assumptions"]["note"])
    if "--json" in argv:
        path = argv[argv.index("--json") + 1]
        with open(path, "w") as f:
            json.dump(report, f, indent=2)
        print("json:", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
