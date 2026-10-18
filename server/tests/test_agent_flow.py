import os, tempfile
os.environ["SIGNBRIDGE_DATA"] = tempfile.mkdtemp(); os.environ["LLM_PROVIDER"] = "offline"; os.environ["NORMALIZER"] = "rule"
os.environ["LUMEN_SPEAK_DELAY_S"] = "0.1"; os.environ["LUMEN_CONFIRM_TIMEOUT_S"] = "3"
from fastapi.testclient import TestClient
from server.app import app


def drain_until(ws, kind, log):
    while True:
        m = ws.receive_json(); log.append(m)
        if m["type"] == kind: return m


def test_end_to_end_confirmed_flow():
    """A clear phrase is SPOKEN automatically; the signer still approves texting the emergency contact.

    The policy changed on purpose (see policy.py): only a risky phrase needs a spoken-read-back
    approval. HELP is a texting word, so the one question here is the send_message one."""
    with TestClient(app) as c, c.websocket_connect("/ws?role=dashboard") as ws:
        ws.receive_json()  # initial tokens
        ws.send_json({"type": "send", "tokens": [{"gloss": g, "conf": 0.95} for g in ("PAIN", "HEAD", "ALLERGIC", "HELP")]})
        log = []; req = drain_until(ws, "confirm_request", log)
        assert "text" in req["summary"].lower()                                     # the ONLY question is about texting
        assert any(m["type"] == "speech_done" for m in log)                         # the sentence was already spoken automatically
        assert not any("notify_contact" in m.get("text", "") for m in log)          # nothing outward before approval
        assert not any(m.get("text") == req["summary"] for m in log if m["type"] in ("say", "tts")), "confirmation is text-only"
        ws.send_json({"type": "confirm", "id": req["id"], "ok": True}); drain_until(ws, "agent_done", log)
        texts = [m.get("text", "") for m in log if m["type"] == "agent_log"]
        assert any("submit_intake" in t for t in texts) and any("speak_to_clinician" in t for t in texts)
        norm = next(m for m in log if m["type"] == "normalized")                    # the interpreted sentence
        assert norm["source"] == "rule" and norm["sentence"] and norm["glosses"] == ["PAIN", "HEAD", "ALLERGIC", "HELP"]
        assert any(m["type"] == "say" for m in log)                                 # the sentence is spoken aloud
        assert [m["stage"] for m in log if m["type"] == "stage"][-1] == "done"


def test_rejected_texting_confirmation_blocks_the_message_but_not_the_emergency_speech():
    """Refusing the texting question must not send anything; a clear emergency is still spoken."""
    with TestClient(app) as c, c.websocket_connect("/ws?role=dashboard") as ws:
        ws.receive_json(); ws.send_json({"type": "send", "tokens": [{"gloss": "EMERGENCY", "conf": 0.99}]})
        log = []; req = drain_until(ws, "confirm_request", log)
        ws.send_json({"type": "confirm", "id": req["id"], "ok": False}); log2 = []; drain_until(ws, "agent_done", log2)
        texts = " ".join(m.get("text", "") for m in log + log2 if m["type"] == "agent_log")
        assert "notify_contact" not in texts, "a refused texting question must not send anything"
        assert "speech_done" in [m["type"] for m in log + log2], "the emergency itself is still spoken automatically"


def test_notify_tool_is_guarded_in_code():
    import asyncio
    from server.mcp_server import notify_contact
    from server.state import rt
    rt.confirmed.clear(); out = asyncio.run(notify_contact("test"))
    assert out["sent"] is False and "blocked" in out["error"]


def test_notify_dry_run_says_out_loud_that_nothing_was_sent():
    import asyncio
    from server.mcp_server import notify_contact
    from server.state import rt
    rt.confirmed.add(rt.phrase_id)                       # approved, but no notification backend is configured
    try:
        out = asyncio.run(notify_contact("test"))
    finally:
        rt.confirmed.clear()
    assert out["sent"] is False and "DRY RUN" in out["note"] and "no notification backend" in out["note"]


def test_recording_upload_validation():
    with TestClient(app) as c:
        t = [i / 30 for i in range(10)]; hand = [0.0] * 63
        ok = {"label": "PAIN", "signer": "t1", "t": t, "left": [None] * 10, "right": [hand] * 10}
        assert c.post("/api/recordings", json=ok).status_code == 200
        assert c.post("/api/recordings", json={**ok, "label": "NOPE"}).status_code == 422
        assert c.post("/api/recordings", json={**ok, "t": t[::-1]}).status_code == 422
        assert c.post("/api/recordings", json={**ok, "right": [[1.0] * 5] * 10}).status_code == 422
        assert c.get("/api/vocab").json()["glosses"][0] == "HELLO"
