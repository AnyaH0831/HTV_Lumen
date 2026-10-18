"""Planners: decide which MCP tools to call, from the normalizer's sentence + the raw glosses.

Talks to the Lumen MCP server ONLY through MCP (list_tools / call_tool).
LLM_PROVIDER = anthropic | gemini | offline.  'offline' is a deterministic planner: the demo must not die
on venue Wi-Fi.  Stage sequencing (normalize -> plan -> spoken read-back -> approve -> act) lives in
orchestrator.py; this module only plans.  Every Gemini call goes through gemini_gate, so a burst of
signing degrades to the offline planner instead of a rate-limit storm.
"""
import json, os
from .state import rt, DEFAULT_GEMINI_MODEL
from .gemini_gate import gate


def gemini_schema(input_schema):
    """MCP/JSON-Schema -> a Gemini function-declaration schema.

    Gemini has no anyOf, so Optional[x] must become nullable: type x + nullable: true.
    Anything unexpected collapses to a permissive string rather than dropping the tool."""
    def conv(s):
        if not isinstance(s, dict): return {"type": "string"}
        s = dict(s)
        if "anyOf" in s:
            opts = [o for o in s["anyOf"] if isinstance(o, dict) and o.get("type") != "null"]
            nullable = len(opts) < len(s["anyOf"])
            rest = {k: v for k, v in s.items() if k != "anyOf"}
            s = {**(opts[0] if len(opts) == 1 else {}), **rest}
            s.setdefault("type", "string")
            if nullable: s["nullable"] = True
        out = {k: s[k] for k in ("type", "format", "description", "nullable", "enum")
               if k in s}
        if "default" in s and s["default"] is not None: out["default"] = s["default"]
        if isinstance(s.get("properties"), dict):
            out["properties"] = {k: conv(v) for k, v in s["properties"].items()}
            if isinstance(s.get("required"), list): out["required"] = list(s["required"])
        if "items" in s: out["items"] = conv(s["items"])
        return out or {"type": "object"}
    return conv(input_schema)

SYSTEM = """You act for a Deaf patient who communicates in ASL through a headset that recognises a small vocabulary of signs.
You receive the confirmed sign glosses (each with a confidence), ONE plain-English sentence an interpreter stage produced from them,
and a POLICY line saying whether the sentence may be spoken automatically or needs the signer's approval first. Rules:
1. Use ONLY what was signed. Never invent symptoms, allergens, durations, doses, severity, negations or pain levels. Unknown fields stay empty.
2. Glosses are keywords, not grammar: interpret conservatively. Base what you say on the interpreter's sentence; do not re-interpret beyond it.
3. Speaking: call speak_to_clinician(text). The signer sees a countdown and can cancel, so do NOT ask for approval first -- unless the POLICY
   line says approval is needed (or the tool returns status "held"): then call request_confirmation with a short plain-English read-back
   (purpose "readback"), and only if confirmed call speak_to_clinician again.
4. After status "spoken": submit_intake, and flag_allergy if ALLERGIC. If the signer cancelled or confirmation was refused or timed out,
   do nothing further and say so.
5. Texting the emergency contact (notify_contact) is only for EMERGENCY or HELP, and ONLY after request_confirmation with purpose
   "send_message" is approved. Never text without it.
6. Report tool outcomes EXACTLY as the tool returns them: if a tool says sent:false, "DRY RUN", held, cancelled or error, then it did
   not happen -- never claim a message was sent, spoken or delivered when the tool says otherwise."""

BODY = {"HEAD", "ARM", "EAR", "EYE", "MOUTH", "TOOTH"}
SYMPTOM = {"PAIN": "pain", "SICK": "feeling sick", "HOT": "fever / feels hot", "COLD": "feels cold", "HUNGRY": "hungry", "THIRSTY": "thirsty", "FALL": "recent fall"}


def build_prompt(norm, tokens, policy=None):
    """What the LLM planners receive: raw glosses with confidence + the normalizer's sentence + the policy decision."""
    line = "Signed glosses in order (with recognition confidence): " + ", ".join(
        f"{t['gloss']} ({t.get('conf', 1):.2f})" for t in tokens)
    line += f"\nInterpreter stage ({norm['source']}) read this as: {norm['sentence']}"
    if norm["dropped"]: line += f"\nDropped as repeats: {', '.join(norm['dropped'])}"
    if norm["uncertain"]: line += "\nThe interpreter is unsure" + (f": {norm['note']}" if norm['note'] else "")
    if policy:
        line += ("\nPOLICY: speak automatically (the signer can cancel); no approval needed before speaking. Do NOT call "
                 "request_confirmation for this phrase -- call speak_to_clinician directly." if policy["action"] == "speak"
                 else "\nPOLICY: approval needed BEFORE speaking. Reasons: " + "; ".join(policy["reasons"]))
        if policy.get("needs_sms"): line += "\nEMERGENCY/HELP was signed: you may offer to text the emergency contact (purpose 'send_message')."
    return line


