# Project KAZE

KAZE is a **server-authoritative safety timer with duress semantics**: a person starts a *watch*, the
server checks in on a schedule, and if the person does not answer, it escalates to trusted contacts on
its own. It is deliberately **not** a medical device and never claims to detect incapitation.

KAZE shares the Lumen server. The Lumen app mounts the same router (`server/app.py` → `server.kaze`),
and it also runs stand-alone (`uvicorn server.kaze.app:app`). Code lives in `server/kaze/`.

> Status: the code is complete and fully unit-tested. Nothing has been run against **real people, real
> SMS, a real Postgres cluster, or a real headset**. The scenarios and the sweep are simulations on a
> fake clock. Read "What is real / simulated / assumed / needs validation" before making any claim.

## 1. What is real vs simulated

| Real (implemented and tested) | Simulated / not wired |
|---|---|
| Server-side countdown and escalation state machine that survives the device disconnecting and a server restart (`service.py`, restart recovery tested) | With **no** notification backend configured, still nothing is sent: `SimulatedNotifier` logs a `notify` event and returns delivered/not-delivered. When a backend **is** configured (Telegram/ntfy/Twilio/webhook), `MirroringNotifier` sends a real copy in a background thread — see `docs/NOTIFICATIONS.md`. |
| Safe PIN / duress PIN / lockout logic. Safe and duress replies are byte-identical (tested). | "Emergency services" is one logged `fallback_dispatch` event, marked `SIMULATED`. Nothing is called. |
| Consent-scoped location: only during an active alert, only to permitted contacts, at the chosen precision, logged, deleted on resolve (`_location_for`) | Locations in tests are synthetic coordinates. |
| Per-PIN-failure lockout, per-endpoint rate limits, per-alert contact link tokens (HMAC, expire with the alert) | Loss of connectivity is a flag (`sim_offline`) that makes the device API return 503; there is no real network detection. |
| Storage on SQLite (default) or Postgres/Tiger Data (TimescaleDB hypertable when available), with reconnect-once (`db.py`) | The Postgres path was run in the original sandbox; it has **not** been re-run here (needs a real `KAZE_DB_URL`). |
| Full erasure of a user (documents, events, locations) and location purge on resolve / consent withdrawal | No retention policy beyond locations; events are kept by design as the audit trail. |

## 2. What the prototype actually demonstrates

From the seven scenarios (`scenarios.py`, all machine-checked):

1. **Safe** — a check-in is answered with a tap or the safe PIN; no one is contacted, nothing is released.
2. **No response** — a missed check-in becomes a visible `alert_pending` countdown, then the first contact
   is told at a time fixed by configuration (demo preset: 23 s = 10 + 8 + 5). The message is minimal
   (name + per-alert link, no coordinates, no health detail).
3. **Duress** — a second PIN looks identical to the safe PIN on the wire and on screen (same body, same
   headers, same size), but silently starts escalation, cannot be cancelled by the person's safe PIN or
   taps, and never shows an alert state to an onlooker. Contacts are told *not* to call or text.
4. **Connectivity failure** — the countdown lives on the server, so the alert still fires when the device
   is cut off, at the scheduled time, and survives a fresh process reading the same database.
5. **Unacknowledged alert** — contacts are told one at a time with an acknowledgement window; a silent or
   unreachable contact is skipped; an acknowledgement stops the chain; if no one is reachable a simulated
   emergency hand-over is logged once.
6. **False alarm** — the person (PIN) or an authorised contact can resolve it; an unauthorised contact
   cannot; repeated wrong PINs lock the pad but never stop the alert; contacts are told it was a false alarm.
7. **Privacy** — before an alert every contact link is refused; during an alert each contact sees location
   only at the precision the person allowed; releases are logged for the person; resolving deletes the
   samples; erasing the person removes every trace.

## 3. States and configuration

```
WATCHING → CHECKIN_DUE → ALERT_PENDING → ESCALATING → ACKNOWLEDGED → RESOLVED
```

- `WATCHING` waiting for the next check-in.
- `CHECKIN_DUE` prompted; `response_window_s` to confirm safe.
- `ALERT_PENDING` missed: `alert_grace_s` countdown; only a PIN (safe or duress) cancels it — a tap cannot.
- `ESCALATING` contacts told one at a time; each has `ack_timeout_s` (`duress_ack_timeout_s` for duress).
- `ACKNOWLEDGED` a contact responded; resolved by the person's PIN or an authorised contact.

Presets (`models.py`):

