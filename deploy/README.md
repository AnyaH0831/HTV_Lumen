# Deploying Lumen + KAZE

This is a **single-node prototype deployment**: one small VM running both Lumen and KAZE, behind
HTTPS. It is enough to demo from anywhere, not enough for real use (see `docs/KAZE.md` §7). Nothing
in this folder has been run against real infrastructure yet.

## What you need

- A small Linux VM (the plan named a **Vultr** instance; 1 vCPU / 1 GB is enough for the prototype).
- A domain (**GoDaddy** in the original plan) with an `A` record pointing at the VM.
- Ports 80 and 443 open; **Caddy** gets the TLS certificate automatically from Let's Encrypt.
- The repo checked out on the VM, with `make install` run.

## Files

| File | Purpose |
|---|---|
| `install.sh` | one-shot server setup (packages, user, venv, env, systemd, optional Caddy) — see below |
| `lumen.service` | systemd unit: runs `uvicorn server.app:app` as an unprivileged user, restarts on failure |
| `Caddyfile` | reverse proxy + automatic HTTPS for `DOMAIN` |
| `env.production.example` | server environment (tokens, DB URL, provider keys) — copy to `/etc/lumen/lumen.env` |

## Fast path: `install.sh`

The repo is private, so copy it up and run the installer (no `git` on the server needed):

```bash
# 1. on your machine, from the repo root (your .env travels up to carry real credentials)
rsync -az --delete \
  --exclude '.git' --exclude 'data' --exclude 'runs' --exclude '.venv' \
  --exclude '__pycache__' --exclude '.pytest_cache' --exclude 'node_modules' \
  --exclude '*.sqlite' --exclude '*.pyc' \
  ./ root@HOST:/root/lumen/

# 2. on the server
DOMAIN=lumen.example.com bash /root/lumen/deploy/install.sh
```

No domain? Use `<IP>.sslip.io` (e.g. `155.138.210.93.sslip.io`) — it resolves to your IP and
Caddy still gets a real Let's Encrypt certificate. **The headset needs HTTPS (a secure context) for
WebXR**, so a domain or sslip.io name is strongly preferred over a bare IP.

`install.sh` is idempotent (safe to re-run). It generates stable KAZE tokens, **carries
`KAZE_DB_URL` (Tiger Data) and your provider/notification keys — LLM provider + model, Gemini, the
ElevenLabs voice id/model, and captions — out of the local `.env` you copied up
into `/etc/lumen/lumen.env`**, and starts the service. Anything the script doesn't carry, add by
hand, then `systemctl restart lumen`. It logs which keys it carried. (Note: `systemctl enable --now`
does **not** restart an already-running service — run `systemctl restart lumen` after a code sync.)

## Runbook (manual, equivalent)

```bash
# 1. on the VM
sudo useradd -r -m -d /opt/lumen lumen
sudo -u lumen git clone <your repo> /opt/lumen/app && cd /opt/lumen/app
sudo -u lumen make install                      # pip install ml + server requirements

# 2. secrets and config (never commit these)
sudo mkdir -p /etc/lumen
sudo cp deploy/env.production.example /etc/lumen/lumen.env
sudo chmod 600 /etc/lumen/lumen.env
# edit it: set KAZE_ADMIN_TOKEN, KAZE_SECRET, KAZE_BASE_URL=https://DOMAIN, and any provider keys
python -c 'import secrets;print(secrets.token_urlsafe(32))'   # use for the two secrets

# 3. services
sudo cp deploy/lumen.service /etc/systemd/system/lumen.service
sudo sed -i 's/DOMAIN/your.domain/' deploy/Caddyfile
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile
sudo systemctl daemon-reload && sudo systemctl enable --now lumen
sudo systemctl reload caddy

# 4. verify
curl https://your.domain/api/health
curl https://your.domain/api/kaze/health
curl -H "X-Admin-Token: $KAZE_ADMIN_TOKEN" https://your.domain/api/kaze/admin/health
```

## Troubleshooting

- **No certificate / `tlsv1 alert internal error` and Caddy logs `Timeout during connect (likely firewall problem)`**
  — Let's Encrypt can't reach 80/443. Some Ubuntu images ship with **ufw active (SSH only)**. Open them:
  `sudo ufw allow 80/tcp && sudo ufw allow 443/tcp && sudo ufw allow 443/udp && sudo systemctl restart caddy`.
  Also check any **Vultr Firewall Group** attached to the instance allows 80/443. `install.sh` now opens ufw
  automatically when it is active.
- **`/api/kaze/health` returns 503** — KAZE failed to start (the Lumen core still runs). Usually `psycopg`
  missing while `KAZE_DB_URL` points at Postgres: `sudo -u lumen /opt/lumen/app/.venv/bin/pip install "psycopg[binary]"`.
  Check `journalctl -u lumen | grep kaze`.
- **Cert issued but `curl` warns about the hostname** — the `DOMAIN` you passed to `install.sh` must match the
  `Caddyfile` name and resolve to the VM.
