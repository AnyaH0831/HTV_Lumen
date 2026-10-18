"""Speak text aloud, in one place.

ElevenLabs (ELEVENLABS_API_KEY) renders an mp3 and we emit {type:'tts', url}. With no key --
venue Wi-Fi, no budget -- we emit {type:'say', text} instead and the browser speaks it with
speechSynthesis, so the read-back is always HEARD, never only shown.

Used by request_confirmation (the read-back the signer approves) and speak_to_clinician
(the sentence that goes to the doctor after approval)."""
import os
import uuid
from .state import ROOT, rt


async def speak(text: str, roles=None) -> dict:
    """Emit audio for `text` to every connected client (or only to `roles`, falling back to everyone). Returns {"audio": bool(mp3), "text": text}."""
    url = None
    key = os.getenv("ELEVENLABS_API_KEY")
    if key:
        try:
            import httpx
            voice = os.getenv("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.post(f"https://api.elevenlabs.io/v1/text-to-speech/{voice}", headers={"xi-api-key": key},
                                 json={"text": text, "model_id": os.getenv("ELEVENLABS_MODEL", "eleven_multilingual_v2")})
                r.raise_for_status()
            out = ROOT / "server" / "static" / "audio"; out.mkdir(parents=True, exist_ok=True)
            name = f"{uuid.uuid4().hex}.mp3"; (out / name).write_bytes(r.content); url = f"/static/audio/{name}"
        except Exception as e:
            rt.store.log("tts_error", {"error": str(e)})   # fall through to browser speech
    if url: await rt.emit_to(roles, "tts", url=url, text=text)   # text lets the client fall back to browser speech
    else: await rt.emit_to(roles, "say", text=text)
    return {"audio": bool(url), "text": text}
