"""Lumen MCP server. Any MCP client can (a) read what the signer said and (b) act on it through guarded tools.

Run standalone (stdio) for Claude Desktop / other clients:  python -m server.mcp_server
The web app connects to the SAME server object in-process (see app.py), so both paths exercise identical tools.

Safety invariants (enforced in code, not in the prompt):
  * speak_to_clinician follows the policy decided in policy.py: a clear phrase is spoken after a cancel countdown with no
    approval click; a risky one (critical word, contradiction, interpreter doubt) is HELD until the signer approves a read-back.
    Whatever is spoken may only use words the signs support (policy.grounded); otherwise the plain fallback sentence is spoken.
  * notify_contact refuses to run unless the signer approved sending a message for the current phrase
    (request_confirmation with purpose "send_message"). Approving a read-back alone does not unlock it."""
import asyncio, json, os, uuid
from pathlib import Path
from mcp.server.fastmcp import FastMCP
from .state import rt, DATA, ROOT
from .tts import speak
from . import notify, policy, speech

mcp = FastMCP("lumen")
URGENCY = {"routine", "urgent", "emergency"}


@mcp.tool()
def get_signed_phrase() -> dict:
    """Return the words the signer has confirmed on the headset for the current phrase, in order, with confidence."""
    toks = rt.phrase_tokens or rt.tokens or (rt.store.recent("phrase", 1) or [{}])[0].get("tokens", [])
    return {"phrase_id": rt.phrase_id, "tokens": toks, "note": "Glosses are the ONLY information the signer provided."}


@mcp.tool()
def submit_intake(chief_complaint: str, body_location: str = "", symptoms: list[str] | None = None,
                  pain_level: int | None = None, urgency: str = "routine") -> dict:
    """Record a clinic intake note. Only include facts that appear in the signed phrase; leave unknown fields empty."""
    if urgency not in URGENCY: return {"error": f"urgency must be one of {sorted(URGENCY)}"}
    if pain_level is not None and not 0 <= pain_level <= 10: return {"error": "pain_level must be 0-10"}
    rec = dict(id=uuid.uuid4().hex[:8], phrase_id=rt.phrase_id, chief_complaint=chief_complaint, body_location=body_location,
               symptoms=symptoms or [], pain_level=pain_level, urgency=urgency)
    rt.store.log("intake", rec); return {"saved": True, **rec}


@mcp.tool()
def flag_allergy(substance: str, reaction: str = "unknown") -> dict:
    """Flag an allergy on the patient record. If the signer did not specify the substance, use 'unspecified'."""
    rec = dict(phrase_id=rt.phrase_id, substance=substance, reaction=reaction); rt.store.log("allergy", rec); return {"flagged": True, **rec}


@mcp.tool()
async def speak_to_clinician(text: str) -> dict:
    """Say a short plain-English sentence aloud for the patient. The signer sees a countdown and can cancel, so do NOT ask for approval first.
    Returns status: spoken | cancelled | held | busy. 'held' means the phrase is risky (critical word, contradiction, doubt): call
    request_confirmation with a plain read-back, then call this tool again."""
    pol = rt.policy
    if rt.hub is None or pol is None:                 # standalone / stdio use, or no pipeline run: nothing to stage
        audio = await speak(text); await rt.emit("caption_out", text=text); rt.store.log("speak", {"text": text, "audio": audio["audio"]})
        return {"spoken": True, "status": "spoken", **audio}
    if pol["action"] == "ask" and rt.phrase_id not in rt.readback_ok:
        return {"spoken": False, "status": "held", "reason": "; ".join(pol["reasons"]),
                "next": "call request_confirmation with a plain read-back (purpose 'readback'), then call speak_to_clinician again"}
    ok, bad = policy.grounded(text, pol["words"]); replaced = not ok
    if replaced: text = pol["fallback_sentence"]      # an LLM sentence with words nobody signed is never spoken
    if pol.get("hedge"): text = text.rstrip() + " " + pol["hedge"]
    r = await speech.stage(text, pol["words"], pol["raw"], notes=[f"sentence replaced: unsupported words {', '.join(bad)}"] if replaced else [])
    return {"spoken": r["status"] == "spoken", **r, **({"sentence_replaced": True, "unsupported_words": bad} if replaced else {})}


@mcp.tool()
async def request_confirmation(summary: str, purpose: str = "readback") -> dict:
    """Ask the SIGNER (they can sign YES / NO or poke Approve / Reject on the headset) a yes/no question.
    purpose 'readback' = approve what will be said; 'send_message' = approve texting their emergency contact (this also covers the read-back).
    The question is shown as text and blocks up to LUMEN_CONFIRM_TIMEOUT_S (default 25 s) for the answer; no answer counts as No."""
    if purpose not in ("readback", "send_message"): return {"confirmed": False, "error": "purpose must be 'readback' or 'send_message'"}
    if rt.hub is None: return {"confirmed": False, "reason": "no headset attached"}
    cid = uuid.uuid4().hex[:8]; fut = asyncio.get_running_loop().create_future(); rt.pending[cid] = fut
    timeout = float(os.getenv("LUMEN_CONFIRM_TIMEOUT_S", "25"))
    await rt.emit("confirm_request", id=cid, summary=summary, seconds=timeout)
    try: ok = bool(await asyncio.wait_for(fut, timeout))
    except asyncio.TimeoutError: ok = False
    finally:
        rt.pending.pop(cid, None)
        await rt.emit("confirm_closed", id=cid)                  # headset + dashboard: this question is over, however it ended
    if ok:
        rt.readback_ok.add(rt.phrase_id)
        if purpose == "send_message": rt.confirmed.add(rt.phrase_id)           # only this purpose unlocks notify_contact
    rt.store.log("confirmation", {"phrase_id": rt.phrase_id, "summary": summary, "purpose": purpose, "approved": ok})
    return {"confirmed": ok}


@mcp.tool()
async def notify_contact(message: str) -> dict:
    """Send an alert to the signer's saved emergency contact. Refuses unless the signer approved it: request_confirmation(purpose='send_message') first.
    The transport is whichever is configured (Twilio SMS, Telegram, ntfy, or a webhook) -- see docs/NOTIFICATIONS.md; with none set it is a DRY RUN."""
    if rt.phrase_id not in rt.confirmed:
        return {"sent": False, "error": "blocked: call request_confirmation with purpose 'send_message' and get approval first"}
    result = await notify.send(message, to=os.getenv("EMERGENCY_CONTACT"))
    rt.store.log("notify", {"message": message, **result})
    return {"sent": result["sent"], "mode": result["mode"], "backend": result.get("backend"),
            "note": "message delivered" if result["sent"] else "DRY RUN: no notification backend is configured (see docs/NOTIFICATIONS.md)",
            "message": message}


if __name__ == "__main__":
    mcp.run()   # stdio
