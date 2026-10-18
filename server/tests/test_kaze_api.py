"""KAZE over HTTP and WebSocket: authentication, the full journey, duress on the wire, rate limits, and the pages. Fake clock, in-memory database."""
import os, tempfile
os.environ["SIGNBRIDGE_DATA"] = tempfile.mkdtemp()
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from server.kaze import models as M
from server.kaze.app import create_app
from server.kaze.runtime import runtime, RateLimiter
from server.kaze.service import FakeClock

ADMIN = "admin-test-token"; H = {"X-Admin-Token": ADMIN}
USER = dict(name="Sam", safe_pin="4821", duress_pin="9137", preset="demo", contacts=[
    {"name": "Alex", "permissions": {"location": "precise", "can_resolve": True}}, {"name": "Bo", "permissions": {"location": "coarse"}}, {"name": "Cy", "permissions": {"location": "none"}}])


@pytest.fixture
def env():
    clk = FakeClock(); runtime.settings = {}; runtime.limiter = RateLimiter()
    runtime.configure(db_url="sqlite:///:memory:", admin_token=ADMIN, secret=b"s" * 32, scrypt_n=2 ** 8, clock=clk, base_url="https://kaze.test")
    with TestClient(create_app()) as c:
        yield c, clk


def tick(clk, seconds):
    for _ in range(int(seconds / 0.5)): clk.advance(0.5); runtime.service.tick()


def make(c):
    r = c.post("/api/kaze/admin/users", json=USER, headers=H); assert r.status_code == 200, r.text; j = r.json()
    return j["user_id"], {"Authorization": "Bearer " + j["device_token"]}, {x["name"]: x["id"] for x in j["contacts"]}


def links(c, uid):
    sid = c.get(f"/api/kaze/admin/sessions/{uid}", headers=H).json()[0]["id"]; return sid, {l["contact"]: l["token"] for l in c.get(f"/api/kaze/admin/links/{sid}", headers=H).json()}


