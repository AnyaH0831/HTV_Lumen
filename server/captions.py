"""Optional: clinician speech -> captions in the headset (ENABLE_CAPTIONS=1).

Two audio sources feed one pipeline:

* ``run(rt)``      -- a microphone on the machine that runs the server (local dev / a laptop).
* ``feed(rt, pcm)`` -- 16 kHz mono audio streamed by a browser, so captions still work when the
                      server itself has no microphone (a headset page, or a cloud VM).

Energy VAD segments utterances; each is transcribed with faster-whisper and broadcast as
``{type:'caption'}``."""
import asyncio, base64, os
import numpy as np

SR = 16000


def transcribe(model, audio):
    """Transcribe on a worker thread. faster-whisper returns a *lazy* generator, so it must be
    consumed here: iterating it (the actual decode) on the event loop would block every HTTP
    and WebSocket request for the length of the utterance, which makes the headset pages hang."""
    segs, _ = model.transcribe(audio, language="en", vad_filter=True)
    return " ".join(s.text.strip() for s in segs).strip()


class Segmenter:
    """Energy-VAD segmentation shared by every audio source: ~0.7 s of silence (or 30 s of
    speech) ends an utterance and hands it to Whisper. Chunks of any size may be fed in."""

    def __init__(self, rt):
        self.rt = rt
        self.model = None
        self.buf = []                 # float32 chunks of the current utterance
        self.n = 0                    # samples buffered in self.buf
        self.speaking = False
        self.silence = 0              # samples of silence since the last loud chunk
        self.thr = float(os.getenv("VAD_RMS", "0.015"))
        self.lock = asyncio.Lock()

    async def _model(self):
        if self.model is None:
            from faster_whisper import WhisperModel
            self.model = await asyncio.to_thread(WhisperModel, os.getenv("WHISPER_MODEL", "base.en"), compute_type="int8")
        return self.model

    async def feed(self, chunk):
        chunk = np.asarray(chunk, dtype=np.float32)
        n = int(chunk.size)
        if not n: return
        loud = float(np.sqrt((chunk ** 2).mean())) > self.thr
        if loud: self.speaking, self.silence = True, 0
        if self.speaking:
            self.buf.append(chunk); self.n += n
            if not loud: self.silence += n
        if self.speaking and (self.silence >= SR * 0.7 or self.n > SR * 30):     # ~0.7 s pause or 30 s max
            audio = np.concatenate(self.buf)
            self.buf, self.n, self.speaking, self.silence = [], 0, False, 0
            if len(audio) > SR * 0.4:
                async with self.lock:                                            # one decode at a time
                    text = await asyncio.to_thread(transcribe, await self._model(), audio)
                if text: await self.rt.doctor_said(text)


_seg = None


def segmenter(rt):
    """One process-wide segmenter (the Whisper model is expensive to build and to keep warm)."""
    global _seg
    if _seg is None: _seg = Segmenter(rt)
    return _seg


async def feed(rt, pcm):
    """Feed mono float32 audio in [-1, 1] at SR (used by the browser WebSocket path)."""
    await segmenter(rt).feed(pcm)


async def feed_b64(rt, b64):
    """Browser chunk: base64 of little-endian int16 PCM at 16 kHz, mono."""
    try: raw = base64.b64decode(b64 or "")
    except Exception: return
    await feed(rt, np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0)


async def run(rt):
    """Server-side microphone source. A machine with no input device (a cloud VM) simply yields
    no captions here -- the browser path still works."""
    try:
        import sounddevice as sd
    except Exception as e:
        print(f"[captions] sounddevice unavailable: {e}"); return
    q: asyncio.Queue = asyncio.Queue(); loop = asyncio.get_running_loop()
    try:
        stream = sd.InputStream(samplerate=SR, channels=1, dtype="float32", blocksize=1600,
                                callback=lambda x, *_: loop.call_soon_threadsafe(q.put_nowait, x[:, 0].copy()))
        stream.start()
    except Exception as e:
        print(f"[captions] no server microphone -- use the dashboard mic (browser-streamed): {e}"); return
    seg = segmenter(rt)
    while True:
        await seg.feed(await q.get())
