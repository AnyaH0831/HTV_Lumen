"""Staged speech: show the sentence, count down, then say it unless the signer cancels.

speak_to_clinician goes through stage(), so the countdown, the cancel and the audit log cannot be skipped by any planner."""
import asyncio, os, time, uuid
from .state import rt
from .tts import speak

PREFACE = "The patient is Deaf and signing. This is an automatic reading and may contain mistakes."
PREFACE_AFTER_S = 600                      # repeat the preface after 10 quiet minutes (a new person may be in front of the patient)


def delay_s() -> float: return float(os.getenv("LUMEN_SPEAK_DELAY_S", "1.5"))


async def stage(text: str, words: list[str], raw: list[str], notes: list[str] | None = None, seconds: float | None = None) -> dict:
    if rt.speech: return {"status": "busy"}
    seconds = delay_s() if seconds is None else seconds
    sid = uuid.uuid4().hex[:8]; ev = asyncio.Event(); rt.speech = {"id": sid, "cancel": False, "event": ev}
    try:
        await rt.emit("speech_pending", id=sid, text=text, raw=raw, words=words, notes=notes or [], seconds=seconds)
        try: await asyncio.wait_for(ev.wait(), seconds)             # ends early on Cancel or Speak now
        except asyncio.TimeoutError: pass
    finally:
        st, rt.speech = rt.speech, None                              # always cleared, even if this task is cancelled mid-countdown
    if st["cancel"]:
        rt.store.log("speech_cancelled", {"phrase_id": rt.phrase_id, "text": text}); await rt.emit("speech_cancelled", id=sid)
        return {"status": "cancelled", "text": text}
    spoken = (PREFACE + " " if time.time() - rt.last_spoken > PREFACE_AFTER_S else "") + text
    rt.last_spoken = time.time()
    audio = await speak(spoken, roles=["dashboard", "quest"])    # clinician's laptop and/or the patient's headset (falls back to all)
    await rt.emit("caption_out", text=text)
    await rt.emit("speech_done", id=sid, text=text)
    rt.store.log("speak", {"phrase_id": rt.phrase_id, "text": text, "words": words, "raw": raw, "notes": notes or [], "audio": audio["audio"], "auto": True})
    return {"status": "spoken", "text": text, "audio": audio["audio"]}


def cancel() -> bool:
    if not rt.speech: return False
    rt.speech["cancel"] = True; rt.speech["event"].set(); return True


def speak_now() -> bool:
    if not rt.speech: return False
    rt.speech["event"].set(); return True
