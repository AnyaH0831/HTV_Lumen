"""The auto-speak flow through the real app, pipeline and MCP tools (offline planner, rule normalizer: no network)."""
import asyncio, os, tempfile, time
from contextlib import ExitStack
os.environ["SIGNBRIDGE_DATA"] = tempfile.mkdtemp(); os.environ["LLM_PROVIDER"] = "offline"; os.environ["NORMALIZER"] = "rule"
os.environ["LUMEN_SPEAK_DELAY_S"] = "0.15"; os.environ["LUMEN_QUIET_S"] = "0.2"; os.environ["LUMEN_EXTEND_S"] = "0.25"
import pytest
from fastapi.testclient import TestClient
from server.app import app
from server.state import rt, Hub
import server.orchestrator as orch


@pytest.fixture(autouse=True)
def fresh_state():
    rt.tokens = []; rt.busy = False; rt.busy_count = 0; rt.speech = None; rt.phrase_tokens = []; rt.policy = None
    rt.readback_ok = set(); rt.confirmed = set(); rt.quiet_task = None; rt.quiet_phase = ""; rt.norm_cache = None; rt.last_spoken = 0.0; rt.doctor = []; rt.first_t = 0.0


def tok(g, c, *alts): return {"gloss": g, "conf": c, "alts": [{"gloss": a, "conf": p} for a, p in alts]}
def drain_until(ws, kind, log):
    while True:
        m = ws.receive_json(); log.append(m)
        if m["type"] == kind: return m
def kinds(log): return [m["type"] for m in log]
def agent_texts(log): return [m.get("text", "") for m in log if m["type"] == "agent_log"]
def connect(c, st, role="dashboard"):
    w = st.enter_context(c.websocket_connect(f"/ws?role={role}")); w.receive_json(); return w          # closed with the ExitStack


def test_clear_phrase_is_spoken_automatically_to_the_laptop_and_the_headset():
    with TestClient(app) as c, ExitStack() as st:
        dash, head = connect(c, st), connect(c, st, "quest")
        dash.send_json({"type": "send", "tokens": [tok("PAIN", .95), tok("HEAD", .93)]})
        d, h = [], []; drain_until(dash, "agent_done", d); drain_until(head, "agent_done", h)
        k = kinds(d); assert "confirm_request" not in k and k.index("speech_pending") < k.index("speech_done") and k.index("policy") < k.index("speech_pending")
        said = [m["text"] for m in d if m["type"] == "say"]; assert len(said) == 1 and "Pain, head." in said[0] and said[0].startswith("The patient is Deaf")
        # The read-back reaches the clinician's laptop AND the patient's headset. Routing it to the laptop alone
        # left a solo signer in silence whenever a dashboard tab happened to be open.
        hsaid = [m["text"] for m in h if m["type"] == "say"]
        assert len(hsaid) == 1 and "Pain, head." in hsaid[0], "the headset hears the read-back too"
        t = agent_texts(d); assert t.index(next(x for x in t if "speak_to_clinician" in x)) < t.index(next(x for x in t if "submit_intake" in x))


def test_signer_can_cancel_during_the_countdown_and_nothing_is_said_or_saved():
    with TestClient(app) as c, ExitStack() as st:
        dash = connect(c, st); dash.send_json({"type": "send", "tokens": [tok("PAIN", .95), tok("HEAD", .93)]})
        log = []; drain_until(dash, "speech_pending", log); dash.send_json({"type": "cancel_speech"}); drain_until(dash, "agent_done", log)
        assert "speech_cancelled" in kinds(log) and "speech_done" not in kinds(log) and "say" not in kinds(log)
        assert not any("submit_intake" in t for t in agent_texts(log))


def test_critical_close_call_asks_the_signer_before_speaking():
    toks = [tok("PAIN", .9), tok("FALL", .5, ("HEAD", .45))]
    for ok in (False, True):
        with TestClient(app) as c, ExitStack() as st:
            dash = connect(c, st); dash.send_json({"type": "send", "tokens": toks}); log = []
            req = drain_until(dash, "confirm_request", log); assert "speech_pending" not in kinds(log)
            dash.send_json({"type": "confirm", "id": req["id"], "ok": ok}); drain_until(dash, "agent_done", log)
            assert ("speech_done" in kinds(log)) is ok


