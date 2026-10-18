"""HTTP + WebSocket API for KAZE. Three kinds of caller, three kinds of credential:
  admin   X-Admin-Token        sets people up, runs the simulations, sees everything (including duress internals)
  device  Authorization: Bearer <device token>       the person's own phone, wearable or headset
  contact the per-alert link token in the URL        a trusted contact, for one alert only"""
import asyncio, hmac, os
from pathlib import Path
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from .runtime import runtime, _data_dir
from .service import KazeError

STATIC = Path(__file__).parent / "static"
router = APIRouter(prefix="/api/kaze")


class ContactIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    channel: str = "sms"
    permissions: dict = {}


class UserIn(BaseModel):
    name: str; safe_pin: str; duress_pin: str; contacts: list[ContactIn]; preset: str = "realistic"; config: dict = {}; location_policy: str = "on_alert"


class UserPatch(BaseModel):
    name: str | None = None; safe_pin: str | None = None; duress_pin: str | None = None
    config: dict | None = None; location_policy: str | None = None; contacts: list[ContactIn] | None = None


class CheckinIn(BaseModel): method: str = "tap"; pin: str | None = None
class PinIn(BaseModel): pin: str
class LocationIn(BaseModel): lat: float; lon: float; acc: float | None = None
class ConsentIn(BaseModel): location_policy: str | None = None; contacts: list[dict] = []
class ResolveIn(BaseModel): reason: str = "false_alarm"
class OfflineIn(BaseModel): user_id: str; offline: bool = True
class UnreachableIn(BaseModel): contact_id: str; unreachable: bool = True
class WatchIn(BaseModel): config: dict = {}


def svc():
    if not runtime.service: raise HTTPException(503, "KAZE is not running")
    return runtime.service


def admin(x_admin_token: str | None = Header(default=None), authorization: str | None = Header(default=None)):
    tok = x_admin_token or (authorization or "").removeprefix("Bearer ").strip()
    if not tok or not runtime.admin_token or not hmac.compare_digest(tok, runtime.admin_token): raise HTTPException(401, "admin token required")


def device(authorization: str | None = Header(default=None)) -> str:
    uid = svc().authenticate_device((authorization or "").removeprefix("Bearer ").strip())
    if not uid: raise HTTPException(401, "invalid device token")
    if not runtime.limiter.allow(f"dev:{uid}", 240): raise HTTPException(429, "slow down")
    if svc().db.get_user(uid).get("sim_offline"): raise HTTPException(503, "simulated: this device has no connection")
    return uid


def pin_limit(uid):
    if not runtime.limiter.allow(f"pin:{uid}", 12): raise HTTPException(429, "too many PIN attempts, wait a minute")


def install_handlers(app: FastAPI):
    @app.exception_handler(KazeError)
    async def _kaze_error(request: Request, exc: KazeError): return JSONResponse({"ok": False, "error": exc.code, **exc.extra}, status_code=exc.status)


# ---------------------------------------------------------------- public
@router.get("/health")
def health():
    h = svc().health(); return {"ok": h["ok"], "last_tick_age_s": h["last_tick_age_s"]}      # nothing sensitive: this one is public


@router.post("/demo/pair")
def demo_pair():
    """Pair the single demo Quest without exposing the admin setup console."""
    if os.getenv("KAZE_DEMO_MODE", "0").lower() not in ("1", "true", "yes", "on"):
        raise HTTPException(404, "demo pairing is disabled")
    token_file = _data_dir() / "kaze_demo_device_token.txt"
    token = token_file.read_text().strip() if token_file.is_file() else ""
    if token and runtime.service.authenticate_device(token):
        return {"name": "Quest demo", "device_token": token}
    _, token = svc().create_user(
        "Quest demo", "1234", "0000",
        [{"name": "Default emergency contact", "channel": "push", "permissions": {"location": "none", "can_resolve": True}}],
        location_policy="never", preset="realistic")
    token_file.parent.mkdir(parents=True, exist_ok=True); token_file.write_text(token); os.chmod(token_file, 0o600)
    return {"name": "Quest demo", "device_token": token}


