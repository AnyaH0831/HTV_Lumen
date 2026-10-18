"""Outbound notifications for Lumen and KAZE: Twilio SMS, Telegram, ntfy, or a generic webhook.

Pick whatever you have -- the FIRST configured backend wins, so you never need Twilio to demo a real alert:

  twilio    TWILIO_SID + TWILIO_TOKEN + TWILIO_FROM + a destination number (per-call `to`, or EMERGENCY_CONTACT).
            Free trial credit works, but only to the one number you verify, and trial SMS carry a prefix.
  telegram  TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID. Free and unlimited: talk to @BotFather to make a bot, then message it.
  ntfy      NTFY_TOPIC (e.g. "lumen-demo-8f3a"). Free push with no account: install the ntfy app, subscribe to the topic.
  webhook   NOTIFY_WEBHOOK_URL. Any endpoint that accepts a JSON POST {"subject": ..., "text": ...}.

With nothing configured, `send` returns sent=False and mode "dry-run ..." and NOTHING leaves the machine -- the demo is
honest about it (see `docs/NOTIFICATIONS.md`). Everything is plain HTTP via httpx; no SDKs.
"""
import os
import threading

import httpx

TIMEOUT = float(os.getenv("NOTIFY_TIMEOUT_S", "10"))


def _v(name: str) -> str:
    return (os.getenv(name) or "").strip()


def backends() -> list[str]:
    """Configured backends, in the order they will be tried. Empty means nothing will be sent."""
    out = []
    if all(_v(k) for k in ("TWILIO_SID", "TWILIO_TOKEN", "TWILIO_FROM")): out.append("twilio")
    if _v("TELEGRAM_BOT_TOKEN") and _v("TELEGRAM_CHAT_ID"): out.append("telegram")
    if _v("NTFY_TOPIC"): out.append("ntfy")
    if _v("NOTIFY_WEBHOOK_URL"): out.append("webhook")
    return out


def _post(url: str, **kw) -> bool:
    try:
        r = httpx.post(url, timeout=TIMEOUT, **kw)
        return r.status_code < 300
    except Exception:
        return False


def send_sync(text: str, *, to: str | None = None, subject: str = "Lumen") -> dict:
    """Try the configured backends in order; the first 2xx wins. Returns {sent, backend, mode, to?}."""
    configured = backends()
    if not configured:
        return {"sent": False, "backend": None, "mode": "dry-run (no notification backend configured)"}
    dest = (to or _v("EMERGENCY_CONTACT")).strip()
    for name in configured:
        try:
            if name == "twilio" and dest:
                r = httpx.post(f"https://api.twilio.com/2010-04-01/Accounts/{_v('TWILIO_SID')}/Messages.json",
                               auth=(_v("TWILIO_SID"), _v("TWILIO_TOKEN")),
                               data={"From": _v("TWILIO_FROM"), "To": dest, "Body": text}, timeout=TIMEOUT)
                if r.status_code < 300:
                    return {"sent": True, "backend": name, "mode": "twilio", "to": dest}
            elif name == "telegram":
                r = httpx.post(f"https://api.telegram.org/bot{_v('TELEGRAM_BOT_TOKEN')}/sendMessage",
                               json={"chat_id": _v("TELEGRAM_CHAT_ID"), "text": text}, timeout=TIMEOUT)
                if r.status_code < 300 and r.json().get("ok"):
                    return {"sent": True, "backend": name, "mode": "telegram"}
            elif name == "ntfy":
                base = _v("NTFY_SERVER") or "https://ntfy.sh"
                r = httpx.post(f"{base}/{_v('NTFY_TOPIC')}", content=text.encode(),
                               headers={"Title": subject, "Tags": "rotating_light", "Priority": "high"}, timeout=TIMEOUT)
                if r.status_code < 300:
                    return {"sent": True, "backend": name, "mode": "ntfy"}
            elif name == "webhook":
                r = httpx.post(_v("NOTIFY_WEBHOOK_URL"), json={"subject": subject, "text": text}, timeout=TIMEOUT)
                if r.status_code < 300:
                    return {"sent": True, "backend": name, "mode": "webhook"}
        except Exception:
            pass                                     # a broken backend must not stop the next one
    return {"sent": False, "backend": None, "mode": "dry-run (configured backend(s) failed): " + ", ".join(configured)}


async def send(text: str, *, to: str | None = None) -> dict:
    """Async wrapper: the HTTP calls run in a worker thread so the event loop never blocks on a slow backend."""
    import asyncio
    return await asyncio.to_thread(send_sync, text, to=to)


def mirror(text: str, *, to: str | None = None) -> None:
    """Fire-and-forget a duplicate notification without blocking the caller (used by KAZE's simulated notifier)."""
    if not backends():
        return
    threading.Thread(target=send_sync, args=(text,), kwargs={"to": to}, daemon=True).start()
