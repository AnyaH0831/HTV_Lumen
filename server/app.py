"""FastAPI app: serves the Quest web app, a websocket hub, recording upload, and hosts the MCP server + agent."""
import asyncio, json, os, re, time
from contextlib import AsyncExitStack, asynccontextmanager
import yaml
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from mcp.shared.memory import create_connected_server_and_client_session as connect
from .state import rt, Hub, ROOT, DATA, env_flag
from .groups import Progress, clip_counts, load_groups, next_session
from .mcp_server import mcp
from .orchestrator import run_pipeline, normalize_cached
from . import captions, speech
from .kaze.app import mount as mount_kaze, kaze_lifespan

VOCAB = yaml.safe_load(open(ROOT / "vocab" / "vocab.yaml"))["signs"]
GLOSSES = [s["gloss"] for s in VOCAB]
GROUPS = load_groups()
GROUP_IDS = {g["id"] for g in GROUPS}
progress = Progress(DATA / "quest" / "progress.json")


@asynccontextmanager
async def lifespan(app):
    rt.hub = Hub()
    async with AsyncExitStack() as stack:
        rt.mcp_session = await stack.enter_async_context(connect(mcp))
        if env_flag("KAZE_ENABLED", True):                                         # Project KAZE (safety timers). A failure here must never take Lumen down.
            try: await stack.enter_async_context(kaze_lifespan())
            except Exception as e: print(f"[kaze] not started: {type(e).__name__}: {e}")
        task = asyncio.create_task(captions.run(rt)) if env_flag("ENABLE_CAPTIONS") else None
        yield
        if task: task.cancel()


app = FastAPI(lifespan=lifespan)
if env_flag("KAZE_ENABLED", True): mount_kaze(app)                             # routes must be added before the static mounts at the bottom


@app.middleware("http")
async def _fresh_code(request: Request, call_next):
    """A headset that caches the page or its modules can keep running stale code across restarts (a dead Start
    button that no server fix can reach). HTML/JS is cheap here, so always revalidate it."""
    resp = await call_next(request)
    if resp.headers.get("content-type", "").startswith(("text/html", "text/javascript", "application/javascript")):
        resp.headers["Cache-Control"] = "no-store"
    return resp


GUIDE = yaml.safe_load(open(ROOT / "vocab" / "sign_guide.yaml"))["signs"]
SIGN_IMG_DIR = ROOT / "quest-app" / "signs"


def _guide():
    """Per-sign instructions plus the URL of a reference photo if one exists on disk (looked up per request, so adding photos needs no restart)."""
    out = {}
    for g in GLOSSES:
        e = dict(GUIDE.get(g) or {}); e["verified"] = e.get("verified") is True
        for ext in ("jpg", "jpeg", "png", "webp"):
            if (SIGN_IMG_DIR / f"{g}.{ext}").is_file(): e["image"] = f"/signs/{g}.{ext}"; break
        out[g] = e
    return out


@app.get("/api/vocab")
def vocab(): return {"glosses": GLOSSES, "phrases": {s["gloss"]: s["phrase"] for s in VOCAB}, "guide": _guide()}


def _clean_signer(raw):
    return re.sub(r"[^A-Za-z0-9_-]", "", str(raw or ""))[:32] or "anon"


@app.get("/api/groups")
def word_groups(signer: str = "anon"):
    """Word-group cards for the recording page: the words, sessions done, per-word clip counts."""
    s = _clean_signer(signer)
    done = progress.sessions(s)
    clips = clip_counts(s)
    groups = []
    for g in GROUPS:
        sessions = done.get(g["id"], 0)
        groups.append({**g, "sessions": sessions, "done": sessions >= g["needed"],
                       "clips": {gl: clips.get(gl, 0) for gl in g["glosses"]}})
    return {"groups": groups, "nextSession": next_session(s)}


@app.get("/api/health")
def health(): return {"ok": True, "clients": len(rt.hub.clients), "phrase_id": rt.phrase_id}


def _valid_hand(h): return h is None or (isinstance(h, list) and len(h) == 63 and all(isinstance(v, (int, float)) for v in h))