# ---------------------------------------------------------------- admin
@router.get("/admin/health", dependencies=[Depends(admin)])
def admin_health(): return svc().health()


@router.post("/admin/users", dependencies=[Depends(admin)])
def create_user(u: UserIn):
    uid, token = svc().create_user(u.name, u.safe_pin, u.duress_pin, [c.model_dump() for c in u.contacts], config=u.config, location_policy=u.location_policy, preset=u.preset)
    return {"user_id": uid, "device_token": token, "contacts": [{"id": c["id"], "name": c["name"]} for c in svc().public_profile(uid)["contacts"]]}


@router.get("/admin/users/{uid}", dependencies=[Depends(admin)])
def get_user(uid: str): return svc().public_profile(uid, admin=True)


@router.get("/admin/users", dependencies=[Depends(admin)])
def list_users(): return svc().list_users()


@router.patch("/admin/users/{uid}", dependencies=[Depends(admin)])
def patch_user(uid: str, p: UserPatch):
    return svc().update_user(uid, name=p.name, config=p.config, location_policy=p.location_policy, safe_pin=p.safe_pin,
                             duress_pin=p.duress_pin, contacts=[c.model_dump() for c in p.contacts] if p.contacts is not None else None)


@router.post("/admin/users/{uid}/token", dependencies=[Depends(admin)])
def rotate_token(uid: str): return {"device_token": svc().rotate_device_token(uid)}


@router.delete("/admin/users/{uid}", dependencies=[Depends(admin)])
def admin_erase(uid: str): return svc().erase_user(uid)


@router.post("/admin/users/{uid}/watch", dependencies=[Depends(admin)])
def admin_watch(uid: str, w: WatchIn = WatchIn()): s = svc().start_watch(uid, w.config); return {"session_id": s["id"], "status": s["status"]}


@router.get("/admin/sessions/{uid}", dependencies=[Depends(admin)])
def sessions(uid: str): return [{k: s[k] for k in ("id", "status", "duress", "origin", "started_at", "resolved_reason")} for s in svc().sessions(uid)]


@router.get("/admin/state/{uid}", dependencies=[Depends(admin)])
def admin_state(uid: str): return svc().admin_state(uid)


@router.get("/admin/timeline/{sid}", dependencies=[Depends(admin)])
def timeline(sid: str): return svc().timeline(sid)


@router.get("/admin/outbox", dependencies=[Depends(admin)])
def outbox(limit: int = 100): return svc().outbox(min(limit, 500))


@router.get("/admin/links/{sid}", dependencies=[Depends(admin)])
def links(sid: str):
    return [{**l, "url": f"{svc().base_url}/kaze/c/{l['token']}"} for l in svc().contact_links(sid)]


@router.post("/admin/sim/offline", dependencies=[Depends(admin)])
def sim_offline(b: OfflineIn): return {"offline": svc().sim_offline(b.user_id, b.offline)}


@router.post("/admin/sim/unreachable", dependencies=[Depends(admin)])
def sim_unreachable(b: UnreachableIn): return {"unreachable": svc().sim_unreachable(b.contact_id, b.unreachable)}


@router.get("/admin/scenarios", dependencies=[Depends(admin)])
def list_scenarios():
    from .scenarios import SCENARIOS
    return [{"name": n, "title": s["title"], "shows": s["shows"]} for n, s in SCENARIOS.items()]


@router.post("/admin/scenarios/{name}/run", dependencies=[Depends(admin)])
async def run_scenario(name: str, mode: str = "fast"):
    from .scenarios import SCENARIOS, run
    if name not in SCENARIOS: raise HTTPException(404, "unknown scenario")
    if mode not in ("fast", "live"): raise HTTPException(400, "mode must be fast or live")
    return await run(name, mode, svc() if mode == "live" else None)


@router.get("/admin/report", dependencies=[Depends(admin)])
async def report():
    from .scenarios import build_report
    return await build_report()


@router.post("/admin/sweep", dependencies=[Depends(admin)])
async def sweep(n: int = Query(100, ge=10, le=1000), p_ack: float = 0.7, p_unreachable: float = 0.05, p_forget: float = 0.1):
    from .sim import sweep as run_sweep
    return await asyncio.to_thread(run_sweep, n, p_ack=p_ack, p_unreachable=p_unreachable, p_forget=p_forget)