STEPS = 0                    # tool calls made by the current planner run (reset by the orchestrator)


async def call(session, name, args):
    global STEPS; STEPS += 1
    await rt.emit("agent_log", text=f"{name}({json.dumps(args, ensure_ascii=False)[:110]})")
    res = await session.call_tool(name, args)
    text = "".join(getattr(c, "text", "") for c in res.content)
    await rt.emit("agent_log", text=("error: " if res.isError else "-> ") + text[:110])
    try: return json.loads(text)
    except Exception: return {"raw": text}


async def plan_offline(session, norm, policy=None):
    """Deterministic planner: same tools, same guards. Speaks the normalizer's sentence automatically unless the policy says ask."""
    g = norm["glosses"]; S = set(g)
    body = next((b for b in g if b in BODY), ""); symptoms = [SYMPTOM[x] for x in g if x in SYMPTOM]
    urgency = "emergency" if "EMERGENCY" in S else "urgent" if S & {"HELP", "FALL", "DOCTOR"} else "routine"
    complaint = ", ".join(symptoms) or "unspecified (see signed words)"
    sentence = norm["sentence"].rstrip(".")
    sms = bool(S & {"EMERGENCY", "HELP"}); sms_ok = False
    if policy and policy["action"] == "ask":
        q = "I understood: " + sentence + ". Is this right?" + (" If yes, I will also text your emergency contact." if sms else "")
        ok = (await call(session, "request_confirmation", dict(summary=q, purpose="send_message" if sms else "readback"))).get("confirmed")
        if not ok: return "Not confirmed: nothing was said."
        sms_ok = sms
    r = await call(session, "speak_to_clinician", dict(text=sentence + "."))
    if r.get("status") == "cancelled": return "Cancelled by the signer: nothing was said or saved."
    if r.get("status") != "spoken": return f"Not spoken ({r.get('status')}): {r.get('reason', '')}"
    await call(session, "submit_intake", dict(chief_complaint=complaint, body_location=body.lower(), symptoms=symptoms, urgency=urgency))
    if "ALLERGIC" in S: await call(session, "flag_allergy", dict(substance="unspecified"))
    if sms:
        if not sms_ok:
            sms_ok = bool((await call(session, "request_confirmation", dict(summary="Send a text message to your emergency contact?", purpose="send_message"))).get("confirmed"))
        if sms_ok: await call(session, "notify_contact", dict(message="Emergency help requested. " + sentence + "."))
    return "Done."


async def plan_anthropic(session, prompt):
    from anthropic import AsyncAnthropic
    client, model = AsyncAnthropic(), os.getenv("AGENT_MODEL", "claude-sonnet-5-5")
    tools = [{"name": t.name, "description": t.description or "", "input_schema": t.inputSchema} for t in (await session.list_tools()).tools]
    msgs = [{"role": "user", "content": prompt}]
    for _ in range(8):
        r = await client.messages.create(model=model, max_tokens=1024, system=SYSTEM, tools=tools, messages=msgs)
        msgs.append({"role": "assistant", "content": r.content})
        if r.stop_reason != "tool_use": return "".join(b.text for b in r.content if b.type == "text")
        results = []
        for b in r.content:
            if b.type == "tool_use":
                out = await call(session, b.name, b.input)
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": json.dumps(out)})
        msgs.append({"role": "user", "content": results})
    return "Stopped after 8 steps."


async def plan_gemini(session, prompt):
    """Gemini planner: an explicit function-call loop (MCP tools in, MCP calls out), same shape as
    plan_anthropic. Passing the MCP session as config tools= to google-genai deepcopies it and dies
    on its asyncio Future, so the session is only ever touched through list_tools/call_tool."""
    from google import genai
    from google.genai import types
    client = genai.Client()
    tools = [types.Tool(function_declarations=[{
        "name": t.name, "description": t.description or "", "parameters": gemini_schema(t.inputSchema)}])
        for t in (await session.list_tools()).tools]
    contents = [types.Content(role="user", parts=[types.Part(text=prompt)])]
    for _ in range(8):
        r = await gate(lambda: client.aio.models.generate_content(        # through the gate: retry/backoff/breaker
            model=os.getenv("GEMINI_MODEL", DEFAULT_GEMINI_MODEL), contents=contents,
            config=types.GenerateContentConfig(system_instruction=SYSTEM, tools=tools)))
        cand = (r.candidates or [None])[0]
        if cand is None or not cand.content: return (r.text or "").strip() or "Done."
        parts = cand.content.parts or []
        fcs = [p.function_call for p in parts if p.function_call is not None]
        if not fcs:
            contents.append(cand.content)
            return "".join(p.text for p in parts if p.text) or "Done."
        contents.append(cand.content)
        replies = []
        for fc in fcs:                           # every outward step still goes through MCP
            out = await call(session, fc.name, dict(fc.args or {}))
            replies.append(types.Part.from_function_response(name=fc.name, response=out))
        contents.append(types.Content(role="user", parts=replies))
    return "Stopped after 8 steps."