@app.post("/api/recordings")
async def recordings(req: Request):
    r = await req.json()
    label, t = r.get("label"), r.get("t", [])
    if label not in GLOSSES + ["__background__"]: raise HTTPException(422, "unknown label")
    if not (6 <= len(t) <= 400) or any(b <= a for a, b in zip(t, t[1:])): raise HTTPException(422, "bad timestamps")
    if len(r.get("left", [])) != len(t) or len(r.get("right", [])) != len(t): raise HTTPException(422, "length mismatch")
    if not all(_valid_hand(h) for h in r["left"] + r["right"]): raise HTTPException(422, "bad hand data")
    signer = _clean_signer(r.get("signer"))
    group = r.get("group")
    if group is not None and group not in GROUP_IDS: raise HTTPException(422, "unknown group")
    r["signer"] = signer
    (DATA / "quest").mkdir(parents=True, exist_ok=True)
    with open(DATA / "quest" / f"{signer}.jsonl", "a") as f: f.write(json.dumps(r) + "\n")
    return {"ok": True}


def _clean_token(m):
    """Validate one recognised sign from the headset or the dashboard simulator. Returns None for anything malformed."""
    if not isinstance(m, dict) or m.get("gloss") not in GLOSSES: return None
    def prob(v):
        try: return min(max(float(v), 0.0), 1.0)
        except (TypeError, ValueError): return None
    conf = prob(m.get("conf", 1.0))
    if conf is None: return None
    alts = [{"gloss": a["gloss"], "conf": prob(a.get("conf", 0)) or 0.0} for a in (m.get("alts") or [])[:3]
            if isinstance(a, dict) and a.get("gloss") in GLOSSES and a["gloss"] != m["gloss"]]
    return {"gloss": m["gloss"], "conf": conf, "alts": alts, **({"tentative": True} if m.get("decision") == "confirm" else {})}


def _quiet_s(): return float(os.getenv("LUMEN_QUIET_S", "1.5"))          # a pause this long ends the sentence
def _extend_s(): return float(os.getenv("LUMEN_EXTEND_S", "1.5"))         # one extra wait if the interpreter thinks the signer is not finished
def _dedupe_s(): return float(os.getenv("LUMEN_DEDUPE_S", "2.5"))         # the same sign again within this window counts as one
def _max_wait_s(): return float(os.getenv("LUMEN_MAX_WAIT_S", "10"))      # a sentence is handed over after this long even if signing never pauses
def _max_queue(): return int(os.getenv("LUMEN_MAX_QUEUE", "8"))           # ... or once this many signs are waiting


def _add_token(tok):
    """Append a recognised sign, or merge it into the previous one if it is the same sign again within the dedupe window
    (a segmenter bounce or a repeated sign is one word, not two). Only signs the pipeline has not taken yet can be merged."""
    now = time.time(); start = rt.busy_count if rt.busy else 0
    if len(rt.tokens) <= start: rt.first_t = now                             # the queue was empty: the max-wait clock starts here
    last = rt.tokens[-1] if len(rt.tokens) > start else None
    if last and last["gloss"] == tok["gloss"] and now - last.get("t", 0) < _dedupe_s():
        last["conf"] = max(last["conf"], tok["conf"]); last["t"] = now; last["count"] = last.get("count", 1) + 1; return False
    rt.tokens.append({**tok, "t": now, "count": 1}); return True


async def _quiet_loop():
    """After the signer pauses, hand everything recognised so far to the pipeline. No Send button needed."""
    while True:
        waited = time.time() - rt.first_t if rt.first_t else 0.0
        rt.quiet_phase = "sleep"; await asyncio.sleep(max(0.05, min(_quiet_s(), _max_wait_s() - waited)))   # a new sign restarts this, but never past the max wait
        if not rt.tokens: return
        rt.quiet_phase = "run"
        while rt.busy: await asyncio.sleep(0.2)
        toks = list(rt.tokens)
        if os.getenv("LUMEN_WAIT_FOR_COMPLETE", "1") != "0":
            try: complete = (await normalize_cached(toks)).get("complete", True)
            except Exception: complete = True                                # the pipeline will fall back to rules and report it
            if len(rt.tokens) != len(toks): continue                         # more signs arrived while we checked: wait again
            if not complete:
                await rt.emit("stage", stage="waiting"); rt.quiet_phase = "sleep"
                await asyncio.sleep(_extend_s())                             # new signs cancel this task and restart the loop
                rt.quiet_phase = "run"; toks = list(rt.tokens)
        await _run(toks)
        if not rt.tokens: return                                             # signs made during the run get their own turn


