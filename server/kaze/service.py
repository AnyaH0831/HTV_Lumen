"""The KAZE service: a server-authoritative safety timer with duress semantics. Everything is synchronous and runs on the server,
so a phone that disconnects, a headset that is taken off, or a client that crashes cannot stop an alert that is already counting down.

States (real, internal):  WATCHING -> CHECKIN_DUE -> ALERT_PENDING -> ESCALATING -> ACKNOWLEDGED -> RESOLVED
  WATCHING       waiting for the next scheduled check-in
  CHECKIN_DUE    the person is prompted and has `response_window_s` to confirm they are safe
  ALERT_PENDING  check-in missed: `alert_grace_s` countdown; only a PIN (safe or duress) can cancel it, a tap cannot
  ESCALATING     contacts are told one at a time; each has `ack_timeout_s` to acknowledge before the next is tried
  ACKNOWLEDGED   a contact has acknowledged; resolved by the person's PIN or by an authorised contact

Duress: the duress PIN looks exactly like the safe PIN to whoever is watching the screen (same reply, same screen afterwards), but the
server starts a SILENT escalation. From then on the device only ever sees a harmless "shadow" schedule, and the person's own safe PIN
can no longer cancel it. Only a contact (or an admin) can end it.

Time is injected (`clock`), so the same code runs live and, in tests and simulations, on a fake clock.
Nothing here sends a real message: notifications go through a simulated notifier."""
import functools, hmac, math, secrets, threading, time
from . import models as M
from .db import KazeDB

HEARTBEAT_STALE_S = 120
LOCATION_MIN_GAP_S = 5
RELEASE_LOG_GAP_S = 30


class FakeClock:
    def __init__(self, t=1_700_000_000.0): self.t = t
    def __call__(self): return self.t
    def advance(self, dt): self.t += dt


class SimulatedNotifier:
    """No real SMS, call or push is ever sent. `unreachable` simulates a contact whose phone cannot be reached."""
    def __init__(self): self.unreachable = set()
    def deliver(self, contact, text): return contact["id"] not in self.unreachable


class MirroringNotifier(SimulatedNotifier):
    """Live-runtime notifier: KAZE still treats every message as simulated (same escalation, `unreachable`, audit trail),
    but when a real transport is configured (Twilio/Telegram/ntfy/webhook -- see server/notify.py) the message is ALSO
    sent for real, in a background thread, so a slow phone cannot stall the 0.5 s ticker."""
    def deliver(self, contact, text):
        ok = super().deliver(contact, text)
        if ok:
            try:
                from ..notify import mirror
                mirror(f"KAZE · {contact.get('name', 'a contact')}: {text}")
            except Exception:
                pass
        return ok


class KazeError(Exception):
    def __init__(self, code, status=400, **extra): super().__init__(code); self.code, self.status, self.extra = code, status, extra


def _ceil(x): return max(0, int(math.ceil(x)))