def test_low_confidence_asks_to_sign_again_and_says_nothing():
    with TestClient(app) as c, ExitStack() as st:
        dash = connect(c, st); dash.send_json({"type": "send", "tokens": [tok("PAIN", .3), tok("HEAD", .3)]})
        log = []; drain_until(dash, "agent_done", log); k = kinds(log)
        assert "notice" in k and "speech_pending" not in k and "confirm_request" not in k


def test_emergency_is_spoken_at_once_and_texting_needs_its_own_approval():
    for ok in (False, True):
        with TestClient(app) as c, ExitStack() as st:
            dash = connect(c, st); dash.send_json({"type": "send", "tokens": [tok("EMERGENCY", .95)]}); log = []
            req = drain_until(dash, "confirm_request", log)
            assert "speech_done" in kinds(log) and "text message" in req["summary"].lower()
            assert not any("notify_contact" in t for t in agent_texts(log))
            dash.send_json({"type": "confirm", "id": req["id"], "ok": ok}); drain_until(dash, "agent_done", log)
            assert any("notify_contact" in t for t in agent_texts(log)) is ok


def test_a_weak_emergency_asks_once_and_that_one_approval_also_covers_texting():
    with TestClient(app) as c, ExitStack() as st:
        dash = connect(c, st); dash.send_json({"type": "send", "tokens": [tok("EMERGENCY", .6)]}); log = []
        req = drain_until(dash, "confirm_request", log); assert "speech_pending" not in kinds(log) and "text your emergency contact" in req["summary"]
        dash.send_json({"type": "confirm", "id": req["id"], "ok": True}); drain_until(dash, "agent_done", log)
        assert "speech_done" in kinds(log) and any("notify_contact" in t for t in agent_texts(log))
        assert kinds(log).count("confirm_request") == 1, "exactly one question: the combined approval covers speaking and texting"


def test_readback_approval_alone_does_not_unlock_texting():
    from server.mcp_server import notify_contact
    rt.phrase_id += 1; rt.readback_ok.add(rt.phrase_id)
    out = asyncio.run(notify_contact("test")); assert out["sent"] is False and "blocked" in out["error"]
    rt.confirmed.add(rt.phrase_id); assert asyncio.run(notify_contact("test"))["mode"].startswith("dry-run")


def _policy(**kw): return {"action": "speak", "reasons": [], "hedge": "", "words": ["PAIN", "HEAD"], "raw": ["PAIN", "HEAD"], "fallback_sentence": "Pain, head.", **kw}


def test_a_sentence_with_invented_facts_is_replaced_by_the_plain_fallback():
    from server.mcp_server import speak_to_clinician
    rt.hub = Hub(); rt.policy = _policy()
    r = asyncio.run(speak_to_clinician("I have severe pain in my head for 3 days."))
    assert r["status"] == "spoken" and r["sentence_replaced"] and r["text"] == "Pain, head." and {"severe", "3", "days"} <= set(r["unsupported_words"])


def test_the_tool_holds_a_risky_phrase_until_the_signer_approves_the_readback():
    from server.mcp_server import speak_to_clinician
    rt.hub = Hub(); rt.phrase_id += 1; rt.policy = _policy(action="ask", reasons=["unsure between fall and head"], words=["FALL"], raw=["FALL"], fallback_sentence="Fall.")
    assert asyncio.run(speak_to_clinician("Fall."))["status"] == "held"
    rt.readback_ok.add(rt.phrase_id); assert asyncio.run(speak_to_clinician("Fall."))["status"] == "spoken"


def test_a_noncritical_close_call_is_spoken_with_the_hedge_appended():
    from server.mcp_server import speak_to_clinician
    rt.hub = Hub(); rt.policy = _policy(hedge="Not sure if that was ear or eye.", words=["PAIN", "EAR"], raw=["PAIN", "EAR"], fallback_sentence="Pain, ear.")
    assert asyncio.run(speak_to_clinician("Pain, ear."))["text"] == "Pain, ear. Not sure if that was ear or eye."


def test_signs_are_spoken_after_a_pause_with_no_send_button():
    with TestClient(app) as c, ExitStack() as st:
        dash = connect(c, st)
        for g, p in (("PAIN", .9), ("ARM", .9)): dash.send_json({"type": "token", "gloss": g, "conf": p, "assist": "auto"})
        log = []; done = drain_until(dash, "speech_done", log)
        assert done["text"] == "Pain, arm." and "agent_start" in kinds(log)


