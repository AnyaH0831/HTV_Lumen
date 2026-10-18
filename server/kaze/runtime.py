"""Wires the service to the outside world: configuration from the environment, the ticker, websocket hubs, rate limiting."""
import asyncio, os, secrets
from pathlib import Path
from . import models as M
from .db import connect
from .service import KazeService, MirroringNotifier

PROMPT_KINDS = {"checkin_due", "alert_pending", "alert_cancelled", "alert_sent", "acknowledged", "resolved"}


class RateLimiter:
    """Sliding window per key, in memory. PIN endpoints are limited per person so a stolen phone cannot be used to guess a PIN quickly."""
    def __init__(self, clock=None): import time; self.clock = clock or time.monotonic; self.hits = {}

    def allow(self, key, limit, window_s=60.0):
        now = self.clock(); q = [t for t in self.hits.get(key, []) if now - t < window_s]
        if len(q) >= limit: self.hits[key] = q; return False
        q.append(now); self.hits[key] = q
        if len(self.hits) > 5000: self.hits = {k: v for k, v in self.hits.items() if v and now - v[-1] < window_s}
        return True


def _data_dir() -> Path: return Path(os.getenv("SIGNBRIDGE_DATA") or "data")


def _secret_file(name, make):
    p = _data_dir() / name
    if p.is_file(): return p.read_text().strip()
    p.parent.mkdir(parents=True, exist_ok=True); v = make(); p.write_text(v); os.chmod(p, 0o600); return v


class KazeRuntime:
    def __init__(self):
        self.service = None; self.admin_token = None; self.task = None; self.loop = None
        self.admin_ws = set(); self.device_ws = {}; self.limiter = RateLimiter(); self.settings = {}

    def configure(self, **kw): self.settings.update(kw); return self

    def build_service(self):
        s = self.settings
        url = s.get("db_url") or os.getenv("KAZE_DB_URL") or os.getenv("DATABASE_URL") or f"sqlite:///{_data_dir() / 'kaze.sqlite'}"
        self.admin_token = s.get("admin_token") or os.getenv("KAZE_ADMIN_TOKEN")
        if not self.admin_token:
            self.admin_token = _secret_file("kaze_admin_token.txt", M.new_token)
            print(f"[kaze] KAZE_ADMIN_TOKEN is not set: using the generated one in {_data_dir() / 'kaze_admin_token.txt'}")
        secret = s.get("secret") or (os.getenv("KAZE_SECRET") or _secret_file("kaze_secret.txt", lambda: secrets.token_hex(32))).encode()
        self.service = KazeService(connect(url), clock=s.get("clock") or __import__("time").time, notifier=MirroringNotifier(),
                                   secret=secret, base_url=s.get("base_url") or os.getenv("KAZE_BASE_URL", "http://localhost:8000"),
                                   scrypt_n=int(s.get("scrypt_n") or os.getenv("KAZE_SCRYPT_N", 2 ** 14)))
        self.service.listeners.append(self._on_event); return self.service

    async def start(self):
        self.loop = asyncio.get_running_loop()
        if not self.service: self.build_service()
        self.task = asyncio.create_task(self._ticker())

    async def stop(self):
        if self.task: self.task.cancel()
        try: await self.task
        except (asyncio.CancelledError, Exception): pass
        self.task = None
        if self.service: self.service.db.close(); self.service = None

    async def _ticker(self):
        """Runs for the life of the server. An exception in one tick is counted and the loop carries on: the countdown must never stop."""
        while True:
            try: self.service.tick()
            except Exception: self.service.tick_errors += 1
            await asyncio.sleep(0.5)

    # ---- live push to consoles and devices
    def _on_event(self, ev):
        if self.loop and (self.admin_ws or self.device_ws): self.loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self._push(ev)))

    async def _push(self, ev):
        for ws in list(self.admin_ws):
            try: await ws.send_json({"type": "event", **ev})
            except Exception: self.admin_ws.discard(ws)
        if "u" in ev["vis"] and ev["kind"] in PROMPT_KINDS:
            for ws in list(self.device_ws.get(ev["user_id"], ())):
                try: await ws.send_json({"type": "prompt", "kind": ev["kind"], "payload": ev["payload"]})
                except Exception: self.device_ws[ev["user_id"]].discard(ws)


runtime = KazeRuntime()