class KazeService:
    def __init__(self, db: KazeDB, clock=time.time, notifier=None, secret: bytes | None = None, base_url="", scrypt_n=2 ** 14, location_retention_s=3600):
        self.db, self.clock, self.notifier = db, clock, notifier or SimulatedNotifier()
        self.secret = secret or secrets.token_bytes(32); self.base_url = base_url.rstrip("/"); self.scrypt_n = scrypt_n
        self.location_retention_s = location_retention_s
        self.listeners = []; self.last_tick = 0.0; self.tick_errors = 0; self._last_purge = 0.0; self.lock = threading.RLock()
        self._dummy = "0" * 64

    def now(self): return self.clock()

    # ------------------------------------------------------------------ plumbing
    def _event(self, s, uid, kind, vis, at=None, **payload):
        ev = {"ts": self.now() if at is None else at, "id": M.event_id(), "session_id": s["id"] if s else None, "user_id": uid, "kind": kind, "vis": vis, "payload": payload}
        self.db.add_event(ev); self._dispatch(ev); return ev

    def _transient(self, uid, kind, vis="u", **payload):          # a prompt for the device that is not part of the audit trail
        self._dispatch({"ts": self.now(), "id": M.new_id(), "session_id": None, "user_id": uid, "kind": kind, "vis": vis, "payload": payload, "transient": True})

    def _dispatch(self, ev):
        for fn in self.listeners:
            try: fn(ev)
            except Exception: pass

    def _user(self, uid):
        u = self.db.get_user(uid)
        if not u: raise KazeError("no_user", 404)
        return u

    def _save(self, s): s["version"] += 1; self.db.put_session(s)
    def _save_user(self, u): self.db.put_user(u)

    def _active(self, user):
        sid = user.get("active_session_id"); s = self.db.get_session(sid) if sid else None
        return s if s and s["status"] != M.RESOLVED else None

    # ------------------------------------------------------------------ users and consent
    def create_user(self, name, safe_pin, duress_pin, contacts, config=None, location_policy="on_alert", preset="realistic"):
        name = str(name).strip()
        if not 1 <= len(name) <= 60: raise KazeError("bad_name")
        if not (M.valid_pin(safe_pin) and M.valid_pin(duress_pin)): raise KazeError("bad_pin", detail="a PIN is 4 to 64 characters: digits, or a phrase of signs")
        if M.normalize_pin(safe_pin) == M.normalize_pin(duress_pin): raise KazeError("duress_must_differ")
        if location_policy not in M.LOCATION_POLICIES: raise KazeError("bad_location_policy")
        if not 1 <= len(contacts) <= 5: raise KazeError("need_1_to_5_contacts")
        cs = []
        for c in contacts:
            perms = c.get("permissions") or {}
            lvl = perms.get("location", "none")
            if lvl not in M.LOCATION_LEVELS: raise KazeError("bad_location_level")
            cs.append({"id": M.new_id(), "name": str(c["name"]).strip()[:60], "channel": c.get("channel", "sms") if c.get("channel", "sms") in ("sms", "call", "push") else "sms",
                       "permissions": {"location": lvl, "can_resolve": bool(perms.get("can_resolve", False))}})
        try: cfg = M.clean_config(config, base=M.DEMO if preset == "demo" else M.REALISTIC)
        except ValueError as e: raise KazeError("bad_config", detail=str(e))
        uid, token, salt = M.new_id(), M.new_token(), secrets.token_bytes(16)
        user = {"id": uid, "name": name, "created": self.now(), "token_hash": M.hash_token(token), "salt": salt.hex(),
                "safe_hash": M.hash_pin(safe_pin, salt, self.scrypt_n), "duress_hash": M.hash_pin(duress_pin, salt, self.scrypt_n),
                "contacts": cs, "location_policy": location_policy, "config": cfg, "active_session_id": None, "pin_fails": [], "locked_until": 0.0, "sim_offline": False}
        self._save_user(user); self._event(None, uid, "user_created", "ua", contacts=len(cs), location_policy=location_policy)
        return uid, f"{uid}.{token}"

    def authenticate_device(self, device_token):
        try: uid, token = device_token.split(".", 1)
        except (ValueError, AttributeError): return None
        u = self.db.get_user(uid)
        return uid if u and hmac.compare_digest(u["token_hash"], M.hash_token(token)) else None

    DEVICE_HIDDEN_CONFIG = ("duress_ack_timeout_s",)             # internal tuning that would hint at the duress mechanism to anyone reading the device's data

    def public_profile(self, uid, admin=False):
        u = self._user(uid); cfg = u["config"] if admin else {k: v for k, v in u["config"].items() if k not in self.DEVICE_HIDDEN_CONFIG}
        return {"id": u["id"], "name": u["name"], "location_policy": u["location_policy"], "config": cfg, "sim_offline": u.get("sim_offline", False),
                "contacts": [{"id": c["id"], "name": c["name"], "channel": c["channel"], "permissions": c["permissions"]} for c in u["contacts"]]}

    def list_users(self):
        return [{"id": u["id"], "name": u["name"], "active": bool(self._active(u)), "sim_offline": u.get("sim_offline", False),
                 "config": u["config"], "location_policy": u["location_policy"],
                 "contacts": [{"id": c["id"], "name": c["name"], "channel": c["channel"], "permissions": c["permissions"]} for c in u["contacts"]]}
                for u in self.db.list_users()]

    def update_user(self, uid, *, name=None, config=None, location_policy=None, safe_pin=None, duress_pin=None, contacts=None):
        """Edit a person's setup after creation: check-in timings, contacts, PINs, consent. Every field is optional.
        A running session keeps the config it started with; `POST /admin/users/{uid}/watch` can start the next one with an override."""
        u = self._user(uid); changed = {}
        if name is not None:
            name = str(name).strip()
            if not 1 <= len(name) <= 60: raise KazeError("bad_name")
            u["name"] = name; changed["name"] = name
        salt = bytes.fromhex(u["salt"])
        if safe_pin is not None or duress_pin is not None:
            if safe_pin is not None and not M.valid_pin(safe_pin): raise KazeError("bad_pin")
            if duress_pin is not None and not M.valid_pin(duress_pin): raise KazeError("bad_pin")
            if safe_pin is not None and duress_pin is not None and M.normalize_pin(safe_pin) == M.normalize_pin(duress_pin): raise KazeError("duress_must_differ")
            if safe_pin is not None and hmac.compare_digest(M.hash_pin(safe_pin, salt, self.scrypt_n), u["duress_hash"] or self._dummy): raise KazeError("duress_must_differ")
            if duress_pin is not None and hmac.compare_digest(M.hash_pin(duress_pin, salt, self.scrypt_n), u["safe_hash"]): raise KazeError("duress_must_differ")
            if safe_pin is not None: u["safe_hash"] = M.hash_pin(safe_pin, salt, self.scrypt_n)
            if duress_pin is not None: u["duress_hash"] = M.hash_pin(duress_pin, salt, self.scrypt_n)
            u["pin_fails"] = []; changed["pins"] = "updated"
        if config is not None:
            try: u["config"] = M.clean_config(config, base=u["config"])
            except ValueError as e: raise KazeError("bad_config", detail=str(e))
            changed["config"] = u["config"]
        if location_policy is not None:
            if location_policy not in M.LOCATION_POLICIES: raise KazeError("bad_location_policy")
            u["location_policy"] = location_policy; changed["location_policy"] = location_policy
            if location_policy == "never": self.db.purge_locations(uid)
        if contacts is not None:
            if not 1 <= len(contacts) <= 5: raise KazeError("need_1_to_5_contacts")
            cs = []
            for c in contacts:
                perms = c.get("permissions") or {}; lvl = perms.get("location", "none")
                if lvl not in M.LOCATION_LEVELS: raise KazeError("bad_location_level")
                cs.append({"id": c.get("id") or M.new_id(), "name": str(c["name"]).strip()[:60],
                           "channel": c.get("channel", "sms") if c.get("channel", "sms") in ("sms", "call", "push") else "sms",
                           "permissions": {"location": lvl, "can_resolve": bool(perms.get("can_resolve", False))}})
            u["contacts"] = cs; changed["contacts"] = len(cs)
        self._save_user(u); self._event(self._active(u), uid, "user_updated", "ua", **changed)
        return self.public_profile(uid, admin=True)

    def rotate_device_token(self, uid):
        """Issue a fresh device token (the old one stops working). The token is shown once, so this is the way to re-pair a device."""
        u = self._user(uid); token = M.new_token(); u["token_hash"] = M.hash_token(token); self._save_user(u)
        self._event(self._active(u), uid, "device_token_rotated", "ua"); return f"{uid}.{token}"

    def _ok(self, uid): return {"ok": True, **self.public_state(uid)}

    def admin_state(self, uid):
        """Operator view: the REAL status (including duress) plus the device's outward state, for the laptop monitor."""
        u = self._user(uid); s = self._active(u)
        if not s: return {"state": "idle", "active": False, "status": "idle", "duress": False}
        return {**self.public_state(uid), "active": True, "session_id": s["id"], "status": s["status"], "duress": s["duress"],
                "round": s["round"], "origin": s.get("origin"), "exhausted": s.get("exhausted", False),
                "contacts_notified": len(s.get("notified", []))}

    def update_consent(self, uid, location_policy=None, contacts=None):
        u = self._user(uid); changed = {}
        if location_policy is not None:
            if location_policy not in M.LOCATION_POLICIES: raise KazeError("bad_location_policy")
            u["location_policy"] = location_policy; changed["location_policy"] = location_policy
            if location_policy == "never": self.db.purge_locations(uid)           # withdrawing consent deletes what is held
        for upd in contacts or []:
            c = next((c for c in u["contacts"] if c["id"] == upd.get("id")), None)
            if not c: raise KazeError("no_such_contact", 404)
            if "location" in upd:
                if upd["location"] not in M.LOCATION_LEVELS: raise KazeError("bad_location_level")
                c["permissions"]["location"] = upd["location"]
            if "can_resolve" in upd: c["permissions"]["can_resolve"] = bool(upd["can_resolve"])
            changed[c["name"]] = c["permissions"]
        self._save_user(u); self._event(self._active(u), uid, "consent_changed", "ua", **{k: v for k, v in changed.items()})
        return self.public_profile(uid)

    def erase_user(self, uid):
        self._user(uid); self.db.delete_user(uid); return {"erased": True}

    # ------------------------------------------------------------------ PINs
    def _check_pin(self, user, pin):
        """'safe' | 'duress' | 'wrong' | 'locked'. One hash is computed and compared against BOTH stored hashes, so timing does not
        reveal which PIN was entered (or that a duress PIN exists)."""
        now = self.now()
        if user.get("locked_until", 0) > now: return "locked", {"retry_in": _ceil(user["locked_until"] - now)}
        h = M.hash_pin(str(pin)[:128], bytes.fromhex(user["salt"]), self.scrypt_n)
        is_safe, is_duress = hmac.compare_digest(h, user["safe_hash"]), hmac.compare_digest(h, user["duress_hash"] or self._dummy)
        if is_safe or is_duress:
            if user["pin_fails"]: user["pin_fails"] = []; self._save_user(user)
            return ("duress" if is_duress else "safe"), {}
        fails = [t for t in user["pin_fails"] if now - t < M.PIN_FAIL_WINDOW_S] + [now]
        if len(fails) >= M.PIN_FAIL_LIMIT:
            user["pin_fails"] = []; user["locked_until"] = now + M.LOCKOUT_S; self._save_user(user)
            self._event(self._active(user), user["id"], "pin_lockout", "uca", locked_for_s=M.LOCKOUT_S)     # the alert itself is NOT affected by a lockout
            return "locked", {"retry_in": M.LOCKOUT_S}
        user["pin_fails"] = fails; self._save_user(user)
        self._event(self._active(user), user["id"], "pin_failed", "ua", attempts_left=M.PIN_FAIL_LIMIT - len(fails))
        return "wrong", {"attempts_left": M.PIN_FAIL_LIMIT - len(fails)}

    # ------------------------------------------------------------------ watching and check-ins
    def start_watch(self, uid, config=None):
        user = self._user(uid); cur = self._active(user)
        try: cfg = M.clean_config(config, base=user["config"])
        except ValueError as e: raise KazeError("bad_config", detail=str(e))
        if cur:
            if cur["status"] == M.WATCHING and cur["config"] != cfg:
                cur["config"] = cfg; cur["next_checkin_at"] = self.now() + cfg["checkin_interval_s"]; self._save(cur)
                self._event(cur, uid, "watch_updated", "ua", next_checkin_in=cfg["checkin_interval_s"])
            return cur
        now = self.now()
        s = {"id": M.new_id(), "user_id": uid, "status": M.WATCHING, "config": cfg, "started_at": now, "next_checkin_at": now + cfg["checkin_interval_s"],
             "due_deadline": None, "alert_fire_at": None, "contact_idx": -1, "round": 0, "contact_deadline": None, "notified": [], "responders": [],
             "acked_by": None, "acked_at": None, "resolved_at": None, "resolved_by": None, "resolved_reason": None, "duress": False, "duress_at": None,
             "origin": None, "shadow": None, "last_seen": now, "exhausted": False, "loc_release_t": {}, "version": 0}
        user["active_session_id"] = s["id"]; self._save(s); self._save_user(user)
        self._event(s, uid, "watch_started", "ua", next_checkin_in=cfg["checkin_interval_s"]); return s

    def stop_watch(self, uid, pin):
        user = self._user(uid); s = self._active(user)
        if not s: raise KazeError("no_watch", 409)
        kind, extra = self._check_pin(user, pin)
        if kind == "locked": raise KazeError("locked", 429, **extra)
        if kind == "wrong": raise KazeError("wrong_pin", 401, **extra)
        if kind == "duress":                                      # "turn it off" under coercion: it looks off, but a silent alert starts
            self._duress(user, s); s = self.db.get_session(s["id"]); s["shadow"]["state"] = "stopped"; self._save(s)
            return {"ok": True, "state": "idle"}
        if s["duress"]: return {"ok": True, "state": "idle"}      # a safe PIN cannot end a duress alert
        self._resolve(s, user, by="user", reason="user_stopped", resume=False); return {"ok": True, "state": "idle"}

    def heartbeat(self, uid):
        user = self._user(uid); s = self._active(user)
        if s: s["last_seen"] = self.now(); self.db.put_session(s)
        return self.public_state(uid)

    def public_state(self, uid):
        """What the person's own device may show. For a duress session this is the shadow schedule, never the real state."""
        user = self._user(uid); s = self._active(user); now = self.now()
        if not s: return {"state": "idle"}
        if s["duress"]:
            sh = s["shadow"]
            if sh["state"] == "stopped": return {"state": "idle"}
            if sh["state"] == "checkin_due": return {"state": "checkin_due", "respond_within": _ceil(sh["deadline"] - now)}
            return {"state": "watching", "next_checkin_in": _ceil(sh["next_at"] - now)}
        st = s["status"]
        if st == M.WATCHING: return {"state": "watching", "next_checkin_in": _ceil(s["next_checkin_at"] - now)}
        if st == M.CHECKIN_DUE: return {"state": "checkin_due", "respond_within": _ceil(s["due_deadline"] - now)}
        if st == M.ALERT_PENDING: return {"state": "alert_pending", "alert_in": _ceil(s["alert_fire_at"] - now)}
        return {"state": "alert_sent", "help_on_the_way": st == M.ACKNOWLEDGED}

    def _ok(self, uid): return {"ok": True, **self.public_state(uid)}

    def checkin(self, uid, method="tap", pin=None):
        """The single entry point for "I am safe". The reply for a safe PIN and for a duress PIN is identical by construction."""
        user = self._user(uid); s = self._active(user)
        if not s: raise KazeError("no_watch", 409)
        if method not in ("tap", "pin"): raise KazeError("bad_method")
        kind = "safe"
        if method == "pin":
            kind, extra = self._check_pin(user, pin)
            if kind == "locked": raise KazeError("locked", 429, **extra)
            if kind == "wrong": raise KazeError("wrong_pin", 401, **extra)
        if kind == "duress": self._duress(user, s); return self._ok(uid)
        if s["duress"]: self._shadow_reset(s); return self._ok(uid)         # looks like a normal check-in; the real alert keeps running
        st, now = s["status"], self.now()
        if st in (M.WATCHING, M.CHECKIN_DUE):
            s["status"] = M.WATCHING; s["next_checkin_at"] = now + s["config"]["checkin_interval_s"]; s["due_deadline"] = None; s["last_seen"] = now
            self._save(s); self._event(s, uid, "checkin_ok", "ua", method=method)
        elif method != "pin": raise KazeError("pin_required", 409)          # a tap can never cancel an impending or active alert
        elif st == M.ALERT_PENDING:
            s["status"] = M.WATCHING; s["alert_fire_at"] = None; s["due_deadline"] = None; s["next_checkin_at"] = now + s["config"]["checkin_interval_s"]
            self._save(s); self._event(s, uid, "alert_cancelled", "ua", by="pin")
        elif st in M.ALERTING: self._resolve(s, user, by="user", reason="user_safe")
        return self._ok(uid)

    def _shadow_reset(self, s):
        s["shadow"] = {"state": "watching", "next_at": self.now() + s["config"]["checkin_interval_s"], "deadline": None}; self._save(s)

    # ------------------------------------------------------------------ duress
    def _duress(self, user, s):
        now, uid = self.now(), user["id"]
        if s["duress"]: self._shadow_reset(s); return
        was = s["status"]; s["duress"] = True; s["duress_at"] = now
        s["shadow"] = {"state": "watching", "next_at": now + s["config"]["checkin_interval_s"], "deadline": None}
        self._event(s, uid, "duress_signal", "ca", from_state=was)         # never visible on the person's own device
        if was in M.ALERTING:
            s["origin"] = "duress"; self._save(s)
            for n in s["notified"]: self._send(s, user, next(c for c in user["contacts"] if c["id"] == n["contact_id"]), now, "duress_update")
        else: self._begin_escalation(s, user, now, "duress")

    # ------------------------------------------------------------------ escalation
    def _link(self, s, c): return f"{self.base_url}/kaze/c/{M.contact_link_token(self.secret, s['id'], c['id'])}"

    def _text(self, s, user, c, kind):
        link = self._link(s, c)
        if kind == "alert": return f"KAZE: {user['name']} missed a safety check-in and may need help. Open: {link}", "KAZE: {name} missed a safety check-in and may need help. Open: {link}"
        if kind == "duress": return f"KAZE: {user['name']} sent a silent distress signal. Do NOT call or text them. Open: {link}", "KAZE: {name} sent a silent distress signal. Do NOT call or text them. Open: {link}"
        if kind == "duress_update": return f"KAZE: update - {user['name']}'s alert is now a silent distress signal. Do NOT call or text them. Open: {link}", "KAZE: update - silent distress signal. Do NOT call or text them. Open: {link}"
        return f"KAZE: {kind}", f"KAZE: {kind}"

    def _send(self, s, user, c, at, kind):
        text, stored = self._text(s, user, c, kind)
        delivered = bool(self.notifier.deliver(c, text))
        self._event(s, user["id"], "notify", "a", at=at, contact_id=c["id"], contact=c["name"], channel=c["channel"], msg=kind, delivered=delivered,
                    text=stored.replace("{name}", user["name"]).replace("{link}", "<per-alert link, not stored>"))
        if delivered and all(n["contact_id"] != c["id"] for n in s["notified"]): s["notified"].append({"contact_id": c["id"], "at": at})
        return delivered

    def _info(self, s, user, text, exclude=None):
        for n in s["notified"]:
            if n["contact_id"] == exclude: continue
            c = next((c for c in user["contacts"] if c["id"] == n["contact_id"]), None)
            if c: self.notifier.deliver(c, f"KAZE: {text}"); self._event(s, user["id"], "notify", "a", contact_id=c["id"], contact=c["name"], channel=c["channel"], msg="info", delivered=True, text=f"KAZE: {text}")

    def _begin_escalation(self, s, user, at, origin):
        s["status"] = M.ESCALATING; s["origin"] = origin; s["contact_idx"] = -1; s["round"] = 0; s["escalated_at"] = at
        self._event(s, user["id"], "alert_sent", "ca" if s["duress"] else "uca", at=at, origin=origin)
        self._notify_next(s, user, at); self._save(s)

    def _notify_next(self, s, user, at):
        cs, cfg = user["contacts"], s["config"]
        timeout = cfg["duress_ack_timeout_s"] if s["duress"] else cfg["ack_timeout_s"]
        while True:
            s["contact_idx"] += 1
            if s["contact_idx"] >= len(cs):
                s["round"] += 1
                if s["round"] < cfg["max_rounds"]: s["contact_idx"] = -1; continue
                s["exhausted"] = True; s["contact_deadline"] = None
                self._event(s, user["id"], "fallback_dispatch", "ca" if s["duress"] else "uca", at=at,
                            note="SIMULATED: no contact acknowledged. A real deployment would hand over to an emergency service here; this prototype never calls one.")
                return
            if self._send(s, user, cs[s["contact_idx"]], at, "duress" if s["duress"] else "alert"): s["contact_deadline"] = at + timeout; return

    def _resolve(self, s, user, by, reason, resume=True):
        now = self.now(); uid = user["id"]
        was_duress = s["duress"]
        s["status"] = M.RESOLVED; s["resolved_at"] = now; s["resolved_by"] = by; s["resolved_reason"] = reason; s["contact_deadline"] = None
        self._save(s)
        if s["notified"]: self._info(s, user, f"the alert for {user['name']} was resolved ({reason.replace('_', ' ')}).")
        self._event(s, uid, "resolved", "ca" if was_duress else "uca", by=by, reason=reason)
        self.db.purge_locations(uid)                                        # data minimisation: nothing is kept once the alert is over
        user["active_session_id"] = None; self._save_user(user)
        if resume and s["config"].get("auto_resume", True): self.start_watch(uid)

    # ------------------------------------------------------------------ the clock
    def tick(self):
        now = self.now(); self.last_tick = now
        for s in self.db.active_sessions():
            try: self._advance(s, now)
            except Exception as e:
                self.tick_errors += 1
                try: self._event(s, s["user_id"], "engine_error", "a", error=repr(e)[:200])
                except Exception: pass
        if now - self._last_purge > 60:
            self._last_purge = now
            try: self.db.purge_locations(before=now - self.location_retention_s)
            except Exception: self.tick_errors += 1

    def _advance(self, s, now):
        user = self.db.get_user(s["user_id"])
        if not user: return
        cfg, uid, dirty = s["config"], s["user_id"], False
        if s["duress"]: dirty |= self._advance_shadow(s, now, uid)
        while True:
            st = s["status"]
            if st == M.WATCHING and not s["duress"] and now >= s["next_checkin_at"]:
                s["status"] = M.CHECKIN_DUE; s["due_deadline"] = s["next_checkin_at"] + cfg["response_window_s"]
                self._event(s, uid, "checkin_due", "ua", at=s["next_checkin_at"], respond_within=cfg["response_window_s"])
            elif st == M.CHECKIN_DUE and now >= s["due_deadline"]:
                s["status"] = M.ALERT_PENDING; s["alert_fire_at"] = s["due_deadline"] + cfg["alert_grace_s"]
                self._event(s, uid, "alert_pending", "ua", at=s["due_deadline"], alert_in=cfg["alert_grace_s"])
            elif st == M.ALERT_PENDING and now >= s["alert_fire_at"]: self._begin_escalation(s, user, s["alert_fire_at"], "missed_checkin")
            elif st == M.ESCALATING and s["contact_deadline"] is not None and now >= s["contact_deadline"]: self._notify_next(s, user, s["contact_deadline"])
            else: break
            dirty = True
        if dirty: self._save(s)

    def _advance_shadow(self, s, now, uid):
        sh = s["shadow"]; cfg = s["config"]; dirty = False
        while sh["state"] != "stopped":
            if sh["state"] == "watching" and now >= sh["next_at"]:
                sh.update(state="checkin_due", deadline=sh["next_at"] + cfg["response_window_s"]); self._transient(uid, "checkin_due", respond_within=cfg["response_window_s"])
            elif sh["state"] == "checkin_due" and now >= sh["deadline"]: sh.update(state="watching", next_at=sh["deadline"] + cfg["checkin_interval_s"], deadline=None)
            else: break
            dirty = True
        return dirty

    # ------------------------------------------------------------------ location and the audit trail
    def post_location(self, uid, lat, lon, acc=None):
        user = self._user(uid)
        try: lat, lon = float(lat), float(lon)
        except (TypeError, ValueError): raise KazeError("bad_location")
        if not (-90 <= lat <= 90 and -180 <= lon <= 180): raise KazeError("bad_location")
        if user["location_policy"] == "never": return {"stored": False, "reason": "location sharing is off"}
        now, last = self.now(), self.db.latest_location(uid)
        if last and now - last["ts"] < LOCATION_MIN_GAP_S: return {"stored": False, "reason": "too frequent"}
        self.db.add_location(uid, now, lat, lon, acc); return {"stored": True}

    def _location_for(self, s, user, c):
        """The ONLY place location can leave the system: during an active alert, to contacts the person allowed, at the precision they chose."""
        level = c["permissions"]["location"]
        if user["location_policy"] == "never" or level == "none" or s["status"] not in M.ALERTING: return None
        loc = self.db.latest_location(user["id"])
        if not loc: return {"available": False}
        now = self.now(); lat, lon = (loc["lat"], loc["lon"]) if level == "precise" else M.coarse(loc["lat"], loc["lon"])
        if now - s["loc_release_t"].get(c["id"], -1e9) >= RELEASE_LOG_GAP_S:
            s["loc_release_t"][c["id"]] = now; self._save(s)
            self._event(s, user["id"], "location_released", "ca" if s["duress"] else "uca", contact=c["name"], precision=level)
        return {"available": True, "lat": lat, "lon": lon, "precision": level, "age_s": _ceil(now - loc["ts"])}

    def device_audit(self, uid, limit=100):
        """What the person may see about themselves. Duress internals are never included; location releases of a duress session appear once it is over."""
        user = self._user(uid); out = []
        for ev in self.db.events(user_id=uid, limit=limit * 3):
            visible = "u" in ev["vis"]
            if not visible and ev["kind"] == "location_released" and ev["session_id"]:
                sess = self.db.get_session(ev["session_id"]); visible = bool(sess and sess["status"] == M.RESOLVED)
            if visible and ev["kind"] != "notify": out.append({"ts": ev["ts"], "kind": ev["kind"], "payload": ev["payload"]})
        return {"events": out[-limit:], "location_policy": user["location_policy"], "contacts": self.public_profile(uid)["contacts"]}

    # ------------------------------------------------------------------ contacts (authenticated by a per-alert link)
    def _contact_ctx(self, token):
        parsed = M.parse_contact_token(self.secret, token or "")
        if not parsed: raise KazeError("invalid_link", 404)
        sid, cid = parsed; s = self.db.get_session(sid)
        if not s: raise KazeError("invalid_link", 404)
        user = self.db.get_user(s["user_id"]); c = next((c for c in (user or {}).get("contacts", []) if c["id"] == cid), None)
        if not c: raise KazeError("invalid_link", 404)
        if cid not in [n["contact_id"] for n in s["notified"]]: raise KazeError("not_notified", 403)     # a link for an alert you were never told about shows nothing
        return s, user, c

    def contact_view(self, token):
        s, user, c = self._contact_ctx(token); now = self.now()
        if s["status"] == M.RESOLVED: return {"state": "resolved", "person": user["name"], "reason": s["resolved_reason"], "location": None, "you_can_resolve": False}
        duress = s["duress"]
        return {"state": "acknowledged" if s["status"] == M.ACKNOWLEDGED else "alert", "person": user["name"], "reason": "silent_distress" if duress else "missed_checkin",
                "since_s": _ceil(now - s.get("escalated_at", now)), "device_last_seen_s": _ceil(now - s["last_seen"]), "device_offline_likely": now - s["last_seen"] > HEARTBEAT_STALE_S,
                "instructions": (["Do NOT call or text them: the signal was silent and contacting them could put them at risk.", "If you believe they are in danger, contact your local emergency service."] if duress
                                 else ["Try to reach them.", "If you cannot reach them and are worried, contact your local emergency service."]),
                "location": self._location_for(s, user, c), "you_can_resolve": c["permissions"]["can_resolve"],
                "acknowledged_by_you": c["id"] in s["responders"], "others_responding": [r for r in s["responders"] if r != c["id"]] != []}

    def contact_ack(self, token):
        s, user, c = self._contact_ctx(token)
        if s["status"] not in M.ALERTING: raise KazeError("not_active", 409)
        if c["id"] not in s["responders"]:
            s["responders"].append(c["id"])
            if s["status"] == M.ESCALATING:
                s["status"] = M.ACKNOWLEDGED; s["acked_by"] = c["id"]; s["acked_at"] = self.now(); s["contact_deadline"] = None
            self._save(s); self._event(s, user["id"], "acknowledged", "ca" if s["duress"] else "uca", contact=c["name"])
            self._info(s, user, f"{c['name']} is responding to {user['name']}'s alert.", exclude=c["id"])
        return self.contact_view(token)

    def contact_resolve(self, token, reason="false_alarm"):
        s, user, c = self._contact_ctx(token)
        if reason not in ("false_alarm", "safe", "help_arrived"): raise KazeError("bad_reason")
        if not c["permissions"]["can_resolve"]: raise KazeError("not_authorized", 403)
        if s["status"] not in M.ALERTING: raise KazeError("not_active", 409)
        self._resolve(s, user, by=f"contact:{c['name']}", reason=reason); return {"ok": True}

    # ------------------------------------------------------------------ admin / simulation
    def timeline(self, session_id): return self.db.events(session_id=session_id, limit=1000)
    def outbox(self, limit=100): return self.db.events(kind="notify", limit=limit)
    def sessions(self, uid): return self.db.user_sessions(uid)

    def contact_links(self, session_id):
        s = self.db.get_session(session_id)
        if not s: raise KazeError("no_session", 404)
        user = self._user(s["user_id"])
        return [{"contact_id": c["id"], "contact": c["name"], "notified": c["id"] in [n["contact_id"] for n in s["notified"]], "token": M.contact_link_token(self.secret, s["id"], c["id"])} for c in user["contacts"]]

    def sim_offline(self, uid, offline):
        u = self._user(uid); u["sim_offline"] = bool(offline); self._save_user(u); self._event(self._active(u), uid, "sim_device_offline" if offline else "sim_device_online", "a"); return offline

    def sim_unreachable(self, contact_id, unreachable):
        (self.notifier.unreachable.add if unreachable else self.notifier.unreachable.discard)(contact_id); return unreachable

    def health(self):
        now = self.now()
        return {"ok": self.db.ping() and (self.last_tick == 0 or now - self.last_tick < 10) and self.tick_errors == 0, "db": self.db.ping(), "last_tick_age_s": None if not self.last_tick else round(now - self.last_tick, 2),
                "tick_errors": self.tick_errors, "active_sessions": len(self.db.active_sessions()), "storage": type(self.db).__name__, "timescale": getattr(self.db, "timescale", "n/a")}


def _locked(fn):
    @functools.wraps(fn)
    def wrapper(self, *a, **kw):
        with self.lock: return fn(self, *a, **kw)
    return wrapper


# Every public operation holds the lock, so an API request on a worker thread and the ticker on the event loop can never interleave
# in the middle of a transition (for example, a PIN cancel racing the moment an alert is sent).
for _name in ("create_user", "update_consent", "erase_user", "start_watch", "stop_watch", "heartbeat", "public_state", "public_profile", "checkin", "post_location",
              "device_audit", "contact_view", "contact_ack", "contact_resolve", "tick", "timeline", "outbox", "contact_links", "sim_offline", "sim_unreachable", "health", "sessions", "_check_pin"):
    setattr(KazeService, _name, _locked(getattr(KazeService, _name)))