def test_malformed_messages_do_not_break_the_connection():
    with TestClient(app) as c, ExitStack() as st:
        dash = connect(c, st); dash.send_text("not json")
        for bad in ({"type": "token", "gloss": "NOPE"}, {"type": "token", "gloss": "PAIN", "conf": "abc"}, {"type": "token"}, {"type": "send", "tokens": [1, "x", None]}): dash.send_json(bad)
        dash.send_json({"type": "token", "gloss": "SICK", "conf": .9, "assist": "auto"}); log = []
        assert drain_until(dash, "speech_done", log)["text"] == "Sick."


def test_signs_made_while_the_pipeline_is_busy_get_their_own_turn():
    with TestClient(app) as c, ExitStack() as st:
        dash = connect(c, st); dash.send_json({"type": "token", "gloss": "SICK", "conf": .9, "assist": "auto"}); log = []
        drain_until(dash, "speech_pending", log); dash.send_json({"type": "token", "gloss": "HUNGRY", "conf": .9, "assist": "auto"})
        spoken = []
        while len(spoken) < 2: spoken.append(drain_until(dash, "speech_done", log)["text"])
        assert spoken == ["Sick.", "Hungry."]
        said = [m["text"] for m in log if m["type"] == "say"]
        assert said[0].startswith("The patient is Deaf") and said[1] == "Hungry.", "the preface is spoken once, not before every sentence"


def test_if_the_interpreter_says_the_signer_is_not_finished_it_waits_once_for_more_signs(monkeypatch):
    real = orch.normalize
    async def fake(glosses, confs, mode=None, context=None):
        n = await real(glosses, confs, mode="rule"); n["complete"] = glosses != ["HEAD"]; return n
    monkeypatch.setattr(orch, "normalize", fake)
    with TestClient(app) as c, ExitStack() as st:
        dash = connect(c, st); dash.send_json({"type": "token", "gloss": "HEAD", "conf": .9, "assist": "auto"}); log = []
        st = drain_until(dash, "stage", log)
        while st["stage"] != "waiting": st = drain_until(dash, "stage", log)
        dash.send_json({"type": "token", "gloss": "PAIN", "conf": .9, "assist": "auto"})
        assert drain_until(dash, "speech_done", log)["text"] == "Head, pain."
        assert kinds(log).count("speech_done") == 1, "the unfinished phrase was not spoken on its own"


def test_approving_a_readback_through_the_real_flow_does_not_unlock_texting():
    from server.mcp_server import notify_contact
    with TestClient(app) as c, ExitStack() as st:
        dash = connect(c, st); dash.send_json({"type": "send", "tokens": [tok("PAIN", .9), tok("FALL", .5, ("HEAD", .45))]}); log = []
        req = drain_until(dash, "confirm_request", log); assert "text" not in req["summary"].lower()          # a read-back, not a texting question
        dash.send_json({"type": "confirm", "id": req["id"], "ok": True}); drain_until(dash, "agent_done", log)
        assert "speech_done" in kinds(log) and rt.phrase_id in rt.readback_ok
        out = asyncio.run(notify_contact("test")); assert out["sent"] is False and "blocked" in out["error"], out


def _start_counts(log): return [m["count"] for m in log if m["type"] == "agent_start"]


def test_the_same_sign_again_within_the_window_is_one_word(monkeypatch):
    monkeypatch.setenv("LUMEN_DEDUPE_S", "5")
    with TestClient(app) as c, ExitStack() as st:
        dash = connect(c, st); log = []
        for _ in range(4): dash.send_json({"type": "token", "gloss": "PAIN", "conf": .8, "assist": "auto"})
        dash.send_json({"type": "token", "gloss": "HEAD", "conf": .9, "assist": "auto"}); drain_until(dash, "speech_done", log)
        assert _start_counts(log) == [2], "four PAIN detections and one HEAD reach the pipeline as two words"
        toks = [m for m in log if m["type"] == "tokens"][-1]["tokens"]; assert toks[0]["count"] == 4 and toks[0]["conf"] == .8


