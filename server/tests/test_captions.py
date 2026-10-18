"""Captions: the Whisper decode must happen off the event loop.

faster-whisper's `model.transcribe()` is lazy: it returns a segment *generator* and does the
heavy decode only when the generator is iterated. Iterating it on the event-loop thread blocks
every HTTP/WebSocket request for the length of the utterance, which is what made the headset
pages (and the Start button) hang. `transcribe()` must consume the generator itself.
"""
import asyncio, base64, threading

import numpy as np

import server.captions as captions


class _Seg:
    def __init__(self, text): self.text = text


class _LazyModel:
    """Mimics faster-whisper: transcribe() is cheap, decoding happens while iterating."""
    def __init__(self, chunks): self.chunks = chunks; self.worked = False

    def transcribe(self, audio, **kw):
        chunks = self.chunks

        def gen():
            self.worked = True
            for c in chunks:
                yield _Seg(c)
        return gen(), {"language": "en"}


def test_transcribe_consumes_the_lazy_generator():
    model = _LazyModel([" hello", " world "])
    assert captions.transcribe(model, b"audio") == "hello world", "the generator must be consumed and joined"
    assert model.worked, "iterating the generator is the actual decode: it must run inside transcribe()"


def test_transcribe_runs_off_the_event_loop():
    """The whole decode must run in a worker thread, not on the loop thread."""
    seen = {}
    model = _LazyModel([])

    def gen():
        seen["thread"] = threading.get_ident()
        yield _Seg("x")

    model.transcribe = lambda audio, **kw: (gen(), {})

    async def main():
        seen["loop_thread"] = threading.get_ident()
        assert await asyncio.to_thread(captions.transcribe, model, b"") == "x"

    asyncio.run(main())
    assert seen["thread"] != seen["loop_thread"], "decode must not run on the event-loop thread"


class _RT:
    def __init__(self): self.said = []

    async def doctor_said(self, text): self.said.append(text)


def _segmenter(rt, chunks):
    seg = captions.Segmenter(rt)
    seg.model = _LazyModel(chunks)          # skip building a real WhisperModel
    seg.thr = 0.01
    return seg


def test_browser_audio_is_segmented_and_transcribed():
    """feed() runs the same VAD as the mic: a loud burst then a pause -> one caption via doctor_said."""
    rt = _RT()
    seg = _segmenter(rt, [" streamed ", " words "])

    async def main():
        loud = np.full(1600, 0.5, dtype="float32")                    # 0.1 s of speech, six times
        for _ in range(6): await seg.feed(loud)
        await seg.feed(np.zeros(int(captions.SR * 0.8), dtype="float32"))   # 0.8 s pause ends the utterance

    asyncio.run(main())
    assert rt.said == ["streamed words"], "streamed browser audio must reach doctor_said as a caption"


def test_silence_alone_produces_no_caption():
    rt = _RT()
    seg = _segmenter(rt, ["should not happen"])

    async def main():
        for _ in range(20): await seg.feed(np.zeros(1600, dtype="float32"))

    asyncio.run(main())
    assert rt.said == [], "background silence must never be transcribed"


def test_feed_b64_decodes_int16_pcm():
    rt = _RT()
    captions._seg = _segmenter(rt, [" over ", " the wire "])
    try:
        async def main():
            loud = base64.b64encode(np.full(1600, 16000, dtype="<i2").tobytes()).decode()
            for _ in range(6): await captions.feed_b64(rt, loud)
            quiet = base64.b64encode(np.zeros(int(captions.SR * 0.8), dtype="<i2").tobytes()).decode()
            await captions.feed_b64(rt, quiet)
        asyncio.run(main())
    finally:
        captions._seg = None
    assert rt.said == ["over the wire"], "base64 int16 chunks decoded from the browser must transcribe"