def test_every_admin_device_and_contact_endpoint_demands_its_own_credential(env):
    c, _ = env; uid, dev, ids = make(c)
    for path in ("/admin/health", "/admin/outbox", "/admin/scenarios", f"/admin/users/{uid}", "/admin/report"): assert c.get("/api/kaze" + path).status_code == 401, path
    assert c.get("/api/kaze/admin/outbox", headers={"X-Admin-Token": "wrong"}).status_code == 401
    assert c.post("/api/kaze/admin/users", json=USER).status_code == 401 and c.delete(f"/api/kaze/admin/users/{uid}").status_code == 401
    for path in ("/device/state", "/device/audit", "/device/profile"): assert c.get("/api/kaze" + path).status_code == 401, path
    assert c.get("/api/kaze/device/state", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert c.get("/api/kaze/device/state", headers={"Authorization": "Bearer " + dev["Authorization"][7:] + "x"}).status_code == 401
    assert c.get("/api/kaze/contact/garbage").status_code == 404 and c.get("/api/kaze/health").json().keys() == {"ok", "last_tick_age_s"}, "health is public and shows nothing sensitive"
    assert c.get("/api/kaze/device/state", headers=dev).status_code == 200


def test_the_whole_journey_over_http_from_missed_check_in_to_resolved(env):
    c, clk = env; uid, dev, ids = make(c)
    assert c.post("/api/kaze/device/watch/start", headers=dev).json() == {"ok": True, "state": "watching", "next_checkin_in": 10}
    tick(clk, 24); assert c.get("/api/kaze/device/state", headers=dev).json() == {"state": "alert_sent", "help_on_the_way": False}
    out = c.get("/api/kaze/admin/outbox", headers=H).json(); assert [o["payload"]["contact"] for o in out if o["payload"]["msg"] == "alert"] == ["Alex"]
    sid, tok = links(c, uid); v = c.get(f"/api/kaze/contact/{tok['Alex']}").json(); assert v["state"] == "alert" and v["reason"] == "missed_checkin" and v["you_can_resolve"] is True
    assert c.get(f"/api/kaze/contact/{tok['Cy']}").status_code == 403, "Cy was never told, so sees nothing"
    assert c.post(f"/api/kaze/contact/{tok['Alex']}/ack").json()["acknowledged_by_you"] is True
    assert c.get("/api/kaze/device/state", headers=dev).json() == {"state": "alert_sent", "help_on_the_way": True}
    assert c.post(f"/api/kaze/contact/{tok['Alex']}/resolve", json={"reason": "bogus"}).json()["error"] == "bad_reason"
    assert c.post(f"/api/kaze/contact/{tok['Alex']}/resolve", json={"reason": "help_arrived"}).json() == {"ok": True}
    assert c.get(f"/api/kaze/contact/{tok['Alex']}").json()["state"] == "resolved" and c.get("/api/kaze/device/state", headers=dev).json()["state"] == "watching"


def test_a_pin_is_required_to_cancel_and_errors_come_back_as_structured_json(env):
    c, clk = env; uid, dev, _ = make(c); c.post("/api/kaze/device/watch/start", headers=dev); tick(clk, 20)
    r = c.post("/api/kaze/device/checkin", json={"method": "tap"}, headers=dev); assert (r.status_code, r.json()) == (409, {"ok": False, "error": "pin_required"})
    r = c.post("/api/kaze/device/cancel", json={"pin": "0000"}, headers=dev); assert (r.status_code, r.json()["error"], r.json()["attempts_left"]) == (401, "wrong_pin", 4)
    r = c.post("/api/kaze/device/cancel", json={"pin": "4821"}, headers=dev); assert r.status_code == 200 and r.json()["state"] == "watching"
    assert c.post("/api/kaze/device/checkin", json={"method": "magic"}, headers=dev).json()["error"] == "bad_method"
    assert c.post("/api/kaze/device/checkin", json={}, headers=dev).status_code == 200 and c.post("/api/kaze/device/cancel", json={}, headers=dev).status_code == 422


def test_on_the_wire_duress_and_safe_look_identical(env):
    c, clk = env; a, deva, _ = make(c); b, devb, _ = make(c)
    for d in (deva, devb): c.post("/api/kaze/device/watch/start", headers=d)
    tick(clk, 11)
    ra = c.post("/api/kaze/device/checkin", json={"method": "pin", "pin": "4821"}, headers=deva); rb = c.post("/api/kaze/device/checkin", json={"method": "pin", "pin": "9137"}, headers=devb)
    assert (ra.status_code, ra.json()) == (rb.status_code, rb.json()) and ra.headers["content-type"] == rb.headers["content-type"]
    assert ra.json() == rb.json() == {"ok": True, "state": "watching", "next_checkin_in": 10} and len(ra.content) == len(rb.content), "same body, same size"
    assert [m["payload"]["contact"] for m in c.get("/api/kaze/admin/outbox", headers=H).json() if m["payload"]["msg"] == "duress"] == ["Alex"]
    audit = c.get("/api/kaze/device/audit", headers=devb).json(); assert not {e["kind"] for e in audit["events"]} & {"duress_signal", "alert_sent", "notify"}
    assert "duress" not in c.get("/api/kaze/device/state", headers=devb).text.lower() and "duress" not in c.get("/api/kaze/device/profile", headers=devb).text.lower()


def test_a_simulated_disconnect_blocks_the_device_but_not_the_server(env):
    c, clk = env; uid, dev, _ = make(c); c.post("/api/kaze/device/watch/start", headers=dev); tick(clk, 18.5)
    assert c.post("/api/kaze/admin/sim/offline", json={"user_id": uid, "offline": True}, headers=H).json() == {"offline": True}
    assert c.get("/api/kaze/device/state", headers=dev).status_code == 503 and c.post("/api/kaze/device/cancel", json={"pin": "4821"}, headers=dev).status_code == 503
    tick(clk, 6); assert [m["payload"]["contact"] for m in c.get("/api/kaze/admin/outbox", headers=H).json() if m["payload"]["msg"] == "alert"] == ["Alex"]
    c.post("/api/kaze/admin/sim/offline", json={"user_id": uid, "offline": False}, headers=H); assert c.get("/api/kaze/device/state", headers=dev).status_code == 200


def test_pin_guessing_is_rate_limited_per_person(env):
    c, clk = env; uid, dev, _ = make(c); c.post("/api/kaze/device/watch/start", headers=dev)
    codes = [c.post("/api/kaze/device/checkin", json={"method": "pin", "pin": "4821"}, headers=dev).status_code for _ in range(14)]
    assert codes[:12] == [200] * 12 and codes[12:] == [429, 429], codes
    other, dev2, _ = make(c); c.post("/api/kaze/device/watch/start", headers=dev2); assert c.post("/api/kaze/device/checkin", json={"method": "pin", "pin": "4821"}, headers=dev2).status_code == 200, "another person is unaffected"


def test_consent_and_erase_over_http(env):
    c, clk = env; uid, dev, ids = make(c); c.post("/api/kaze/device/watch/start", headers=dev)
    assert c.post("/api/kaze/device/location", json={"lat": 43.6, "lon": -79.3}, headers=dev).json() == {"stored": True}
    assert c.post("/api/kaze/device/location", json={"lat": 999, "lon": 0}, headers=dev).json()["error"] == "bad_location"
    r = c.put("/api/kaze/device/consent", json={"location_policy": "never"}, headers=dev); assert r.json()["location_policy"] == "never" and runtime.service.db.count_locations(uid) == 0
    assert c.post("/api/kaze/device/erase", json={"pin": "9137"}, headers=dev).status_code == 401, "a duress PIN never erases anything"
    assert runtime.service.db.get_user(uid) is not None
    assert c.post("/api/kaze/device/erase", json={"pin": "4821"}, headers=dev).json() == {"erased": True} and c.get("/api/kaze/device/state", headers=dev).status_code == 401


def test_setup_validation_rejects_bad_input_with_clear_errors(env):
    c, _ = env
    for patch, code in ((dict(safe_pin="12"), "bad_pin"), (dict(duress_pin="4821"), "duress_must_differ"), (dict(contacts=[]), "need_1_to_5_contacts"), (dict(location_policy="always"), "bad_location_policy"), (dict(config={"checkin_interval_s": 0}), "bad_config")):
        r = c.post("/api/kaze/admin/users", json={**USER, **patch}, headers=H); assert (r.status_code, r.json()["error"]) == (400, code), (patch, r.text)
    assert c.post("/api/kaze/admin/users", json={"name": "x"}, headers=H).status_code == 422


def test_scenarios_run_over_http_and_unknown_ones_are_refused(env):
    c, _ = env; names = [s["name"] for s in c.get("/api/kaze/admin/scenarios", headers=H).json()]; assert len(names) == 7
    for n in names:
        r = c.post(f"/api/kaze/admin/scenarios/{n}/run", headers=H).json(); assert r["passed"] and r["checks"] and r["timeline"] and r["simulated"] and r["assumptions"], n
    assert c.post("/api/kaze/admin/scenarios/nope/run", headers=H).status_code == 404 and c.post("/api/kaze/admin/scenarios/safe/run?mode=turbo", headers=H).status_code == 400
    assert c.post("/api/kaze/admin/sweep?n=5", headers=H).status_code == 422
    rep = c.get("/api/kaze/admin/report", headers=H).json(); assert all(s["passed"] for s in rep["summary"]) and rep["not_yet_connected"] and rep["needs_further_validation"]


def test_the_pages_are_served_and_carry_no_secrets(env):
    c, _ = env
    for path in ("/kaze", "/kaze/setup", "/kaze/device", "/kaze/c/anything"):
        r = c.get(path); assert r.status_code == 200 and "text/html" in r.headers["content-type"] and ADMIN not in r.text, path


def test_the_operator_console_can_list_and_reconfigure_a_person(env):
    c, _ = env; uid, dev, _ = make(c)
    people = c.get("/api/kaze/admin/users", headers=H).json()
    assert [u["id"] for u in people] == [uid] and people[0]["name"] == "Sam" and people[0]["contacts"][0]["name"] == "Alex"
    r = c.patch(f"/api/kaze/admin/users/{uid}", headers=H, json={"name": "Sam A.", "config": {"checkin_interval_s": 5}, "location_policy": "never"})
    assert r.status_code == 200 and r.json()["name"] == "Sam A."
    p = c.get(f"/api/kaze/admin/users/{uid}", headers=H).json()
    assert p["config"]["checkin_interval_s"] == 5 and p["location_policy"] == "never"
    assert p["config"]["response_window_s"] == 8, "an omitted setting keeps its value"
    assert [x["name"] for x in p["contacts"]] == ["Alex", "Bo", "Cy"], "omitting contacts keeps them"


def test_operator_can_edit_timings_contacts_and_pins_then_re_pairs_the_device(env):
    c, clk = env; uid, dev, _ = make(c); old = dev["Authorization"][7:]
    up = {"config": {"checkin_interval_s": 5}, "contacts": [{"name": "Dana", "permissions": {"location": "precise", "can_resolve": True}}],
          "safe_pin": "2468", "duress_pin": "1357"}
    r = c.patch(f"/api/kaze/admin/users/{uid}", json=up, headers=H); assert r.status_code == 200, r.text
    p = c.get(f"/api/kaze/admin/users/{uid}", headers=H).json()
    assert p["config"]["checkin_interval_s"] == 5 and [x["name"] for x in p["contacts"]] == ["Dana"]
    assert c.get("/api/kaze/admin/users", headers=H).json()[0]["id"] == uid
    # a duplicate PIN is refused, comparing against the STORED hash
    assert c.patch(f"/api/kaze/admin/users/{uid}", json={"safe_pin": "4821", "duress_pin": "4821"}, headers=H).json()["error"] == "duress_must_differ"
    assert c.patch(f"/api/kaze/admin/users/{uid}", json={"safe_pin": "4821"}, headers=H).status_code == 200, "a change may be given for one PIN only"
    # the operator can re-pair a device: the fresh token works, the old one stops
    tok = c.post(f"/api/kaze/admin/users/{uid}/token", headers=H).json()["device_token"]
    assert c.get("/api/kaze/device/state", headers={"Authorization": "Bearer " + tok}).status_code == 200
    assert c.get("/api/kaze/device/state", headers={"Authorization": "Bearer " + old}).status_code == 401


def test_admin_state_shows_the_real_status_for_the_operators_live_monitor(env):
    c, clk = env; uid, dev, _ = make(c)
    assert c.get(f"/api/kaze/admin/state/{uid}", headers=H).json()["state"] == "idle"
    c.post("/api/kaze/device/watch/start", headers=dev); st = c.get(f"/api/kaze/admin/state/{uid}", headers=H).json()
    assert st["state"] == "watching" and st["status"] == "WATCHING" and st["duress"] is False
    tick(clk, 10.5); assert c.get(f"/api/kaze/admin/state/{uid}", headers=H).json()["state"] == "checkin_due"
    c.post("/api/kaze/device/checkin", json={"method": "pin", "pin": "9137"}, headers=dev)      # duress, invisible on the device
    assert c.get(f"/api/kaze/device/state", headers=dev).text.lower().find("duress") == -1
    st = c.get(f"/api/kaze/admin/state/{uid}", headers=H).json(); assert st["duress"] is True and st["status"] == "ESCALATING"


def test_websockets_require_the_right_token_and_stream_events(env):
    c, clk = env; uid, dev, _ = make(c)
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/api/kaze/ws/admin?token=wrong"): pass
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/api/kaze/ws/device?token=wrong"): pass
    token = dev["Authorization"][7:]
    with c.websocket_connect(f"/api/kaze/ws/admin?token={ADMIN}") as adm, c.websocket_connect(f"/api/kaze/ws/device?token={token}") as ph:
        c.post("/api/kaze/device/watch/start", headers=dev); m = adm.receive_json(); assert m["type"] == "event" and m["kind"] == "watch_started"
        tick(clk, 10.5); got = []
        for _ in range(3):
            got.append(ph.receive_json()) if len(got) < 1 else None
        assert got[0] == {"type": "prompt", "kind": "checkin_due", "payload": {"respond_within": 8}}


def test_the_ticker_survives_an_error_and_health_reports_it(env):
    c, clk = env; assert c.get("/api/kaze/health").json()["ok"] is True
    runtime.service.tick_errors += 1; assert c.get("/api/kaze/health").json()["ok"] is False and c.get("/api/kaze/admin/health", headers=H).json()["tick_errors"] == 1


def test_contact_links_are_rate_limited_so_a_token_cannot_be_guessed_at_speed(env):
    c, clk = env; uid, dev, _ = make(c); c.post("/api/kaze/device/watch/start", headers=dev); tick(clk, 30); sid, tok = links(c, uid)      # Alex (23 s) and Bo (29 s) have been told
    codes = [c.get(f"/api/kaze/contact/{tok['Alex']}").status_code for _ in range(95)]
    assert codes[:90] == [200] * 90 and set(codes[90:]) == {429}, codes[85:95]
    assert c.get(f"/api/kaze/contact/{tok['Bo']}").status_code == 200, "a different contact link is unaffected"


def test_the_real_ticker_on_the_real_clock_escalates_with_no_help_from_the_test():
    """No fake clock, no manual tick: the background task alone must carry a countdown through to the first contact."""
    import time
    runtime.settings = {}; runtime.limiter = RateLimiter()
    runtime.configure(db_url="sqlite:///:memory:", admin_token=ADMIN, secret=b"s" * 32, scrypt_n=2 ** 8)
    with TestClient(create_app()) as c:
        r = c.post("/api/kaze/admin/users", json={**USER, "preset": "demo", "config": dict(checkin_interval_s=1, response_window_s=1, alert_grace_s=1, ack_timeout_s=1)}, headers=H).json()
        dev = {"Authorization": "Bearer " + r["device_token"]}; c.post("/api/kaze/device/watch/start", headers=dev); t0 = time.time(); told = []
        while time.time() - t0 < 8 and not told:
            time.sleep(0.25); told = [o["payload"]["contact"] for o in c.get("/api/kaze/admin/outbox", headers=H).json() if o["payload"]["msg"] == "alert"]
        assert told == ["Alex"], told; elapsed = time.time() - t0
        assert 2.5 <= elapsed <= 5.0, f"1 s interval + 1 s window + 1 s grace should alert after about 3 s, took {elapsed:.1f}"
        time.sleep(1.4); assert [o["payload"]["contact"] for o in c.get("/api/kaze/admin/outbox", headers=H).json() if o["payload"]["msg"] == "alert"] == ["Alex", "Bo"], "and the next contact 1 s later"
