"""When may a recognised phrase be spoken automatically? Pure functions: no I/O, no LLM, fully unit-tested.

Three outcomes, decided in CODE (not in a prompt) after the normalizer has run:

  speak   say it aloud after a short cancel countdown, with no approval click
  ask     the signer approves a spoken read-back first (they can answer by signing YES / NO)
  repeat  too unsure to say anything: ask the signer to sign again

`ask` is chosen when a mistake would be costly or the system is unsure: the interpreter flagged the phrase as unclear, a
critical word (EMERGENCY, ALLERGIC, HELP, FALL, POLICE, MEDICINE) is a close call or was recognised weakly, or the words
contradict each other (YES+NO, HOT+COLD, BAD+BETTER). Texting anyone is never automatic: that is a separate approval.

Two autonomy levels (LUMEN_AUTONOMY, default `fast`; pass autonomy= to override):
  fast    speak every phrase that is not gated by a CRITICAL word. Interpreter doubt and non-critical contradictions are
          SPOKEN anyway (recorded in `advisories`), so the voice comes out with minimal delay. Only a critical word that is
          a close call or was recognised weakly still asks. This is the demo default.
  tiered  the original safe default: any interpreter doubt or contradiction also asks first. Use for a clinic setting.

Also here: a grounding check. Whatever a planner wants to SAY may only use words the signs support, so an LLM cannot
slip in a duration, severity, negation or cause that nobody signed."""
import os
import re
import yaml
from .state import ROOT

CRITICAL = frozenset({"EMERGENCY", "ALLERGIC", "HELP", "FALL", "POLICE", "MEDICINE"})
CONTRADICTIONS = ({"YES", "NO"}, {"HOT", "COLD"}, {"BAD", "BETTER"})
SMS_WORDS = frozenset({"EMERGENCY", "HELP"})      # phrases for which texting the emergency contact is offered
MIN_ALT = 0.10          # a runner-up needs at least this probability ...
ALT_RATIO = 0.35        # ... and this fraction of the top word's probability to count as a candidate
CLOSE_CALL = 0.5        # runner-up >= 50% of the top probability is a close call
LOW_MEAN = 0.45         # mean confidence below this: ask the signer to sign again
CRIT_MIN_CONF = 0.80    # a critical word recognised below this confidence needs approval


def label(g: str) -> str: return g.lower().replace("_", " ")


def autonomy_level(explicit: str | None = None) -> str:
    """'fast' (default) or 'tiered'. Explicit wins; otherwise LUMEN_AUTONOMY; unknown values fall back to fast."""
    return "tiered" if (explicit or os.getenv("LUMEN_AUTONOMY", "fast")).strip().lower() == "tiered" else "fast"


def low_mean() -> float:
    try: return float(os.getenv("LUMEN_LOW_MEAN", "") or LOW_MEAN)
    except ValueError: return LOW_MEAN


def candidates(tok: dict) -> list[tuple[str, float]]:
    """[(gloss, p)] best first: the chosen word plus plausible runner-ups the recogniser reported."""
    top = (tok["gloss"], float(tok.get("conf", 1.0)))
    alts = sorted(((a["gloss"], float(a.get("conf", 0))) for a in tok.get("alts", []) if a.get("gloss") and a["gloss"] != top[0]), key=lambda x: -x[1])
    return [top] + [a for a in alts if a[1] >= max(MIN_ALT, ALT_RATIO * top[1])][:2]


def _conf_of(tokens, gloss) -> float:
    return max((float(t.get("conf", 1.0)) for t in tokens if t.get("gloss") == gloss), default=0.0)