def test_the_same_sign_after_the_window_is_a_new_word(monkeypatch):
    monkeypatch.setenv("LUMEN_DEDUPE_S", "0.1"); monkeypatch.setenv("LUMEN_QUIET_S", "0.6")
    with TestClient(app) as c, ExitStack() as st:
        dash = connect(c, st); log = []
        dash.send_json({"type": "token", "gloss": "NO", "conf": .9, "assist": "auto"}); time.sleep(0.25)
        dash.send_json({"type": "token", "gloss": "NO", "conf": .9, "assist": "auto"}); drain_until(dash, "agent_start", log)
        assert _start_counts(log) == [2]


def test_a_sentence_is_handed_over_after_the_max_wait_even_if_signing_never_pauses(monkeypatch):
    monkeypatch.setenv("LUMEN_QUIET_S", "0.5"); monkeypatch.setenv("LUMEN_MAX_WAIT_S", "0.7"); monkeypatch.setenv("LUMEN_MAX_QUEUE", "50")
    glosses = ["PAIN", "HEAD", "SICK", "HOT", "COLD", "ARM", "EAR", "EYE"]
    with TestClient(app) as c, ExitStack() as st:
        dash = connect(c, st); p0 = rt.phrase_id; t0 = time.time(); i = 0
        while rt.phrase_id == p0 and time.time() - t0 < 5:                        # a new sign every 0.2 s: the 0.5 s pause never happens
            dash.send_json({"type": "token", "gloss": glosses[i % 8], "conf": .9, "assist": "auto"}); i += 1; time.sleep(0.2)
        took = time.time() - t0; dash.send_json({"type": "clear"})
        assert rt.phrase_id > p0, "never handed over"; assert took < 2.5, f"took {took:.1f}s"
        drain_until(dash, "agent_done", [])


def test_a_full_queue_is_handed_over_at_once(monkeypatch):
    monkeypatch.setenv("LUMEN_QUIET_S", "30"); monkeypatch.setenv("LUMEN_MAX_WAIT_S", "30"); monkeypatch.setenv("LUMEN_MAX_QUEUE", "3")
    with TestClient(app) as c, ExitStack() as st:
        dash = connect(c, st); log = []; t0 = time.time()
        for g in ("PAIN", "HEAD", "SICK"): dash.send_json({"type": "token", "gloss": g, "conf": .9, "assist": "auto"})
        drain_until(dash, "agent_start", log); assert _start_counts(log) == [3]
        assert time.time() - t0 < 3, "handed over by the pause or max wait, not by the full queue"
        drain_until(dash, "agent_done", log)


def test_every_question_ends_with_confirm_closed_so_no_client_is_left_waiting(monkeypatch):
    monkeypatch.setenv("LUMEN_CONFIRM_TIMEOUT_S", "0.3")
    toks = [tok("PAIN", .9), tok("FALL", .5, ("HEAD", .45))]
    with TestClient(app) as c, ExitStack() as st:                                    # nobody answers: timeout
        dash = connect(c, st); dash.send_json({"type": "send", "tokens": toks}); log = []
        req = drain_until(dash, "confirm_request", log); assert req["seconds"] == 0.3
        closed = drain_until(dash, "confirm_closed", log); assert closed["id"] == req["id"]; drain_until(dash, "agent_done", log); assert "speech_done" not in kinds(log)
    monkeypatch.setenv("LUMEN_CONFIRM_TIMEOUT_S", "5")
    with TestClient(app) as c, ExitStack() as st:                                    # answered elsewhere (e.g. the dashboard)
        dash = connect(c, st); dash.send_json({"type": "send", "tokens": toks}); log = []
        req = drain_until(dash, "confirm_request", log); dash.send_json({"type": "confirm", "id": req["id"], "ok": True})
        assert drain_until(dash, "confirm_closed", log)["id"] == req["id"]


def test_an_interrupted_countdown_never_leaves_speech_stuck():
    from server import speech
    async def go():
        rt.hub = Hub(); t = asyncio.create_task(speech.stage("Pain.", ["PAIN"], ["PAIN"], seconds=5)); await asyncio.sleep(0.1)
        assert rt.speech is not None; t.cancel()
        try: await t
        except asyncio.CancelledError: pass
        assert rt.speech is None, "a cancelled task left rt.speech set: every later sentence would be 'busy'"
        return (await speech.stage("Pain.", ["PAIN"], ["PAIN"], seconds=0.05))["status"]
    assert asyncio.run(go()) == "spoken"