| Setting | `realistic` | `demo` | Limits |
|---|---|---|---|
| `checkin_interval_s` | 900 | 10 | 1 … 86400 |
| `response_window_s` | 60 | 8 | 1 … 3600 |
| `alert_grace_s` | 30 | 5 | 1 … 600 |
| `ack_timeout_s` | 120 | 6 | 1 … 3600 |
| `duress_ack_timeout_s` | 60 | 4 | 1 … 3600 |
| `max_rounds` | 1 | 1 | 1 … 5 |
| `auto_resume` | true | true | — |

Time-to-first-contact = `checkin_interval_s + response_window_s + alert_grace_s` (23 s demo, 990 s realistic).

## 4. API and credentials

Three callers, three credentials (`api.py`):

| Caller | Credential | Can do |
|---|---|---|
| admin | `X-Admin-Token` (or `Authorization: Bearer`) | create/erase users, run scenarios and the sweep, see everything including duress internals |
| device | `Authorization: Bearer <uid>.<token>` | its own state, check-in, cancel, location, consent, erase, audit |
| contact | per-alert link token in the URL | view the one alert it was told about, ack, resolve (if permitted) |

Public: `GET /api/kaze/health` (`{ok, last_tick_age_s}` — nothing sensitive). Live: `WS /api/kaze/ws/admin?token=` and `WS /api/kaze/ws/device?token=`.

Pages:

| Page | Who opens it | What it is |
|---|---|---|
| `/kaze` | operator (laptop) | the **scenario demo console**: run the 7 scenarios, step the fake clock, watch the live timeline |
| `/kaze/setup` | operator (laptop) | the **laptop setup console** (item 2): create/edit people, check-in timings, contacts and permissions, show/copy/rotate the device link, and a live monitor (state, sessions, timeline, contacts told, outbox, unreachable, ack/resolve, simulate offline) |
| `/kaze/device` | the person (headset/phone browser) | the **device client**: pair with a token, show the current check-in state, tap/enter-PIN check-in, "I'm safe", start/stop watch, cancel, and speak "Are you safe?" when a check-in is due |
| `/kaze/c/{token}` | a trusted contact | the one alert they were told about; ack, resolve if permitted |

## 4a. The laptop console (item 2)

The agreed shape is a **laptop operator console + a device client**, instead of a website only on the
Quest. Hand sensing still happens on the headset in the Lumen app; KAZE is the safety timer that runs
beside it, and this pair replaces "everything has to be typed into the headset".

**Setup (`/kaze/setup`).** Everything that is customisable per person lives here, not in code:

- create a person or select one, edit name, PINs, `location_policy`;
- check-in timings — `checkin_interval_s`, `response_window_s`, `alert_grace_s`, `ack_timeout_s`,
  `max_rounds` — with the `realistic`/`demo` presets as a starting point;
- contacts (name, channel, location permission, may-resolve) — add up to five;
- the **device link** (`/kaze/device?t=<token>`): copy it, open it in a tab, or **rotate** the token
  (the old token stops working at once);
- a live monitor that polls `GET /admin/state/{uid}` and the session/timeline/link/outbox endpoints while
  the device WebSocket drives refresh-on-event.

API behind it: `GET/POST /admin/users`, `GET/PATCH/DELETE /admin/users/{uid}`,
`POST /admin/users/{uid}/watch`, `POST /admin/users/{uid}/token`, `GET /admin/state/{uid}`,
plus the existing sessions / timeline / links / outbox / sim endpoints.

## 4b. The headset client

`/kaze/device?t=<uid>.<token>` is a plain browser page, so it runs in the Quest browser, on a phone, or
on the laptop. It pairs with the device bearer token from the console and then:

- polls `/api/kaze/device/state` and listens on `WS /api/kaze/ws/device` so the console and the device
  stay in sync;
- check-in by tap or PIN (the safe and duress PINs are byte-identical on the wire/on screen);
- start/stop watch, cancel an alert (PIN only), "I'm safe";
- says **"Are you safe?"** aloud (browser speech) when a check-in is due.

The device token is minted by the console; the person never sees the admin token. Simulating "no
connection" is an **operator** action on `/kaze/setup`, not something the headset page can do.


## 5. Running it

```bash
make run                                   # combined Lumen + KAZE on :8000
# or stand-alone:
python -m uvicorn server.kaze.app:app --port 8001

curl localhost:8000/api/kaze/health
curl -H "X-Admin-Token: $KAZE_ADMIN_TOKEN" localhost:8000/api/kaze/admin/health
curl -H "X-Admin-Token: $KAZE_ADMIN_TOKEN" localhost:8000/api/kaze/admin/report
curl -X POST -H "X-Admin-Token: $KAZE_ADMIN_TOKEN" "localhost:8000/api/kaze/admin/scenarios/safe/run"
curl -X POST -H "X-Admin-Token: $KAZE_ADMIN_TOKEN" "localhost:8000/api/kaze/admin/sweep?n=200"
python scripts/kaze_sweep.py --n 300 --png runs/kaze_sweep.png   # offline sweep + chart
```