def assess(tokens: list[dict], norm: dict, autonomy: str | None = None) -> dict:
    """tokens: [{gloss, conf, alts?, tentative?}] as recognised; norm: the normalizer's result. Returns the decision.

    `advisories` are the doubts that were SPOKEN anyway under fast autonomy; `reasons` are the doubts that still gate a
    phrase (empty -> action 'speak')."""
    fast = autonomy_level(autonomy) == "fast"
    words = list(norm.get("glosses") or [])
    if not words:
        return {"action": "repeat", "reasons": ["no words recognised"], "hedge": "", "mean_conf": 0.0, "needs_sms": False,
                "advisories": [], "say_again": "I did not catch any words. Please sign again."}
    confs = [_conf_of(tokens, w) for w in words]; mean = sum(confs) / len(confs)
    needs_sms = bool(set(words) & SMS_WORDS)
    if mean < low_mean():
        return {"action": "repeat", "reasons": [f"low confidence ({mean:.2f})"], "hedge": "", "mean_conf": mean, "needs_sms": needs_sms,
                "advisories": [], "say_again": "I was not sure what you signed. Please sign again, a little slower."}
    critical_signed = bool(set(words) & CRITICAL)
    reasons, close, advisories = [], [], []
    # Interpreter doubt only gates a phrase in tiered mode, or when a critical word is on the line.
    if norm.get("uncertain"):
        note = "the interpreter was unsure" + (f": {norm['note']}" if norm.get("note") else "")
        if critical_signed or not fast:
            reasons.append(note)
        else:
            advisories.append(note + " (spoken anyway)")
    for t in tokens:
        c = candidates(t)
        if len(c) > 1 and c[1][1] >= CLOSE_CALL * c[0][1]:
            pair = (c[0][0], c[1][0])
            if set(pair) & CRITICAL: reasons.append(f"unsure between {label(pair[0])} and {label(pair[1])}")
            elif t.get("gloss") in words and pair not in close and pair[::-1] not in close: close.append(pair)
    reasons += [f"{label(w)} recognised with low confidence" for w, c in zip(words, confs) if w in CRITICAL and c < CRIT_MIN_CONF]
    for s in CONTRADICTIONS:
        if s <= set(words):
            phrase = f"{' and '.join(sorted(label(x) for x in s))} together"
            (advisories if fast else reasons).append(phrase if not fast else phrase + " (spoken anyway)")
    hedge = " ".join(f"Not sure if that was {label(a)} or {label(b)}." for a, b in close)
    return {"action": "ask" if reasons else "speak", "reasons": list(dict.fromkeys(reasons)), "hedge": "" if reasons else hedge,
            "mean_conf": mean, "needs_sms": needs_sms, "advisories": list(dict.fromkeys(advisories)), "say_again": ""}


# ---------------------------------------------------------------- grounding
FUNCTION_WORDS = frozenset("""i my me am is are was a an the in on at to of and or have has had having feel feels feeling need needs want wants
it this that with about here there maybe possibly unsure uncertain which patient signed signing says said reports reporting their his her they he she
would like asks asking requests requesting also some""".split())
LEX = {"PAIN": "pain painful hurt hurts hurting ache aches", "SICK": "sick ill unwell", "HOT": "hot", "COLD": "cold",
       "FALL": "fall fell fallen falling", "HUNGRY": "hungry", "THIRSTY": "thirsty", "BAD": "bad", "BETTER": "better",
       "WATER": "water", "DRINK": "drink drinking", "MEDICINE": "medicine medication", "DOCTOR": "doctor", "HELP": "help",
       "EMERGENCY": "emergency", "POLICE": "police", "PHONE": "phone call", "ALLERGIC": "allergy allergic allergies",
       "WHERE": "where", "YES": "yes", "NO": "no", "HELLO": "hello hi", "PLEASE": "please", "THANK_YOU": "thank thanks you",
       "HEAD": "head", "ARM": "arm", "EAR": "ear", "EYE": "eye", "MOUTH": "mouth", "TOOTH": "tooth teeth"}
COMPOUNDS = {frozenset({"PAIN", "HEAD"}): "headache", frozenset({"PAIN", "TOOTH"}): "toothache", frozenset({"PAIN", "EAR"}): "earache"}
FIXED_NOTES = ("(substance not specified)",)


def _vocab_words() -> dict[str, set[str]]:
    out = {}
    for s in yaml.safe_load(open(ROOT / "vocab" / "vocab.yaml"))["signs"]:
        out[s["gloss"]] = set(re.findall(r"[a-z]+", str(s.get("phrase", "")).lower()))
    return out


_VOCAB = _vocab_words()


def grounded(text: str, glosses: list[str]) -> tuple[bool, list[str]]:
    """(ok, offending words). Digits, negations ('not', "don't"), intensifiers, durations and unsupported words all fail."""
    allowed = set(FUNCTION_WORDS)
    for g in glosses:
        allowed |= set(LEX.get(g, "").split()) | _VOCAB.get(g, set())
    for pair, word in COMPOUNDS.items():
        if pair <= set(glosses): allowed.add(word)
    t = text.lower()
    for note in FIXED_NOTES: t = t.replace(note, "")
    bad = [w for w in re.findall(r"[a-z0-9']+", t) if w not in allowed]
    return (not bad), bad
