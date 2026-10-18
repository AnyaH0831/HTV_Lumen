# Notifications: where your phone number goes, and how to try it for real

Lumen has two independent things that can leave the machine, and they answer to **different** settings:

| | What triggers it | Code | Destination setting |
|---|---|---|---|
| **Clinic SMS** (`notify_contact`) | the signer signs EMERGENCY / HELP, then approves texting | `server/mcp_server.py` → `server/notify.py` | `EMERGENCY_CONTACT` (or a per-call `to`) |
| **KAZE alert** | a missed check-in or a duress PIN escalates | `server/kaze/service.py` → `server/notify.py` (mirror) | the contacts stored **per person** in the KAZE database |

Both funnel through one small module, `server/notify.py`, so "how do I try this on my own phone?"
has one answer.

## TL;DR — put your number in `.env`

```bash
cp .env.example .env          # .env is gitignored; never commit it
# edit .env and pick ONE free transport (Telegram is easiest), plus your number:
EMERGENCY_CONTACT=+14155550123
TELEGRAM_BOT_TOKEN=123456:ABC...     # free
TELEGRAM_CHAT_ID=987654321           # free
```

Restart the server. From then on a signer-approved EMERGENCY/HELP sends you a real message on that
number (via your Telegram bot). **With nothing configured, nothing is ever sent** — the code returns
`mode = "dry-run ..."` and logs the text instead, and the tests assert that.

> The number is **only** read at the moment of a send, by `server/notify.py`. It is never uploaded on
> startup, never sent to the LLM providers, and never written to the event log. `.env` lives only on
> the machine running the server.

## The transports (first configured one wins)

`notify.backends()` checks them in this order and the **first 2xx wins**. Configure whichever you
have; you do not need all of them, and you never need Twilio.

| Backend | Keys | Cost | Where the message goes | Does your number leave the machine? |
|---|---|---|---|---|
| `twilio` | `TWILIO_SID`, `TWILIO_TOKEN`, `TWILIO_FROM`, + destination | paid (trial credit works) | `api.twilio.com` → real SMS | **yes** — the destination number is sent to Twilio |
| `telegram` | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | **free, unlimited** | `api.telegram.org` → your chat | **no** — only the chat id and text |
| `ntfy` | `NTFY_TOPIC` (+ optional `NTFY_SERVER`) | **free, no account** | `https://ntfy.sh/<topic>` (or your own server) | **no** — only the text |
| `webhook` | `NOTIFY_WEBHOOK_URL` | free | your endpoint, `POST {"subject","text"}` | depends on your endpoint |
| *(none)* | — | — | **dry run**: text is logged, nothing leaves | **no** |

### Telegram (recommended free path)
1. In Telegram, message `@BotFather` → `/newbot` → copy the token to `TELEGRAM_BOT_TOKEN`.
2. Send your new bot any message.
3. Open `https://api.telegram.org/bot<token>/getUpdates` and copy `result[0].message.chat.id` to `TELEGRAM_CHAT_ID`.

### ntfy (no account at all)
1. Install the ntfy app (iOS/Android) or open `https://ntfy.sh`.
2. Subscribe to a topic you pick, e.g. `lumen-anya-8f3a`.
3. Put it in `NTFY_TOPIC`. Anyone who knows the topic can read it, so treat the topic name as the secret.

### Twilio (real SMS to a normal phone)
Set all four of `TWILIO_SID`, `TWILIO_TOKEN`, `TWILIO_FROM` and `EMERGENCY_CONTACT`. A trial account
can only text **numbers you have verified** in the Twilio console, and trial SMS carry a prefix. This
is the most "phone-like" path and the one the demo screenshots tend to use, but it costs money.

## How the number flows in the clinic path

```
signer signs EMERGENCY
  -> policy.assess() sets needs_sms = True                (server/policy.py, code not prompt)
  -> agent must call request_confirmation(purpose="send_message")  -> the signer approves
  -> rt.confirmed is set ONLY by that approval            (server/state.py)
  -> notify_contact() checks rt.confirmed in code, then calls notify.send(text, to=EMERGENCY_CONTACT)
  -> notify.py POSTs to the first configured backend
```

Two safety properties are enforced in code and covered by tests:

- An approved **read-back** never unlocks texting; only a `send_message` approval does
  (`server/tests/test_agent_flow.py`).
- With no backend configured, `notify_contact` is a dry run and says so in its `mode` string, so a demo
  can never accidentally claim it texted someone.

## How the number flows in the KAZE path

KAZE contacts are stored per person in the KAZE database (`server/kaze/service.py`), created in the
laptop console at `/kaze/setup`. The engine's own `SimulatedNotifier` still logs every step and drives
the escalation state machine; a thin `MirroringNotifier` subclass sends a **real** copy through
`notify.mirror` in a background thread **only when a backend is configured**. So:

- Simulation and tests keep running with zero network and zero side effects (they use `SimulatedNotifier`).
- A real deployment with Telegram/ntfy/Twilio set gets real messages out of the same engine.
- We never claim a delivery that the engine did not actually perform; the `notify` event in the
  timeline records delivered/not-delivered either way.

## Configuration reference

All optional; read from the process environment first, then `.env`.

| Key | Purpose |
|---|---|
| `EMERGENCY_CONTACT` | clinic SMS destination (E.164, e.g. `+14155550123`) |
| `TWILIO_SID` / `TWILIO_TOKEN` / `TWILIO_FROM` | Twilio backend |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | Telegram backend (free) |
| `NTFY_TOPIC` / `NTFY_SERVER` | ntfy backend (free; server defaults to `https://ntfy.sh`) |
| `NOTIFY_WEBHOOK_URL` | generic JSON webhook |
| `NOTIFY_TIMEOUT_S` | per-request timeout (default `10`) |

## What is verified vs not

| Verified by tests | Not verified |
|---|---|
| Dry run when nothing is configured; first-configured-wins ordering; `notify_contact` blocked in code without a `send_message` approval (`server/tests/test_notify.py`, `test_agent_flow.py`) | A real Telegram/ntfy/Twilio round trip with a real account — bring your own keys and run it once |
| `MirroringNotifier` keeps the simulated engine's escalation semantics | KAZE real delivery at scale, delivery receipts, retries |