Configuration (`.env`, all optional; see `deploy/env.production.example`):

| Key | Default | Purpose |
|---|---|---|
| `KAZE_ENABLED` | `1` | `0` = do not mount KAZE in the Lumen app |
| `KAZE_DB_URL` / `DATABASE_URL` | `sqlite:///<data>/kaze.sqlite` | `sqlite:///…` or `postgres://…` |
| `KAZE_ADMIN_TOKEN` | generated to `<data>/kaze_admin_token.txt` | admin token |
| `KAZE_SECRET` | generated to `<data>/kaze_secret.txt` | HMAC key for contact links |
| `KAZE_BASE_URL` | `http://localhost:8000` | base for contact links |
| `KAZE_SCRYPT_N` | `16384` | PIN hashing cost (lower in tests) |
| `KAZE_TIMESCALE` | `1` | `0` = never try to create a TimescaleDB hypertable |

### Tiger Data / Postgres (item 3/4)

KAZE is written against Postgres and creates a **TimescaleDB hypertable** when the server has the
extension. It uses SQLite only when no `KAZE_DB_URL`/`DATABASE_URL` is set. To point it at a real
cluster (Tiger Data is hosted TimescaleDB):

```bash
pip install "psycopg[binary]"                # already in server/requirements.txt
export KAZE_DB_URL='postgresql://USER:PASSWORD@HOST:5432/tsdb?sslmode=require'
export KAZE_TIMESCALE=1                       # 0 to skip the hypertable attempt
python -m uvicorn server.app:app --port 8000 # or: python -m uvicorn server.kaze.app:app
curl -H "X-Admin-Token: $KAZE_ADMIN_TOKEN" localhost:8000/api/kaze/admin/health
```

On first connect the service creates its tables and, when `KAZE_TIMESCALE=1` and the `timescaledb`
extension is present, converts `kaze_events` to a hypertable. The Tiger Data connection string is
exactly the one from the Tiger Data console (Service URL → "Connection info"); pass it in `KAZE_DB_URL`
or in `.env`. **The symmetric key (`KAZE_SECRET`) and admin token must be the same across restarts**,
or existing device tokens and contact links will stop validating.

> Status: the Postgres path was exercised in the original sandbox but has **not** been re-run in this
> repo, because it needs a live cluster. `docs/KAZE.md` §7 keeps this on the validation list. `psycopg`
> is a real dependency now; the code degrades to SQLite if `KAZE_DB_URL` is unset.

## 6. The simulation sweep

`sweep(n, p_ack, p_unreachable, p_forget, …)` in `sim.py` runs `n` truly-unresponsive people and `n`
safe-but-forgetful people on a fake clock, then reports:

- **unresponsive**: p50/p95/max seconds from the missed check-in to the first contact being told, an
  analytic bound, seconds to acknowledgement, no-contact-acknowledged, nobody-reachable.
- **safe but forgetful**: how many missed the prompt, how many cancelled with the PIN inside the grace
  window, how many false alarms actually reached a contact.

`scripts/kaze_sweep.py` runs it and optionally writes a PNG chart. These numbers describe **this engine
under the stated assumptions**, not real human behaviour.

## 7. Assumptions and what needs validation

Assumptions (from the scenarios):

- The person notices the prompt and can enter a PIN (or a phrase of signs) at that moment.
- A contact's phone receives and surfaces the message, and the link works.
- The person can enter a duress PIN without arousing suspicion; an onlooker is not comparing timings/logs.
- The device reached the server at least once (so a watch exists); the server and database stay available.
- The person set location permissions deliberately and chose contacts they trust.
- A real hand-over to emergency services would need an agreement and legal review.

Needs validation before any real use:

- **User testing** with people in realistic (including distressed) situations, with ethics approval.
- **Penetration testing** and a privacy/legal review.
- **Availability engineering**: more than one node, monitoring and paging, real delivery with receipts.
- **Accessibility testing** of PIN and sign entry with the people who rely on it.
- **Real Postgres/Tiger Data run** and the **Timescale simulation sweep** at scale.
- **Connect KAZE inside the XR session**: the browser device client at `/kaze/device` is wired to the
  device API today; embedding a check-in overlay directly in the Lumen Quest app (rather than a second
  browser tab) is still to do.

## 8. Design limits (say these out loud)

- The headset **never detects incapacitation**. KAZE only reacts to a missed check-in and to an explicit
  duress PIN. Do not imply otherwise.
- A loud alarm works **against** a discreet duress signal. The duress path is silent by design.
- This is a prototype: single-node, simulated notifier, no delivery guarantees.
