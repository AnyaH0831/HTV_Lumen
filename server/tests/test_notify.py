"""server/notify.py: pick the first configured backend, and be an honest dry run when none is set."""
import server.notify as N

ENV = ("TWILIO_SID", "TWILIO_TOKEN", "TWILIO_FROM", "EMERGENCY_CONTACT",
       "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "NTFY_TOPIC", "NTFY_SERVER", "NOTIFY_WEBHOOK_URL")


def clean(monkeypatch):
    for k in ENV: monkeypatch.delenv(k, raising=False)


def test_nothing_configured_is_a_dry_run(monkeypatch):
    clean(monkeypatch)
    assert N.backends() == []
    out = N.send_sync("hi")
    assert out["sent"] is False and out["mode"].startswith("dry-run")


def test_ntfy_is_used_and_reports_sent(monkeypatch):
    clean(monkeypatch); monkeypatch.setenv("NTFY_TOPIC", "lumen-test-topic")
    calls = {}

    class R: status_code = 200

    def post(url, **kw): calls["url"] = url; calls["content"] = kw.get("content"); return R()
    monkeypatch.setattr(N.httpx, "post", post)
    out = N.send_sync("the patient is safe")
    assert out == {"sent": True, "backend": "ntfy", "mode": "ntfy"}
    assert calls["url"].endswith("/lumen-test-topic") and b"safe" in calls["content"]


def test_telegram_is_preferred_over_ntfy_and_parses_ok(monkeypatch):
    clean(monkeypatch)
    monkeypatch.setenv("NTFY_TOPIC", "t"); monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok"); monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    assert N.backends() == ["telegram", "ntfy"]

    class R:
        status_code = 200
        def json(self): return {"ok": True}

    seen = []
    monkeypatch.setattr(N.httpx, "post", lambda url, **kw: seen.append(url) or R())
    out = N.send_sync("hi")
    assert out["backend"] == "telegram" and len(seen) == 1 and "api.telegram.org" in seen[0]


def test_a_failing_backend_falls_through_to_the_next(monkeypatch):
    clean(monkeypatch)
    monkeypatch.setenv("NTFY_TOPIC", "t"); monkeypatch.setenv("NOTIFY_WEBHOOK_URL", "http://example.test/hook")

    class Bad:
        status_code = 500

    class Good:
        status_code = 204

    monkeypatch.setattr(N.httpx, "post", lambda url, **kw: Bad() if "ntfy" in url else Good())
    out = N.send_sync("hi")
    assert out["sent"] is True and out["backend"] == "webhook"


def test_twilio_needs_a_destination_number(monkeypatch):
    clean(monkeypatch)
    monkeypatch.setenv("TWILIO_SID", "s"); monkeypatch.setenv("TWILIO_TOKEN", "t"); monkeypatch.setenv("TWILIO_FROM", "+100")
    monkeypatch.setattr(N.httpx, "post", lambda url, **kw: (_ for _ in ()).throw(AssertionError("must not send without a destination")))
    out = N.send_sync("hi")                                   # no `to`, no EMERGENCY_CONTACT
    assert out["sent"] is False and out["mode"].startswith("dry-run")