def _arm_quiet_timer():
    t = rt.quiet_task
    if t and not t.done():
        if rt.quiet_phase != "sleep": return                                 # mid-run: the loop comes back for new signs itself
        t.cancel()
    rt.quiet_task = asyncio.create_task(_quiet_loop())


def _stop_quiet_timer():
    if rt.quiet_task and not rt.quiet_task.done() and rt.quiet_phase == "sleep": rt.quiet_task.cancel()


@app.websocket("/ws")
async def ws(sock: WebSocket):
    role = sock.query_params.get("role", "quest"); await sock.accept(); rt.hub.clients[sock] = role
    await sock.send_json({"type": "tokens", "tokens": rt.tokens})
    try:
        while True:
            try: m = json.loads(await sock.receive_text())
            except ValueError: continue                                      # garbage must not kill the connection
            t = m.get("type") if isinstance(m, dict) else None
            if t == "token":
                tok = _clean_token(m)
                if tok:
                    _add_token(tok); rt.store.log("token", tok)
                    await rt.hub.broadcast({"type": "tokens", "tokens": rt.tokens})
                    if m.get("assist") == "auto":
                        waiting = len(rt.tokens) - (rt.busy_count if rt.busy else 0)
                        if waiting >= _max_queue() and not rt.busy: _stop_quiet_timer(); asyncio.create_task(_run(list(rt.tokens)))   # queue full: hand it over now
                        else: _arm_quiet_timer()
            elif t == "undo" and len(rt.tokens) > (rt.busy_count if rt.busy else 0):
                rt.tokens.pop(); await rt.hub.broadcast({"type": "tokens", "tokens": rt.tokens})
            elif t == "clear":
                rt.tokens = rt.tokens[:rt.busy_count] if rt.busy else []; _stop_quiet_timer()
                await rt.hub.broadcast({"type": "tokens", "tokens": rt.tokens})
            elif t == "send" and not rt.busy:
                _stop_quiet_timer()
                toks = [x for x in (_clean_token(y) for y in (m.get("tokens") or rt.tokens)) if x]
                if toks: asyncio.create_task(_run(toks))
            elif t == "cancel_speech": speech.cancel()
            elif t == "speak_now": speech.speak_now()
            elif t == "doctor_says": await rt.doctor_said(m.get("text", ""))   # dashboard chat / voice-to-text
            elif t == "doctor_audio":                                          # browser mic (works where the server has no mic)
                try: await captions.feed_b64(rt, m.get("pcm"))
                except Exception as e: print(f"[captions] {type(e).__name__}: {e}")
            elif t == "session_done": await _session_done(m)
            elif t == "confirm":
                fut = rt.pending.get(m.get("id"))
                if fut and not fut.done(): fut.set_result(bool(m.get("ok")))
    except WebSocketDisconnect: pass
    finally: rt.hub.clients.pop(sock, None)


async def _session_done(m):
    """The headset finished a full pass through a word group: count the session."""
    gid = m.get("group")
    if gid not in GROUP_IDS: return
    signer = _clean_signer(m.get("signer"))
    g = next(x for x in GROUPS if x["id"] == gid)
    n = progress.record(signer, gid, {"session": str(m.get("session") or "")[:32],
                                    "reps": m.get("reps"), "saved": m.get("saved"),
                                    "expected": m.get("expected")})
    rt.store.log("session", {"signer": signer, "group": gid, "sessions": n})
    await rt.emit("notice", text=f"Recording: {g['name']} done for {signer} ({n} session{'s' if n > 1 else ''})")


async def _run(toks):
    rt.busy = True; rt.busy_count = len(toks); await rt.emit("agent_start", count=len(toks))
    try: await run_pipeline(toks)
    finally:
        rt.busy = False; rt.tokens = rt.tokens[rt.busy_count:]; rt.busy_count = 0     # keep signs that arrived while it was working
        rt.first_t = time.time() if rt.tokens else 0.0
        await rt.hub.broadcast({"type": "tokens", "tokens": rt.tokens})


@app.get("/dashboard")
def dashboard(): return FileResponse(ROOT / "server" / "static" / "dashboard.html")


app.mount("/static", StaticFiles(directory=ROOT / "server" / "static"), name="static")
app.mount("/", StaticFiles(directory=ROOT / "quest-app", html=True), name="quest")
