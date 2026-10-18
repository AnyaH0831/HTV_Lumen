"""Pipeline stage: turn the gloss stream into ONE plain-English sentence.

The recogniser emits keyword glosses in order -- sometimes the same sign fires twice
(segmenter bounce), sometimes a sign is wrong, and glosses are keywords rather than grammar.
Gemini reorganises them into a sentence a clinician can read. A deterministic rule pass does
the same job when there is no key / no Wi-Fi (the demo must not die on venue Wi-Fi).

Scope limit, enforced in CODE and not only in the prompt:
  * this stage only interprets LANGUAGE -- it never calls MCP tools and never decides actions
    (that is the planner's job, agent.py), and the signer still approves a read-back;
  * a gloss recognised with confidence >= HARD_DROP_MIN can never be dropped by a normaliser,
    so a high-confidence ALLERGIC / EMERGENCY cannot be talked out of the record;
  * anything a normaliser returns that was not actually signed is discarded.
"""
import asyncio
import os
import yaml
from .state import ROOT, rt, DEFAULT_GEMINI_MODEL
from .gemini_gate import gate

HARD_DROP_MIN = 0.7        # a gloss at/above this confidence may never be dropped downstream

PHRASE = {s["gloss"]: s["phrase"].split(" / ")[0]
          for s in yaml.safe_load(open(ROOT / "vocab" / "vocab.yaml"))["signs"]}

SYSTEM = """You clean up what a Deaf patient signed in ASL, given the recognised glosses.
Turn them into ONE short plain-English sentence a clinician can read aloud.
Rules:
1. Use ONLY the glosses given. Never invent symptoms, allergens, durations, doses, pain levels or body parts.
   Unknown information stays out of the sentence.
2. Glosses are keywords, not grammar: order them into readable English, conservatively.
3. Do not add politeness, diagnoses or medical conclusions that were not signed.
4. Only drop a gloss you are sure was a spurious repeat. If you are unsure about a gloss, keep it and
   set uncertain=true instead of removing it.
5. Keep glosses verbatim (UPPERCASE, underscored) in `glosses`; put removed repeats in `dropped`.
6. If the glosses are contradictory or unclear, set uncertain=true and explain in `note` rather than guessing.
7. You may be told what the clinician said recently. That is CONTEXT ONLY -- the signer did not sign it.
   Use it to pick between readings of an ambiguous gloss (e.g. whether BODY means the aching body or a
   body of water), and never copy the clinician's words, questions or facts into the sentence.
8. Set complete=false ONLY if the signer very likely has more to say (for example a lone body part with no symptom, a lone PLEASE,
   or WHERE with nothing it refers to). A short message that stands on its own, such as PAIN or YES, is complete."""


def assemble(glosses, confs):
    """Local pre-pass: collapse consecutive repeats of the same sign.
    Returns (kept, kept_confs, dropped) -- confidences travel with their gloss, so a drop
    never shifts a later gloss onto someone else's confidence."""
    kept, kept_confs, dropped = [], [], []
    for g, c in zip(glosses, confs):
        if kept and g == kept[-1]: dropped.append(g)
        else: kept.append(g); kept_confs.append(c)
    return kept, kept_confs, dropped


def normalize_rule(glosses, confs, context=None):
    """Deterministic fallback: dedupe, map each gloss to its vocab phrase, join. No invention.

    `context` (the clinician's recent lines) is deliberately ignored here -- a fixed rule set
    cannot weigh it safely, and the fallback must stay reproducible."""
    kept, _kept_confs, dropped = assemble(glosses, confs)
    words = [PHRASE.get(g, g.replace("_", " ").lower()) for g in kept]
    sentence = ", ".join(words)
    sentence = (sentence or "nothing recognised").strip()
    sentence = sentence[0].upper() + sentence[1:] + "." if sentence != "nothing recognised" else "Nothing was recognised."
    return dict(sentence=sentence, glosses=kept, dropped=dropped, source="rule", uncertain=False, note="", complete=True)


async def normalize_gemini(cleaned, confs, context=None):
    """One structured-output Gemini call. Raises on anything unusable; caller falls back to the rule pass.

    `context`: recent clinician lines, offered as context only (SYSTEM rule 7)."""
    from google import genai
    from google.genai import types
    from pydantic import BaseModel

    class Cleaned(BaseModel):
        sentence: str
        glosses: list[str]
        dropped: list[str] = []
        uncertain: bool = False
        note: str = ""
        complete: bool = True

    client = genai.Client()                      # reads GEMINI_API_KEY / GOOGLE_API_KEY; raises without one
    prompt = "Recognised glosses, in order, with confidence:\n" + "\n".join(
        f"- {g} ({c:.2f})" for g, c in zip(cleaned, confs))
    if context:                                   # clinician speech: context only, never signed content
        prompt += ("\nThe clinician said recently (CONTEXT ONLY -- the signer did NOT sign this):\n"
                   + "\n".join(f"- {t}" for t in context))
    r = await gate(lambda: client.aio.models.generate_content(            # through the gate: retry/backoff/breaker
        model=os.getenv("GEMINI_MODEL", DEFAULT_GEMINI_MODEL), contents=prompt,
        config=types.GenerateContentConfig(system_instruction=SYSTEM, response_mime_type="application/json",
                                           response_schema=Cleaned)))
    out = getattr(r, "parsed", None)
    if out is None or not str(getattr(out, "sentence", "")).strip():
        raise ValueError("normalizer returned no usable sentence")
    return out


async def normalize(glosses, confs, mode=None, context=None):
    """glosses/confs -> dict(sentence, glosses, dropped, source, uncertain, note).

    mode: auto (Gemini if it works, else rule) | gemini (Gemini, rule if it fails) | rule.
    context: recent clinician lines (rt.doctor) -- handed to Gemini only, ignored by the rule pass.
    """
    mode = (mode or os.getenv("NORMALIZER", "auto")).lower()
    cleaned, cleaned_confs, local_dropped = assemble(glosses, confs)
    out = None; failed = None
    if mode in ("auto", "gemini") and cleaned:
        try:
            # bounded: a hung / retrying API must degrade to rules, never freeze the phrase
            out = await asyncio.wait_for(normalize_gemini(cleaned, cleaned_confs, context),
                                         timeout=float(os.getenv("NORMALIZER_TIMEOUT", "15")))
        except Exception as e:
            # never fatal: the demo must run with no key and no Wi-Fi
            failed = f"{type(e).__name__}: {str(e)[:200]}"
            rt.store.log("normalize_error", {"mode": mode, "error": failed})
    if out is None:
        rule = normalize_rule(glosses, confs)
        if failed: rule["error"] = failed          # so the trace can say WHY it fell back
        return rule

    # --- code-level guards on whatever the model returned -------------------------------
    high = {g for g, c in zip(glosses, confs) if c >= HARD_DROP_MIN}
    signed = set(cleaned)
    dropped = list(dict.fromkeys(d for d in (getattr(out, "dropped", None) or []) if d in signed and d not in high))
    kept = [g for g in cleaned if g not in set(dropped)]
    sentence = str(out.sentence).strip()
    if not sentence:                              # never hand an empty sentence downstream
        return normalize_rule(glosses, confs)
    if sentence[-1] not in ".!?": sentence += "."
    return dict(sentence=sentence, glosses=kept, dropped=local_dropped + dropped, source="gemini",
                uncertain=bool(getattr(out, "uncertain", False)), note=str(getattr(out, "note", "") or "").strip(),
                complete=bool(getattr(out, "complete", True)))
