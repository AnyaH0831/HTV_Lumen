#!/usr/bin/env bash
# One-shot server setup for Lumen + KAZE on a fresh Ubuntu (Debian-family) VM.
#
#   DOMAIN=155.138.210.93.sslip.io bash /root/lumen/deploy/install.sh
#
# Run as root. It expects the repo to have been copied to /root/lumen first
# (see deploy/README.md) or pass SRC=/somewhere/lumen. Safe to re-run.
#
# No domain? Use "<IP>.sslip.io" (e.g. 155.138.210.93.sslip.io) -- it resolves to
# your IP and Caddy can still get a real Let's Encrypt certificate, which the
# headset needs for WebXR (a secure context).
set -euo pipefail

DOMAIN="${DOMAIN:-}"
SRC="${SRC:-/root/lumen}"
APP=/opt/lumen/app
DATA=/var/lib/lumen
SVC=lumen
ENVF=/etc/lumen/lumen.env

log(){ printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
[ "$(id -u)" = 0 ] || { echo "Run as root."; exit 1; }
[ -d "$SRC" ] || { echo "Repo not found at $SRC (set SRC=/path/to/lumen)."; exit 1; }

log "Base packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3-venv python3-pip rsync ca-certificates curl gnupg \
  debian-keyring debian-archive-keyring apt-transport-https

# Small boxes can OOM while pip builds wheels; give the box some swap.
if ! swapon --show 2>/dev/null | grep -q . && [ ! -f /swapfile ]; then
  log "Adding 1 GB swap (pip is memory-hungry on 1 GB boxes)"
  fallocate -l 1G /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=1024 status=none
  chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

log "Service user and directories"
id "$SVC" >/dev/null 2>&1 || useradd -r -m -d /opt/lumen "$SVC"
mkdir -p "$APP" "$DATA" /etc/lumen /var/log/caddy

log "Installing code -> $APP"
if [ "$(readlink -f "$SRC")" != "$(readlink -f "$APP")" ]; then
  rsync -a --delete \
    --exclude '.git' --exclude 'data' --exclude 'runs' --exclude '.venv' \
    --exclude '__pycache__' --exclude 'node_modules' --exclude '.env' --exclude '*.sqlite' \
    --exclude '*.pyc' --exclude '.pytest_cache' \
    "$SRC"/ "$APP"/
fi
chown -R "$SVC:$SVC" /opt/lumen "$DATA"

log "Virtualenv + server dependencies"
sudo -u "$SVC" python3 -m venv "$APP/.venv"
sudo -u "$SVC" "$APP/.venv/bin/pip" install -q --upgrade pip
sudo -u "$SVC" "$APP/.venv/bin/pip" install -q -r "$APP/server/requirements.txt"

log "Environment file $ENVF"
ENVSRC="${ENVSRC:-$SRC/.env}"        # the local .env you rsync'd up (holds KAZE_DB_URL etc.)
if [ ! -f "$ENVF" ]; then
  cp "$APP/deploy/env.production.example" "$ENVF"
  echo "  created $ENVF from the example"
fi
chmod 600 "$ENVF"

set_env() {  # set_env KEY VALUE -- idempotent; matches the key whether commented or not.
  # awk (not `sed -i`) so it behaves the same on GNU and BSD sed. Values may contain &, |, /.
  local k="$1" v="$2"
  awk -v k="$k" -v v="$v" '
    $0 ~ "^[[:space:]]*#?[[:space:]]*" k "=" { print k "=" v; found=1; next }
    { print }
    END { if (!found) print k "=" v }
  ' "$ENVF" > "$ENVF.tmp" && chmod 600 "$ENVF.tmp" && mv "$ENVF.tmp" "$ENVF"
}

# Generated secrets: create once and keep stable across re-runs.
# [^[:space:]#] so a blank value or a trailing "# comment" is not mistaken for "already set".
grep -qE '^KAZE_ADMIN_TOKEN=[^[:space:]#]' "$ENVF" || set_env KAZE_ADMIN_TOKEN "$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
grep -qE '^KAZE_SECRET=[^[:space:]#]'      "$ENVF" || set_env KAZE_SECRET      "$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
set_env SIGNBRIDGE_DATA "$DATA"
if [ -n "$DOMAIN" ]; then
  set_env KAZE_BASE_URL "https://$DOMAIN"
else
  ip=$(hostname -I 2>/dev/null | awk '{print $1}')            # no domain: best-effort so links aren't https://DOMAIN
  [ -n "$ip" ] && set_env KAZE_BASE_URL "http://$ip"
fi

# Carry your real credentials from the local .env (whatever you actually set wins).
CARRY_KEYS="KAZE_DB_URL KAZE_TIMESCALE EMERGENCY_CONTACT TWILIO_SID TWILIO_TOKEN TWILIO_FROM TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID NTFY_TOPIC NTFY_SERVER NOTIFY_WEBHOOK_URL ANTHROPIC_API_KEY ANTHROPIC_MODEL GEMINI_API_KEY GEMINI_MODEL LLM_PROVIDER NORMALIZER ELEVENLABS_API_KEY ELEVENLABS_VOICE_ID ELEVENLABS_MODEL ENABLE_CAPTIONS WHISPER_MODEL VAD_RMS LUMEN_AUTONOMY"
if [ -f "$ENVSRC" ]; then
  carried=""
  for k in $CARRY_KEYS; do
    v=$( { grep -E "^[[:space:]]*${k}=" "$ENVSRC" 2>/dev/null || true; } | tail -1 | cut -d= -f2-)
    # drop a trailing "# comment" and surrounding quotes/space; skip empty or comment-only values
    v=$(printf '%s' "$v" | sed -E -e 's/[[:space:]]+#.*$//' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//")
    if [ -n "$v" ] && [ "${v#\#}" = "$v" ]; then set_env "$k" "$v"; carried="$carried $k"; fi
  done
  echo "  carried from $ENVSRC:${carried:- (nothing set)}"
  grep -qE '^KAZE_DB_URL=[^[:space:]#]' "$ENVF" \
    && echo "  KAZE_DB_URL set -> KAZE will use Postgres/Tiger Data" \
    || echo "  no KAZE_DB_URL: KAZE will use SQLite under $DATA"
else
  echo "  no $ENVSRC to carry credentials from -- edit $ENVF by hand"
fi

log "systemd service"
cp "$APP/deploy/lumen.service" /etc/systemd/system/lumen.service
systemctl daemon-reload
systemctl enable --now lumen
sleep 2
systemctl --no-pager --full status lumen | head -n 12 || true

if [ -n "$DOMAIN" ]; then
  # Some Ubuntu images ship with ufw active (SSH only). Let's Encrypt must reach 80/443 or no cert is issued.
  if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q '^Status: active'; then
    log "Firewall (ufw is active: allowing 80/443)"
    ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null; ufw allow 443/udp >/dev/null
  fi
  log "Caddy + automatic HTTPS for $DOMAIN"
  if ! command -v caddy >/dev/null 2>&1; then
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
      | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
      > /etc/apt/sources.list.d/caddy-stable.list
    apt-get update -y && apt-get install -y caddy
  fi
  chown caddy:caddy /var/log/caddy 2>/dev/null || true
  sed "s/DOMAIN/$DOMAIN/g" "$APP/deploy/Caddyfile" > /etc/caddy/Caddyfile
  systemctl enable caddy 2>/dev/null || true
  systemctl reload caddy 2>/dev/null || systemctl restart caddy
  log "Verifying https://$DOMAIN"
  for i in 1 2 3 4 5; do
    if curl -fsS "https://$DOMAIN/api/health"; then echo; break; fi
    echo "  waiting for the certificate... ($i)"; sleep 5
  done
  echo
  curl -fsS "https://$DOMAIN/api/kaze/health" || true; echo
else
  log "No DOMAIN set: the app is on 127.0.0.1:8000 only"
  echo "  Re-run with:  DOMAIN=155.138.210.93.sslip.io bash $APP/deploy/install.sh"
  echo "  (the headset needs HTTPS for WebXR, so set a DOMAIN before using the headset)"
fi

log "Done"
cat <<EOF
Next:
  1. Review $ENVF (KAZE_DB_URL and any notification keys were carried from your local .env).
     Add anything missing, e.g. TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... EMERGENCY_CONTACT=+1...
  2. systemctl restart lumen     # only needed if you change the env
  3. Open https://${DOMAIN:-<your-domain>}/kaze/setup and create a person.
EOF
