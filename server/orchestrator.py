"""The orchestrator: one signed phrase in, approved actions out.

    assemble -> normalize (Gemini -> one plain-English sentence, rule fallback)
             -> plan     (anthropic | gemini | offline planner, offline fallback)
             -> confirm  (read-back SPOKEN to the signer, then they approve)
             -> act      (speak_to_clinician / notify_contact, guarded in tool code)

Every stage can fail on its own: losing Gemini or the LLM degrades the WORDS, never the
safety gate. The normalizer never calls tools, the planner never speaks for the signer, and
`notify_contact` still refuses in code without an approved read-back.

Stage events (`stage`, `normalized`) are broadcast so the dashboard and headset can show
where the phrase is, instead of a silent hang.
"""
import os
from .state import rt
from .normalize import normalize, normalize_rule
from . import agent, policy

PLANNERS = {"anthropic": agent.plan_anthropic, "gemini": agent.plan_gemini}


def _key(tokens):
    return (tuple((t["gloss"], round(float(t.get("conf", 1.0)), 3)) for t in tokens), tuple(rt.doctor))


async def normalize_cached(tokens):
    """normalize() once per distinct phrase: the 'is the signer finished?' check and the pipeline share one Gemini call."""
    key = _key(tokens)
    if rt.norm_cache and rt.norm_cache[0] == key: return rt.norm_cache[1]
    norm = await normalize([t["gloss"] for t in tokens], [float(t.get("conf", 1.0)) for t in tokens], context=list(rt.doctor))
    rt.norm_cache = (key, norm); return norm


async def run_pipeline(tokens):
    session = rt.mcp_session
    rt.phrase_id += 1; rt.phrase_tokens = list(tokens); rt.policy = None
    rt.store.log("phrase", {"phrase_id": rt.phrase_id, "tokens": tokens})
    glosses = [t["gloss"] for t in tokens]
    confs = [float(t.get("conf", 1.0)) for t in tokens]
    try:
        # --- stage 1: understand what was signed ------------------------------------
        await rt.emit("stage", stage="normalize")
        try:
            norm = await normalize_cached(tokens)
        except Exception as e:                    # rule pass itself broke: last-ditch local pass
            await rt.emit("agent_log", text=f"Normalizer failed ({type(e).__name__}); using rule pass")
            norm = normalize_rule(glosses, confs)
        await rt.emit("normalized", phrase_id=rt.phrase_id, sentence=norm["sentence"], glosses=norm["glosses"],
                      dropped=norm["dropped"], source=norm["source"], uncertain=norm["uncertain"], note=norm["note"])
        await rt.emit("agent_log", text=f"Normalizer: {norm['source']} -> {norm['sentence']}"
                      + (f" (dropped: {', '.join(norm['dropped'])})" if norm["dropped"] else "")
                      + (f" -- fell back: {norm['error']}" if norm.get("error") else ""))

        # --- stage 1b: policy -- speak on its own, ask first, or ask the signer to sign again (decided in code) ----
        pol = policy.assess(tokens, norm)
        pol.update(words=norm["glosses"], raw=glosses, fallback_sentence=normalize_rule(glosses, confs)["sentence"])
        rt.policy = pol
        await rt.emit("policy", phrase_id=rt.phrase_id, action=pol["action"], reasons=pol["reasons"], advisories=pol.get("advisories", []))
        await rt.emit("agent_log", text=f"Policy: {pol['action']}" + (f" ({'; '.join(pol['reasons'])})" if pol["reasons"] else "")
                      + (f" [spoken anyway: {'; '.join(pol['advisories'])}]" if pol.get("advisories") else ""))
        if pol["action"] == "repeat":
            await rt.emit("notice", text=pol["say_again"]); await rt.emit("agent_log", text="Asked the signer to sign again.")
            return

        # --- stage 2: decide what to do about it ------------------------------------
        await rt.emit("stage", stage="plan")
        provider = os.getenv("LLM_PROVIDER") or ("anthropic" if os.getenv("ANTHROPIC_API_KEY") else "offline")
        await rt.emit("agent_log", text=f"Planner: {provider}")
        agent.STEPS = 0
        try:
            if provider == "offline":
                out = await agent.plan_offline(session, norm, pol)
            else:
                plan = PLANNERS[provider]
                try:
                    out = await plan(session, agent.build_prompt(norm, tokens, pol))
                except Exception as e:
                    if agent.STEPS: raise        # already acted: replaying would duplicate those calls
                    await rt.emit("agent_log", text=f"{provider} failed once ({type(e).__name__}: "
                                  f"{str(e)[:140]}); retrying once")
                    agent.STEPS = 0
                    out = await plan(session, agent.build_prompt(norm, tokens, pol))
        except Exception as e:
            if agent.STEPS:
                # it already called tools: restarting would duplicate those actions, so stop instead
                await rt.emit("agent_log", text=f"{provider} failed mid-run ({type(e).__name__}: {str(e)[:140]}) after "
                              f"{agent.STEPS} tool call(s); NOT restarting -- review the trace")
                out = "Stopped: the planner failed after acting. Nothing further was done."
            else:
                await rt.emit("agent_log", text=f"{provider} failed ({type(e).__name__}: {str(e)[:140]}); "
                              f"falling back to offline planner")
                out = await agent.plan_offline(session, norm, pol)
        await rt.emit("agent_log", text=out or "Done.")
    finally:
        # stage 3 is inside request_confirmation (spoken read-back + signer approval);
        # clients always get a terminal event, whatever broke above.
        rt.policy = None
        await rt.emit("stage", stage="done")
        await rt.emit("agent_done")