- **No spoken voice / read-back is silent** — `server/tts.py` uses ElevenLabs when `ELEVENLABS_API_KEY` is set,
  otherwise it falls back to the browser's `speechSynthesis` (which a headset often lacks). On a **free** ElevenLabs
  plan only a voice you **own** works through the API: a library voice returns HTTP 402 and the server silently
  falls back to browser speech. Make sure `ELEVENLABS_VOICE_ID` is carried (it is now in `CARRY_KEYS`) and set to
  your own voice. Check `journalctl -u lumen | grep tts_error`.
- **No clinician captions** — `captions.py` needs audio. A **cloud VM has no microphone**, so `ENABLE_CAPTIONS=1`
  there logs `[captions] sounddevice unavailable` (harmless). Captions still work because the browser streams its
  mic audio to the server's Whisper — open `/dashboard` and press the mic (Chrome/Edge use built-in speech
  recognition; other browsers stream to the server). The first caption downloads the Whisper model
  (`WHISPER_MODEL`, ~150 MB) and on a 1-vCPU box a few-second utterance takes a few seconds to transcribe.
- **Gemini returns HTTP 429 in bursts** — the free tier's requests-per-minute is lower than one signed phrase
  costs (one normalizer call plus up to 8 planner rounds), so signing quickly trips it. Every call now goes
  through `server/gemini_gate.py`: calls are serialised, 429/5xx/hangs retry with exponential backoff, and after
  `GEMINI_429_TRIP` 429s inside `GEMINI_429_WINDOW_S` the breaker **opens** and the app runs on the rule
  normalizer + offline planner for `GEMINI_COOLDOWN_S` seconds (phrases still speak, just less fluently) before
  half-opening a real call again. If the 429 says **"You exceeded your current quota"** the free plan's
  quota is spent for the day, so the gate opens on that first hit (`GEMINI_QUOTA_COOLDOWN_S`) instead of
  retrying. Watch `journalctl -u lumen | grep -iE "429|breaker"`. To spend no quota at all
  set `NORMALIZER=rule` and `LLM_PROVIDER=offline`; to raise the ceiling, upgrade the key.

## Data and database

- Default storage is **SQLite** under `SIGNBRIDGE_DATA` (set it to `/var/lib/lumen` in `lumen.env`).
  Keep that directory on a real disk with backups; it holds `server.sqlite`, `kaze.sqlite`,
  `kaze_admin_token.txt`, `kaze_secret.txt`, recordings and progress.
- For Postgres / Tiger Data set `KAZE_DB_URL` (or `DATABASE_URL`) to the connection string from the
  Tiger Data console, e.g. `postgresql://user:pass@host:5432/tsdb?sslmode=require`. Install
  `psycopg[binary]` (already in `server/requirements.txt`). With `KAZE_TIMESCALE=1` and the
  `timescaledb` extension available the event table becomes a hypertable automatically; otherwise it
  stays plain Postgres. Keep `KAZE_SECRET` and `KAZE_ADMIN_TOKEN` stable across restarts or existing
  device tokens and contact links stop validating. See `docs/KAZE.md` §5.

## Notifications (real SMS / push)

The clinic SMS and the KAZE alert both go through `server/notify.py`. Set **one** free backend in
`lumen.env` (`TELEGRAM_*`, `NTFY_TOPIC`, or `NOTIFY_WEBHOOK_URL`) or Twilio (`TWILIO_*` +
`EMERGENCY_CONTACT`). With none set the server runs an honest dry run and sends nothing. Details and
step-by-step bot setup: **`docs/NOTIFICATIONS.md`**.

## Rate limits and tokens (already in code)

- Admin: `X-Admin-Token` / `Authorization: Bearer` (`KAZE_ADMIN_TOKEN`).
- Device: per-user bearer token; 240 requests/minute.
- PIN endpoints: 12 attempts/minute per person; 5 wrong PINs inside 5 minutes locks the pad for 5 minutes.
- Contact links: 90 requests/minute per link; the link token is HMAC-derived per alert and dies with it.
- Gemini: serialised calls, retry with backoff, and a circuit breaker (`gemini_gate.py`) so a rate-limit burst
  degrades to the rule normalizer / offline planner instead of storming the API — see the 429 bullet above.
- Put Caddy (or your proxy) in front; the app already refuses unauthenticated traffic.

## Venue fallback (no internet)

For a demo with no venue Wi-Fi, run everything locally and reach the server over USB:

```bash
adb reverse tcp:8000 tcp:8000            # the headset reaches the laptop at localhost:8000
LLM_PROVIDER=offline NORMALIZER=rule make run
```

`Caddyfile.local.example` does plain HTTP on `localhost` if you want a proxy locally too. The headset
needs `make vendor` first so three.js / onnxruntime-web are served from the box.

## Before any real deployment

- Move off a single node; add monitoring/paging and a real notifier (SMS provider) with delivery receipts.
- Replace the generated admin token/secret with managed secrets.
- Complete the validation list in `docs/KAZE.md` §7 (penetration test, privacy/legal review, user testing
  with ethics approval) and the device/Quest checks in `Lumen_TODO.md` §1.