# ---------------------------------------------------------------- the person's device
@router.get("/device/state")
def d_state(uid: str = Depends(device)): return svc().public_state(uid)


@router.post("/device/watch/start")
def d_start(w: WatchIn = WatchIn(), uid: str = Depends(device)): svc().start_watch(uid, w.config); return {"ok": True, **svc().public_state(uid)}


@router.post("/device/watch/stop")
def d_stop(b: PinIn, uid: str = Depends(device)): pin_limit(uid); return svc().stop_watch(uid, b.pin)


@router.post("/device/heartbeat")
def d_heartbeat(uid: str = Depends(device)): return svc().heartbeat(uid)


@router.post("/device/checkin")
def d_checkin(b: CheckinIn, uid: str = Depends(device)):
    if b.method == "pin": pin_limit(uid)
    return svc().checkin(uid, b.method, b.pin)


@router.post("/device/cancel")
def d_cancel(b: PinIn, uid: str = Depends(device)): pin_limit(uid); return svc().checkin(uid, "pin", b.pin)


@router.post("/device/location")
def d_location(b: LocationIn, uid: str = Depends(device)): return svc().post_location(uid, b.lat, b.lon, b.acc)


@router.get("/device/audit")
def d_audit(uid: str = Depends(device)): return svc().device_audit(uid)


@router.get("/device/profile")
def d_profile(uid: str = Depends(device)): return svc().public_profile(uid)


@router.put("/device/consent")
def d_consent(b: ConsentIn, uid: str = Depends(device)): return svc().update_consent(uid, b.location_policy, b.contacts)


@router.post("/device/erase")
def d_erase(b: PinIn, uid: str = Depends(device)):
    pin_limit(uid); u = svc().db.get_user(uid)
    kind, extra = svc()._check_pin(u, b.pin)
    if kind == "locked": raise KazeError("locked", 429, **extra)
    if kind != "safe": raise KazeError("wrong_pin", 401, **extra)      # a duress PIN does not erase anything
    return svc().erase_user(uid)


# ---------------------------------------------------------------- contacts
def contact_limit(token):
    if not runtime.limiter.allow(f"c:{token[:20]}", 90): raise HTTPException(429, "slow down")


@router.get("/contact/{token}")
def c_view(token: str): contact_limit(token); return svc().contact_view(token)


@router.post("/contact/{token}/ack")
def c_ack(token: str): contact_limit(token); return svc().contact_ack(token)


@router.post("/contact/{token}/resolve")
def c_resolve(token: str, b: ResolveIn = ResolveIn()): contact_limit(token); return svc().contact_resolve(token, b.reason)


# ---------------------------------------------------------------- live streams
@router.websocket("/ws/admin")
async def ws_admin(ws: WebSocket, token: str = Query("")):
    if not runtime.admin_token or not hmac.compare_digest(token, runtime.admin_token): await ws.close(code=4401); return
    await ws.accept(); runtime.admin_ws.add(ws)
    try:
        while True: await ws.receive_text()
    except WebSocketDisconnect: pass
    finally: runtime.admin_ws.discard(ws)


@router.websocket("/ws/device")
async def ws_device(ws: WebSocket, token: str = Query("")):
    uid = runtime.service.authenticate_device(token) if runtime.service else None
    if not uid: await ws.close(code=4401); return
    await ws.accept(); runtime.device_ws.setdefault(uid, set()).add(ws)
    try:
        while True: await ws.receive_text()
    except WebSocketDisconnect: pass
    finally: runtime.device_ws.get(uid, set()).discard(ws)


# ---------------------------------------------------------------- pages
pages = APIRouter()


@pages.get("/kaze")
def console(): return FileResponse(STATIC / "console.html")


@pages.get("/kaze/setup")
def setup_page(): return FileResponse(STATIC / "setup.html")


@pages.get("/kaze/device")
def device_page(): return FileResponse(STATIC / "device.html", headers={"Cache-Control": "no-store"})


@pages.get("/kaze/c/{token}")
def contact_page(token: str): return FileResponse(STATIC / "contact.html")
