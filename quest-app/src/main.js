import * as THREE from "three";
import { WEBXR_JOINTS, WEBXR_TO_MP21, extractFeatures } from "./features.js";
import { Segmenter } from "./segmenter.js";
import { Classifier } from "./classifier.js";
import { Net } from "./net.js";
import { toRecord, buildPrompts, buildBackgroundPrompts, pickGroup } from "./recorder.js";
import { HandSkeleton } from "./skeleton.js";
import { STEPS, REST_MS, INTRO, currentStep, cardFor, cue } from "./coach.js";
import { Panel, Button, CircleButton, Buttons, C, wrap } from "./ui.js";
import { route, mergeToken, DEDUPE_MS, TENT_MIN, BG_MAX } from "./assist.js";
import { Chat, chipFor, drawCard, CARD, lazyAnchor, yawFromQuat, cardPose, handFade } from "./chat.js";
import { KazeClient, kazeSettings } from "./kaze.js";

const q = new URLSearchParams(location.search);
const MODE = q.get("mode") === "record" ? "record" : "live";
const CARD_UI = MODE === "live" && q.get("ui") !== "classic";            // one translucent conversation card (default). ?ui=classic = the three panels
const SIDE = ["left", "right"].includes(q.get("card")) ? q.get("card") : "center";   // where the card sits: ?card=left|right
const ASSIST = CARD_UI || q.get("assist") === "auto" ? "auto" : "manual";  // auto: every sign goes to the agent, it speaks after a pause. manual: the old Yes/Send flow
const SIGNER = q.get("signer") || "anon", SESSION = q.get("session") || "s1", REPS = +(q.get("reps") || 5);
const GROUP = q.get("group") || null;                       // word group to record (default: first unfinished)
const BACKGROUND_ONLY = q.get("background") === "1";
const $ = id => document.getElementById(id), log = m => { console.log(m); $("log").textContent = m; };
// Any uncaught error would otherwise leave the Start button at its disabled HTML default, with no clue why.
addEventListener("error", e => { const b = $("log"); if (b && e.message) b.textContent = "Error: " + e.message; });
addEventListener("unhandledrejection", e => { const b = $("log"); const m = e.reason && e.reason.message || e.reason; if (b && m) b.textContent = "Error: " + m; });

const S = { tokens: [], pending: null, last: null, status: "Raise both hands and sign", busy: false, agent: [], confirm: null,
            caption: "", conn: "connecting", hands: false, recording: false, prompts: [], idx: 0, pendingRec: null, saved: 0, phrases: {}, guide: {}, intro: MODE === "record", restUntil: 0, speech: null, busyCount: 0, busySince: 0, lastClip: null, lastTokenAt: 0,
            group: null, reported: false, cueFor: -1, cueStart: 0, cueTick: null };   // cue*: the get-ready countdown before each record clip
const kaze = new KazeClient(); let kazeLastState = "";
// ?strict=1: an experimental stricter "is this a sign?" gate (hands higher, held longer). Compare false words per idle minute before trusting it.
const STRICT = { startHold: 0.2, minDur: 0.5, raiseBelowHead: 0.45 };
const seg = new Segmenter(q.get("strict") === "1" ? STRICT : {}), net = new Net("quest"), clf = new Classifier(), buttons = new Buttons();
const GATE = { tentMin: +(q.get("tent") || TENT_MIN), bgMax: +(q.get("bg") || BG_MAX) }, DEDUPE = +(q.get("dedupe") || DEDUPE_MS / 1000) * 1000;
const chat = new Chat(), follow = { pos: [0, 1.6, 0], yaw: 0, active: false }; let cardOpacity = 1, lastFrameT = 0, lastTick = null, cardP = null, btnP = null;
let renderer, scene, camera, placed = false, headPos = new THREE.Vector3();
const panels = {}, tipDots = []; let lastDbg = 0, lastHands = null;
const SHOW_SKEL = q.get("skeleton") !== null;              // ?skeleton=1: draw the full 21-joint skeleton
const skels = [new HandSkeleton(C.warn), new HandSkeleton(C.ok)];   // left amber, right teal
function drawDebug(d, session) {                       // ?debug=1: the headset itself reports why hands/dots are missing
  const feat = session.enabledFeatures ? (session.enabledFeatures.includes("hand-tracking") ? "granted" : "NOT granted") : "unknown";
  console.log(`inputs ${d.src} hands ${d.hand} full ${d.full} dots ${d.dots} hand-tracking ${feat}`);
  panels.debug.draw((ctx) => { ctx.fillStyle = C.paper; ctx.font = "500 28px system-ui"; ctx.textBaseline = "top";
    ctx.fillText(`inputs ${d.src}   hands ${d.hand}   full ${d.full}   dots ${d.dots}`, 24, 22); ctx.fillText(`hand-tracking: ${feat}`, 24, 72); });
}

/* ---------- three / XR setup ---------- */
function initThree() {
  renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true }); renderer.setPixelRatio(devicePixelRatio);
  renderer.setSize(innerWidth, innerHeight); renderer.xr.enabled = true; renderer.setClearColor(0x000000, 0);
  scene = new THREE.Scene(); camera = new THREE.PerspectiveCamera(70, innerWidth / innerHeight, 0.01, 20);
  document.body.appendChild(renderer.domElement); renderer.domElement.style.display = "none";
  for (let i = 0; i < 2; i++) { const d = new THREE.Mesh(new THREE.SphereGeometry(0.012), new THREE.MeshBasicMaterial({ color: C.ok })); d.visible = false; scene.add(d); tipDots.push(d); }
  if (SHOW_SKEL) for (const s of skels) scene.add(s.group);
  if (CARD_UI) panels.card = new Panel(CARD.w, CARD.h, CARD.ppm, CARD.alpha);       // translucent: the room stays visible behind it
  else { panels.status = new Panel(0.5, 0.36); panels.agent = new Panel(0.46, 0.36); panels.caption = new Panel(0.7, 0.14); }
  if (q.get("debug")) panels.debug = new Panel(0.5, 0.09);
  if (MODE === "live") { panels.kazeButton = buttons.add(new CircleButton("I'M SAFE", () => kaze.checkin(), C.ok)); panels.kazeButton.mesh.visible = false; }
  for (const p of Object.values(panels)) scene.add(p.mesh);
  const B = (l, f, c) => { const b = buttons.add(new Button(l, f, c)); scene.add(b.mesh); return b; };
  if (CARD_UI) {                                       // controls appear under the card only when they apply
    panels.cancel = B("Cancel", cancelAll, C.bad); panels.now = B("Speak now", speakNow, C.ok);
    panels.no = B("No", () => answer(false), C.bad); panels.yes = B("Yes", () => answer(true), C.ok);
    panels.notsign = B("Not sign", notASign, C.dim); panels.safe = B("I'm safe", () => kaze.checkin(), C.ok);
    for (const b of [panels.cancel, panels.now, panels.yes, panels.no, panels.notsign, panels.safe]) { b.set(false); b.mesh.visible = false; }
  } else if (MODE === "live") {
    panels.confirm = B("Yes", confirmPending, C.ok); panels.redo = B("Redo", () => { S.pending = null; S.status = "Sign again"; draw(); }, C.warn);
    panels.undo = B("Undo", () => { S.tokens.pop(); net.send({ type: "undo" }); draw(); }, C.dim);
    panels.send = B("Send", sendPhrase, C.ok); panels.clear = B("Clear", () => { S.tokens = []; S.pending = null; net.send({ type: "clear" }); draw(); }, C.bad);
    panels.yes = B("Approve", () => answer(true), C.ok); panels.no = B("Reject", () => answer(false), C.bad);
  } else {
    panels.discard = B("Discard", () => { S.pendingRec = null; S.status = "Discarded. Sign again."; draw(); }, C.bad);
    panels.skip = B("Skip", () => { S.idx++; draw(); }, C.warn);
    panels.start = B("Start", () => { S.intro = false; S.status = "Ready. Sign when you like."; armCue(performance.now()); draw(); }, C.ok);
  }
  addEventListener("resize", () => { renderer.setSize(innerWidth, innerHeight); });
}

function positionCard(anchor, yaw, eye) {                 // card + its button row, from the ANCHOR (not from the head, so it holds still for you to poke)
  const pose = cardPose(anchor, yaw, SIDE), P = new THREE.Vector3(...pose.pos), Bp = new THREE.Vector3(...pose.buttons), R = new THREE.Vector3(...pose.right);
  cardP = P; btnP = Bp; panels.card.place(P, eye);
  const at = d => Bp.clone().addScaledVector(R, d);
  panels.cancel.place(at(-0.08), eye); panels.now.place(at(0.08), eye); panels.no.place(at(-0.08), eye); panels.yes.place(at(0.08), eye); panels.notsign.place(at(-0.24), eye);
  panels.safe.place(Bp, eye);
  const K = P.clone().addScaledVector(R, (SIDE === "right" ? -1 : 1) * (CARD.w / 2 + 0.16));
  panels.kazeButton.place(K, eye);
  if (panels.debug) panels.debug.place(P.clone().add(new THREE.Vector3(0, CARD.h / 2 + 0.06, 0)), eye);
}
function updateCard(time, hp, hq, left, right, tips) {   // every frame: hold still, re-centre only after a real change, fade while a hand is in front
  const dt = Math.min(0.1, Math.max(0.001, (time - (lastFrameT || time)) / 1000)); lastFrameT = time;
  const eye = [hp.x, hp.y, hp.z], poking = tips.some(t => (cardP && t.distanceTo(cardP) < 0.32) || (btnP && t.distanceTo(btnP) < 0.16)
    || (panels.kazeButton?.mesh.visible && t.distanceTo(panels.kazeButton.mesh.position) < 0.12));
  lazyAnchor(follow, eye, yawFromQuat(hq), dt, poking); positionCard(follow.pos, follow.yaw, new THREE.Vector3(hp.x, hp.y, hp.z));
  const pts = []; for (const h of [left, right]) if (h) for (const j of [0, 5, 8, 12, 16, 20]) pts.push([h[j * 3], h[j * 3 + 1], h[j * 3 + 2]]);
  cardOpacity += ((handFade(eye, cardPose(follow.pos, follow.yaw, SIDE), pts) ? 0.2 : 1) - cardOpacity) * Math.min(1, dt * 6);
  panels.card.mesh.material.opacity = cardOpacity;
}
function drawCardPanel() {
  const now = performance.now(), asking = !!S.confirm, counting = !!S.speech;
  const st = { words: S.tokens, pending: S.speech, ask: S.confirm, chip: chipFor({ confirm: S.confirm, speech: S.speech, busy: S.busy, hands: S.hands, pendingWords: S.tokens.length }, now) };
  panels.card.draw((ctx, W, H) => drawCard(ctx, W, H, chat, st, C));
  const waiting = S.tokens.length > S.busyCount && !S.busy, showNow = (counting || waiting) && !asking, showNot = !!S.lastClip && waiting && !asking && now - S.lastTokenAt < 6000;
  for (const b of [panels.yes, panels.no]) { b.set(asking); b.mesh.visible = asking; }
  for (const b of [panels.cancel, panels.now]) { b.set(showNow); b.mesh.visible = showNow; }
  panels.notsign.set(showNot); panels.notsign.mesh.visible = showNot;
  panels.card.mesh.visible = true;
  drawKazeHud();
}
let kazeRing = { state: "", total: 1 };
function kazeWatching() {
  const st = kaze.state?.state;
  return st === "watching" || st === "checkin_due" || st === "alert_pending" || st === "alert_sent";
}
function drawKazeHud() {
  if (!panels.kazeButton) return;
  const st = kaze.state?.state, on = kazeWatching(), canTap = st === "watching" || st === "checkin_due";
  panels.kazeButton.enabled = on && canTap; panels.kazeButton.mesh.visible = on;
  if (panels.safe) { panels.safe.set(false); panels.safe.mesh.visible = false; }
  if (!on) return;
  const remain = Math.max(0, Number(kaze.state.respond_within ?? kaze.state.alert_in ?? kaze.state.next_checkin_in ?? 0));
  if (st !== kazeRing.state || remain > kazeRing.total) kazeRing = { state: st, total: Math.max(remain, 1) };
  const progress = Math.min(1, remain / kazeRing.total), secs = Math.max(0, Math.ceil(remain));
  const color = st === "alert_pending" || st === "alert_sent" ? C.bad : st === "checkin_due" ? C.warn : C.ok;
  panels.kazeButton.color = color;
  panels.kazeButton.draw((ctx, W, H) => {
    const cx = W / 2, cy = H / 2, m = Math.min(W, H), ringR = m * 0.46, fillR = m * 0.32;
    ctx.lineWidth = m * 0.055; ctx.strokeStyle = "rgba(255,255,255,0.22)"; ctx.beginPath(); ctx.arc(cx, cy, ringR, 0, Math.PI * 2); ctx.stroke();
    ctx.strokeStyle = color; ctx.lineCap = "round"; ctx.beginPath(); ctx.arc(cx, cy, ringR, -Math.PI / 2, -Math.PI / 2 + Math.PI * 2 * progress); ctx.stroke();
    ctx.fillStyle = color; ctx.beginPath(); ctx.arc(cx, cy, fillR, 0, Math.PI * 2); ctx.fill();
    ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.fillStyle = C.ink; ctx.font = `800 ${m * 0.11}px system-ui`;
    const label = st === "alert_sent" ? "HELP" : "I'M SAFE";
    label.split(/\s+/).forEach((line, i, a) => ctx.fillText(line, cx, cy + (i - (a.length - 1) / 2) * m * 0.12));
    ctx.fillStyle = color; ctx.font = `800 ${m * 0.08}px system-ui`;
    if (st !== "alert_sent") ctx.fillText(String(secs), cx, cy - (fillR + ringR) / 2);
    ctx.textAlign = "left";
  }, "rgba(0,0,0,0)");
}
function placeUI(head, quat) {                            // once, in front of the wearer, yaw only
  if (CARD_UI) { follow.yaw = yawFromQuat(quat); follow.pos = [head.x, head.y, head.z]; positionCard(follow.pos, follow.yaw, head); placed = true; headPos.copy(head); return; }
  const fwd = new THREE.Vector3(0, 0, -1).applyQuaternion(quat); fwd.y = 0; fwd.normalize();
  const right = new THREE.Vector3(-fwd.z, 0, fwd.x), up = new THREE.Vector3(0, 1, 0);
  const A = head.clone().addScaledVector(fwd, 0.55).addScaledVector(up, -0.05);
  const at = (r, u) => A.clone().addScaledVector(right, r).addScaledVector(up, u);
  panels.status.place(at(-0.56, 0.02), head); panels.agent.place(at(0.56, 0.02), head); panels.caption.place(at(0, 0.42), head);
  if (panels.debug) panels.debug.place(at(0, -0.1), head);
  const row = (names, cx, dx) => names.forEach((n, i) => panels[n] && panels[n].place(at(cx + (i - (names.length - 1) / 2) * dx, -0.24), head));
  if (MODE === "live") { row(["confirm", "redo", "undo"], -0.56, 0.15); row(["send", "clear"], -0.56, 0.15); panels.send.mesh.position.y -= 0.09; panels.clear.mesh.position.y -= 0.09;
                         row(["yes", "no"], 0.56, 0.17);
                         if (panels.kazeButton) panels.kazeButton.place(at(0.56, -0.24), head); }
  else { row(["discard", "skip"], -0.56, 0.17); panels.start.place(at(-0.56, -0.24), head); }
  placed = true; headPos.copy(head);
}

/* ---------- drawing ---------- */
function bar(ctx, x, y, w, h, frac, color) { ctx.fillStyle = C.line; ctx.beginPath(); ctx.roundRect(x, y, w, h, h / 2); ctx.fill(); ctx.fillStyle = color; ctx.beginPath(); ctx.roundRect(x, y, Math.max(h, w * frac), h, h / 2); ctx.fill(); }
function draw() {
  if (CARD_UI) { drawCardPanel(); return; }
  const lat = clf.latency?.();
  panels.status.draw((ctx, W, H) => {
    ctx.textBaseline = "top"; ctx.fillStyle = C.dim; ctx.font = "500 26px system-ui";
    ctx.fillText(MODE === "record" ? `Recording as ${SIGNER}, session ${SESSION}${S.group ? ` — ${S.group.name}` : ""}` : `Live${lat ? `, model ${lat.p50.toFixed(0)} ms` : ""}, link ${S.conn}`, 32, 28);
    if (MODE === "record") {
      ctx.textAlign = "right"; ctx.fillStyle = S.hands ? C.ok : C.bad; ctx.font = "600 26px system-ui";
      ctx.fillText(S.hands ? "Hands seen" : "No hands seen", W - 32, 28); ctx.textAlign = "left";
      if (S.intro) { drawIntro(ctx, W, H); return; }
      const p = S.prompts[S.idx];
      ctx.fillStyle = C.paper; ctx.font = "800 84px system-ui";
      if (!p) { ctx.fillText("Done", 32, 90); return; }
      const bg = p.label === "__background__"; ctx.fillText(bg ? "No sign" : p.label.replace("_", " "), 32, 86);
      ctx.font = "500 30px system-ui"; ctx.fillStyle = C.dim; ctx.fillText(bg ? "Move your hands naturally" : (S.phrases[p.label] || ""), 32, 190);
      ctx.fillStyle = C.paper; ctx.fillText(bg ? `Background break | Clip ${S.idx + 1} of ${S.prompts.length}` : `Attempt ${p.rep} of ${REPS} | Clip ${S.idx + 1} of ${S.prompts.length}`, 32, 250);
      bar(ctx, 32, 300, W - 64, 16, S.idx / S.prompts.length, C.ok);
      const cueTxt = cueLabel(performance.now());
      if (cueTxt) { ctx.fillStyle = cueTxt === "Sign now" ? C.ok : C.warn; ctx.font = "800 46px system-ui"; ctx.fillText(cueTxt, 32, 332); }
      ctx.fillStyle = S.recording ? C.warn : S.pendingRec ? C.warn : C.ok; ctx.font = "700 32px system-ui";
      const recordStatus = S.recording ? "RECORDING NOW - keep signing" : S.status;
      wrap(ctx, recordStatus, W - 64).forEach((l, i) => ctx.fillText(l, 32, 396 + i * 40));
      return;
    }
    const r = S.pending || S.last;
    ctx.fillStyle = C.paper; ctx.font = "800 88px system-ui";
    ctx.fillText(r && r.decision !== "reject" ? r.gloss.replace("_", " ") : "...", 32, 76);
    if (r) { ctx.font = "600 30px system-ui"; ctx.fillStyle = r.decision === "auto" ? C.ok : r.decision === "confirm" ? C.warn : C.bad;
             ctx.fillText(`${(r.conf * 100).toFixed(0)}% ${r.decision === "confirm" ? "- did you sign this?" : r.decision === "auto" ? "- added" : "- not sure"}`, 32, 186);
             bar(ctx, 32, 232, W - 64, 14, r.conf, ctx.fillStyle); }
    ctx.fillStyle = C.paper; ctx.font = "500 30px system-ui"; wrap(ctx, S.status, W - 64).forEach((l, i) => ctx.fillText(l, 32, 276 + i * 38));
    let x = 32, y = 372; ctx.font = "700 34px system-ui";
    for (const t of S.tokens) { const w = ctx.measureText(t.gloss).width + 36; if (x + w > W - 32) { x = 32; y += 60; }
      ctx.fillStyle = C.line; ctx.beginPath(); ctx.roundRect(x, y, w, 50, 25); ctx.fill(); ctx.fillStyle = C.paper; ctx.fillText(t.gloss.replace("_", " "), x + 18, y + 8); x += w + 12; }
  });
  panels.agent.draw((ctx, W, H) => {
    if (MODE === "record") return drawCoachCard(ctx, W, H);
    ctx.textBaseline = "top"; ctx.fillStyle = C.dim; ctx.font = "500 26px system-ui"; ctx.fillText("Agent", 32, 28);
    ctx.font = "500 30px system-ui"; let y = 76;
    if (S.confirm) { ctx.fillStyle = C.warn; ctx.font = "700 32px system-ui"; ctx.fillText("Please check before I act", 32, y); y += 48;
      ctx.fillStyle = C.paper; ctx.font = "500 32px system-ui"; wrap(ctx, S.confirm.summary, W - 64).slice(0, 8).forEach(l => { ctx.fillText(l, 32, y); y += 42; }); return; }
    ctx.fillStyle = C.paper; for (const l of S.agent.slice(-7)) wrap(ctx, l, W - 64).slice(0, 2).forEach(t => { ctx.fillText(t, 32, y); y += 38; });
  });
  panels.caption.draw((ctx, W, H) => {
    if (MODE === "record") return drawSteps(ctx, W, H);
    ctx.textBaseline = "middle"; ctx.fillStyle = C.paper; ctx.font = "600 40px system-ui"; ctx.textAlign = "center";
    const t = S.caption || "Speech from the other person appears here"; if (!S.caption) ctx.fillStyle = C.dim;
    wrap(ctx, t, W - 60).slice(-2).forEach((l, i, a) => ctx.fillText(l, W / 2, H / 2 + (i - (a.length - 1) / 2) * 50)); ctx.textAlign = "left";
  });
  if (MODE === "live") { panels.confirm.set(!!S.pending); panels.redo.set(!!S.pending); panels.undo.set(S.tokens.length > 0);
                         panels.send.set(S.tokens.length > 0 && !S.busy); panels.clear.set(S.tokens.length > 0); panels.yes.set(!!S.confirm); panels.no.set(!!S.confirm); }
  else { const on = !S.intro; panels.start.set(S.intro); panels.start.mesh.visible = S.intro;
         panels.discard.set(on && !!S.pendingRec); panels.skip.set(on); panels.discard.mesh.visible = panels.skip.mesh.visible = on; }
  drawKazeHud();
}


/* ---------- recording coach (record mode) ---------- */
const IMG = {};
function guideImg(url) {
  if (!url) return null; let im = IMG[url];
  if (!im) { im = IMG[url] = new Image(); im.onload = () => draw(); im.src = url; }
  return im.complete && im.naturalWidth ? im : null;
}
function drawIntro(ctx, W, H) {
  ctx.fillStyle = C.paper; ctx.font = "800 44px system-ui"; ctx.fillText("Before you start", 32, 76);
  if (S.group) { ctx.fillStyle = C.ok; ctx.font = "700 30px system-ui"; ctx.fillText(`Group: ${S.group.name} (${S.group.glosses.length} words)`, 32, 120); }
  ctx.font = "500 29px system-ui"; let y = S.group ? 160 : 140;
  INTRO.forEach((t, i) => { wrap(ctx, `${i + 1}. ${t}`, W - 64).forEach(l => { ctx.fillText(l, 32, y); y += 36; }); y += 6; });
  ctx.fillStyle = C.ok; ctx.font = "700 30px system-ui"; ctx.fillText("Poke Start with your index finger.", 32, Math.min(y + 10, H - 52));
}
function drawCoachCard(ctx, W, H) {
  ctx.textBaseline = "top"; ctx.fillStyle = C.dim; ctx.font = "500 26px system-ui"; ctx.fillText("How to sign", 32, 28);
  const c = cardFor(S.prompts[S.idx], S.guide, S.phrases);
  ctx.fillStyle = C.paper;
  if (S.intro || !c) {
    ctx.font = "600 34px system-ui";
    wrap(ctx, S.intro ? "The sign for each word appears here, with a photo when one is available." : "All clips recorded. Thank you!", W - 64).forEach((l, i) => ctx.fillText(l, 32, 90 + i * 44));
    return;
  }
  if (c.kind === "none") {
    ctx.font = "800 52px system-ui"; ctx.fillText(c.title, 32, 90); ctx.font = "500 34px system-ui";
    wrap(ctx, c.tip, W - 64).forEach((l, i) => ctx.fillText(l, 32, 170 + i * 46)); return;
  }
  const X = 32, Y = 84, Sz = 280, im = guideImg(c.image);
  ctx.save(); ctx.beginPath(); ctx.roundRect(X, Y, Sz, Sz, 20); ctx.clip();
  if (im) { const s = Math.min(im.naturalWidth, im.naturalHeight); ctx.drawImage(im, (im.naturalWidth - s) / 2, (im.naturalHeight - s) / 2, s, s, X, Y, Sz, Sz); }
  else { ctx.fillStyle = C.line; ctx.fillRect(X, Y, Sz, Sz); }
  ctx.restore();
  if (!im) { ctx.fillStyle = C.dim; ctx.font = "500 26px system-ui"; ctx.textAlign = "center"; ctx.fillText("No photo yet", X + Sz / 2, Y + Sz / 2 - 14); ctx.textAlign = "left"; }
  ctx.font = "500 24px system-ui"; ctx.fillStyle = c.verified ? C.ok : C.warn;
  wrap(ctx, c.verified ? "Checked by a signer." : "Draft description. Not yet checked by a signer.", Sz).forEach((l, i) => ctx.fillText(l, X, Y + Sz + 20 + i * 30));
  const TX = 344, TW = W - TX - 32; ctx.fillStyle = C.paper; ctx.font = "700 34px system-ui";
  let y = 84; wrap(ctx, `Means: ${c.meaning}`, TW).slice(0, 2).forEach(l => { ctx.fillText(l, TX, y); y += 42; });
  ctx.font = "500 29px system-ui"; y += 8;
  const how = c.how || "No instructions yet. Ask your coordinator to show you this sign.";
  wrap(ctx, how, TW).slice(0, 9).forEach(l => { ctx.fillText(l, TX, y); y += 38; });
}
function drawSteps(ctx, W, H) {
  const done = !S.prompts[S.idx], step = currentStep({ intro: S.intro, done, pendingRec: S.pendingRec, restUntil: S.restUntil }, performance.now());
  if (step < 0) { ctx.textBaseline = "middle"; ctx.textAlign = "center"; ctx.fillStyle = C.dim; ctx.font = "600 34px system-ui";
    ctx.fillText(done && !S.intro ? "All done" : "Read the steps below, then poke Start", W / 2, H / 2); ctx.textAlign = "left"; return; }
  const gap = 16, w = (W - 64 - gap * 2) / 3;
  STEPS.forEach((t, i) => { const x = 32 + i * (w + gap), on = i === step;
    ctx.fillStyle = on ? C.ok : C.line; ctx.beginPath(); ctx.roundRect(x, 36, w, H - 72, 24); ctx.fill();
    ctx.fillStyle = on ? C.ink : C.dim; ctx.font = "700 30px system-ui"; ctx.textBaseline = "middle"; ctx.textAlign = "center"; ctx.fillText(t, x + w / 2, H / 2); ctx.textAlign = "left"; });
}

/* ---------- get-ready cue (record mode) ---------- */
let actx = null;
function beep(freq, dur) {                              // a short tone before each clip; a no-op where WebAudio is absent (tests, some browsers)
  const AC = globalThis.AudioContext || globalThis.webkitAudioContext;
  if (!AC) return;
  try {
    actx = actx || new AC();
    const o = actx.createOscillator(), g = actx.createGain();
    o.type = "sine"; o.frequency.value = freq; g.gain.value = 0.08;
    o.connect(g); g.connect(actx.destination); o.start();
    g.gain.exponentialRampToValueAtTime(0.0001, actx.currentTime + dur); o.stop(actx.currentTime + dur);
  } catch (e) { /* audio is a nicety, never fatal */ }
}

function unlockAudio() {                                   // called from the Start click: XR needs WebAudio unlocked by a real gesture
  const AC = globalThis.AudioContext || globalThis.webkitAudioContext;
  if (!AC) return;
  try { actx = actx || new AC(); if (actx.state === "suspended") actx.resume(); } catch (e) { /* nicety */ }
}

async function playTts(url, text) {                       // server-rendered mp3 -> WebAudio (reliable inside immersive AR)
  const AC = globalThis.AudioContext || globalThis.webkitAudioContext;
  if (AC) {
    try {
      actx = actx || new AC(); if (actx.state === "suspended") await actx.resume();
      const audio = await actx.decodeAudioData(await (await fetch(url)).arrayBuffer());
      const src = actx.createBufferSource(); src.buffer = audio; src.connect(actx.destination); src.start();
      return;
    } catch (e) { console.warn("tts: WebAudio failed, trying <audio>", e); }
  }
  try { await new Audio(url).play(); return; } catch (e) { /* fall through to speech */ }
  if (text) speak(text);                                   // last resort: the browser's own voice
}

function speak(text) { try { const u = new SpeechSynthesisUtterance(text); u.lang = "en-US"; speechSynthesis.speak(u); } catch (e) { console.warn(e); } }
function armCue(nowMs) {                                // start one countdown per prompt, once
  if (S.cueFor === S.idx) return;
  S.cueFor = S.idx; S.cueStart = nowMs; S.cueTick = null;
}
function cueLabel(nowMs) {                              // "Get ready: 3" ... then "Sign now"
  if (S.cueFor !== S.idx || !S.cueStart) return "";
  const c = cue(S.cueStart, nowMs);
  return c.counting ? `Get ready: ${c.seconds}` : "Sign now";
}

/* ---------- application logic ---------- */
function confirmPending() { const r = S.pending; if (!r) return; S.pending = null; S.tokens.push({ gloss: r.gloss, conf: r.conf }); net.send({ type: "token", gloss: r.gloss, conf: r.conf, decision: "confirmed" }); S.status = "Added"; draw(); }
async function notASign() {                              // "that was not a sign": drop the word and keep the clip as a training negative
  const clip = S.lastClip; if (!clip || S.tokens.length <= S.busyCount) return;
  S.tokens.pop(); net.send({ type: "undo" }); S.lastClip = null; S.lastTokenAt = 0; draw();
  const ok = await net.saveRecording(toRecord(clip, "__background__", SIGNER, SESSION, S.group?.id));
  chat.note(ok ? "Saved as 'not a sign' to improve detection." : "Could not save that clip."); draw();
}
function speakNow() { if (S.speech) { net.send({ type: "speak_now" }); return; } if (S.tokens.length > S.busyCount && !S.busy) sendPhrase(); }   // skip the countdown, or skip the pause
function cancelAll() { net.send({ type: "cancel_speech" }); net.send({ type: "clear" }); S.tokens = S.tokens.slice(0, S.busyCount); S.pending = null; S.speech = null; chat.note("Cancelled. Nothing was said."); draw(); }
function sendPhrase() { if (!S.tokens.length) return; S.busy = true; S.agent = []; S.status = "Sent to the assistant"; net.send({ type: "send", tokens: S.tokens }); draw(); }
function answer(ok) { if (!S.confirm) return; net.send({ type: "confirm", id: S.confirm.id, ok }); S.confirm = null; draw(); }

async function onClip(clip) {
  if (MODE === "record") {
    if (S.pendingRec || S.intro || !S.prompts[S.idx]) return;
    const p = S.prompts[S.idx], rec = toRecord(clip, p.label, SIGNER, SESSION, S.group?.id); S.pendingRec = rec; S.status = `Recording ended. Captured ${(clip.t.at(-1) - clip.t[0]).toFixed(1)} s. Poke Discard within 2 s if it was bad.`; draw();
    setTimeout(async () => { if (S.pendingRec !== rec) return; S.pendingRec = null; const ok = await net.saveRecording(rec); S.saved += ok ? 1 : 0; S.idx++; S.restUntil = performance.now() + REST_MS; setTimeout(draw, REST_MS + 50);
      S.status = ok ? "Saved. Rest your hands, then sign the next word." : "Save failed: is the server running?";
      if (S.idx >= S.prompts.length && !S.reported) {          // full pass done: count the session
        S.reported = true;
        net.send({ type: "session_done", signer: SIGNER, group: S.group?.id, session: SESSION, reps: REPS, saved: S.saved, expected: S.prompts.length });
        S.status = S.group ? `Group "${S.group.name}" complete — ${S.saved} clips saved. Thank you!` : "All clips saved. Thank you!";
      }
      draw(); }, 2000);
    return;
  }
  if (S.pending || (S.busy && ASSIST === "manual" && !S.confirm)) return;      // auto: keep signing while the agent works
  const r = await clf.classify(clip); S.last = r;
  const a = route(r, clf.meta.classes, { assist: ASSIST, confirmOpen: !!S.confirm, ...GATE }), nice = g => g.replace("_", " ").toLowerCase();
  if (a.kind === "answer") { answer(a.ok); S.status = a.ok ? "Approved: you signed YES." : "Not approved: you signed NO."; chat.note(a.ok ? "You signed YES" : "You signed NO"); }
  else if (a.kind === "token") {                       // every recognised sign goes to the agent with its runner-ups: no per-word Yes button
    const merged = mergeToken(S.tokens, { gloss: r.gloss, conf: r.conf, tentative: a.tentative, alts: a.alts }, performance.now(), DEDUPE, S.busyCount);   // same sign again = one word
    net.send({ type: "token", gloss: r.gloss, conf: r.conf, alts: a.alts, decision: r.decision, assist: "auto" });     // the server merges too (and keeps the best confidence)
    S.lastClip = clip; S.lastTokenAt = performance.now();
    S.status = merged ? `${nice(r.gloss)} again: counted once.` : a.tentative && a.alts[0] ? `Heard ${nice(r.gloss)} (maybe ${nice(a.alts[0].gloss)}). Keep signing. I will speak when you pause.` : "Got it. Keep signing. I will speak when you pause.";
  }
  else if (a.kind === "retry") { S.status = S.confirm ? "Sign YES or NO, or poke Yes / No." : "Not sure. Please sign it again."; chat.note(S.status); }
  else if (a.kind === "ignore") S.status = "That did not look like a sign. Try again.";
  else if (r.decision === "auto") { S.tokens.push({ gloss: r.gloss, conf: r.conf }); net.send({ type: "token", gloss: r.gloss, conf: r.conf, decision: "auto" }); S.status = "Added. Sign the next word, or poke Send."; }
  else if (r.decision === "confirm") { S.pending = r; S.status = `Also possible: ${r.alt.replace("_", " ")} (${(r.altConf * 100).toFixed(0)}%)`; }
  else S.status = r.gloss === "__background__" ? "That did not look like a sign. Try again." : "Not sure. Please sign it again.";
  draw();
}

net.on("status", s => { S.conn = s; draw(); });
net.on("caption", m => { S.caption = m.text; chat.doctor(m.text); draw(); });
net.on("agent_log", m => { S.agent.push(m.text); draw(); });
net.on("agent_start", m => { S.busy = true; S.busyCount = m.count; S.busySince = performance.now(); draw(); });
net.on("agent_done", () => { S.busy = false; S.busySince = 0; S.confirm = null; S.tokens = S.tokens.slice(S.busyCount); S.busyCount = 0; S.speech = null;          // signs made while it worked stay for the next turn
                             S.status = S.tokens.length ? "Keep going. I will speak when you pause." : "Done. Ready for the next request."; draw(); });
net.on("speech_pending", m => { S.speech = { id: m.id, text: m.text, notes: m.notes || [], until: performance.now() + m.seconds * 1000 }; draw(); });
net.on("speech_cancelled", () => { S.speech = null; chat.note("Cancelled. Nothing was said."); draw(); });
net.on("speech_done", m => { S.speech = null; chat.signer(m.text); draw(); });
net.on("notice", m => { S.status = m.text; chat.note(m.text); draw(); });
net.on("confirm_request", m => { S.confirm = { id: m.id, summary: m.summary, until: m.seconds ? performance.now() + m.seconds * 1000 : 0 }; draw(); });
net.on("confirm_closed", m => { if (S.confirm && (!m.id || S.confirm.id === m.id)) { S.confirm = null; draw(); } });     // answered elsewhere, timed out, or finished: never leave it open
net.on("tts", m => { playTts(m.url, m.text); });
net.on("say", m => { speak(m.text); });
net.on("normalized", m => { S.status = "I understood: " + m.sentence; draw(); });

/* ---------- XR frame loop ---------- */
function readHand(frame, hand, ref) {
  const out = new Float32Array(63);
  for (let j = 0; j < 21; j++) {
    const sp = hand.get(WEBXR_JOINTS[WEBXR_TO_MP21[j]]), pose = sp && frame.getJointPose(sp, ref);
    if (!pose) return null; const p = pose.transform.position; out[j * 3] = p.x; out[j * 3 + 1] = p.y; out[j * 3 + 2] = p.z;
  }
  return out;
}
const tipVec = [new THREE.Vector3(), new THREE.Vector3()];
function onFrame(time, frame) {
  if (!frame) return; const ref = renderer.xr.getReferenceSpace(), vp = frame.getViewerPose(ref); if (!vp) return;
  const hp = vp.transform.position, hq = vp.transform.orientation;
  if (!placed) { placeUI(new THREE.Vector3(hp.x, hp.y, hp.z), new THREE.Quaternion(hq.x, hq.y, hq.z, hq.w)); draw(); }
  let left = null, right = null; S.hands = false;
  const dotAt = [null, null], dbg = { src: frame.session.inputSources.length, hand: 0, full: 0, dots: 0 };
  for (const src of frame.session.inputSources) if (src.hand) {
    S.hands = true; dbg.hand++;
    const h = readHand(frame, src.hand, ref); if (h) dbg.full++;
    if (src.handedness === "left") left = h; else if (src.handedness === "right") right = h;
    // The dot and the poke buttons need only the index fingertip, so they must not depend on all 21 joints being tracked.
    const sp = src.hand.get("index-finger-tip"), tp = sp && frame.getJointPose(sp, ref);
    if (tp) dotAt[src.handedness === "left" ? 0 : src.handedness === "right" ? 1 : (dotAt[0] ? 1 : 0)] = tp.transform.position;
  }
  const tips = []; dotAt.forEach((p, i) => { tipDots[i].visible = !!p; if (p) { dbg.dots++; tipDots[i].position.set(p.x, p.y, p.z); tips.push(tipDots[i].position); } });
  if (SHOW_SKEL) { skels[0].update(left); skels[1].update(right); }
  if (q.get("debug") && time - lastDbg > 500) { lastDbg = time; drawDebug(dbg, frame.session); }
  if ((MODE === "record" || CARD_UI) && S.hands !== lastHands) { lastHands = S.hands; draw(); }
  buttons.update(tips, time / 1000);
  if (CARD_UI && placed) updateCard(time, hp, hq, left, right, tips);
  const nowMs = performance.now(), until = S.speech ? S.speech.until : S.confirm && S.confirm.until ? S.confirm.until : 0;
  if (until) { const sec = Math.ceil((until - nowMs) / 1000); if (sec !== lastTick) { lastTick = sec; draw(); } }                       // countdowns tick
  if (S.confirm && S.confirm.until && nowMs > S.confirm.until + 2000) { S.confirm = null; draw(); }                                  // watchdogs: nothing can stay stuck
  if (S.speech && nowMs > S.speech.until + 25000) { S.speech = null; draw(); }
  if (S.busy && S.busySince && nowMs - S.busySince > 90000) { S.busy = false; S.busySince = 0; S.busyCount = 0; draw(); }
  if (S.lastTokenAt && nowMs - S.lastTokenAt > 6000) { S.lastTokenAt = 0; draw(); }
  if (!S.hands && Math.floor(time / 2000) % 2 === 0 && S.status.indexOf("hand tracking") < 0) { S.status = "No hands seen. Put controllers down and enable hand tracking in Quest settings."; draw(); }
  if (S.hands && S.status.indexOf("hand tracking") >= 0) { S.status = "Raise both hands and sign"; draw(); }
  const clip = seg.push({ t: time / 1000, left, right, head: [hp.x, hp.y, hp.z] });
  if (MODE === "record") {
    const recording = seg.state === "active";
    if (recording !== S.recording) { S.recording = recording; if (recording) S.status = "Recording now. Keep signing."; draw(); }
    if (!S.intro && !S.pendingRec && nowMs >= S.restUntil && S.idx < S.prompts.length) armCue(nowMs);   // one countdown per prompt, after the rest
    const c = cueLabel(nowMs), sec = c === "" ? null : cue(S.cueStart, nowMs).seconds;
    if (sec !== null && sec !== S.cueTick) { S.cueTick = sec; beep(sec === 0 ? 880 : 620, sec === 0 ? 0.18 : 0.07); draw(); }
  }
  if (clip) onClip(clip);
  renderer.render(scene, camera);
}

async function selfTest() {
  const feats = await (await fetch("./tests/parity_features.json")).json(); let worst = 0;
  for (const c of feats) { const f = extractFeatures(c), r = c.features.flat(); for (let i = 0; i < f.length; i++) worst = Math.max(worst, Math.abs(f[i] - r[i])); }
  let msg = `features max diff ${worst.toExponential(1)}`, ok = worst < 1e-4;
  try { const pm = await (await fetch("./models/parity_model.json")).json(); let w2 = 0;
    for (const c of pm) { const p = await clf.predictFeatures(Float32Array.from(c.features.flat())); p.forEach((v, i) => w2 = Math.max(w2, Math.abs(v - c.probs[i]))); }
    msg += `, onnx max diff ${w2.toExponential(1)}`; ok = ok && w2 < 1e-3; } catch (e) { msg += ", no model parity file"; }
  S.status = (ok ? "Self-test PASS: " : "Self-test FAIL: ") + msg; log(S.status); draw();
}

async function enterXR() {
  unlockAudio();                                           // inside the click gesture: XR audio is blocked otherwise
  try { await net.startDoctorAudio(); } catch (e) { console.warn("microphone unavailable", e); }
  const ks = MODE === "live" ? kazeSettings() : { enabled: false };
  if (ks.enabled) {
    try { await kaze.start(ks.minutes); }
    catch (e) { document.getElementById("safetyStatus").textContent = e.message; return log(e.message); }
  }
  try {
    if (!navigator.xr || !(await navigator.xr.isSessionSupported("immersive-ar"))) return log("Passthrough AR is not available in this browser.");
    renderer.xr.setReferenceSpaceType("local");
    const session = await navigator.xr.requestSession("immersive-ar", { optionalFeatures: ["hand-tracking", "local"] });
    session.addEventListener("end", () => net.stopDoctorAudio(), { once: true });
    await renderer.xr.setSession(session); renderer.setAnimationLoop(onFrame);
  } catch (e) { log("Could not start XR: " + e.message); }
}

kaze.onState = s => {
  kaze.state = s;
  if (s.state !== kazeLastState) { kazeLastState = s.state; if (s.state === "checkin_due") chat.note("Safety check-in due. Tap I'm safe."); }
  draw();
};

(async function main() {
  initThree(); $("mode").textContent = MODE === "record" ? `Recording mode for ${SIGNER}` : "Live mode";
  if (MODE === "record") {
    let glosses = [];
    try {
      const v = await (await fetch("/api/vocab")).json(); S.phrases = v.phrases || {}; S.guide = v.guide || {};
      glosses = (v.glosses || []).slice(0, 6);              // fallback if the server has no groups yet
      const g = await (await fetch(`/api/groups?signer=${encodeURIComponent(SIGNER)}`)).json();
      const groups = g.groups || [];
      S.group = groups.find(x => x.id === GROUP) || pickGroup(groups);
      if (S.group) { S.group.needed = S.group.needed || 1; glosses = S.group.glosses; }
    } catch (e) { console.log("could not load words from the server:", e); S.status = "Could not load the word list from the server. Is it running?"; }
    S.prompts = BACKGROUND_ONLY ? buildBackgroundPrompts(REPS * Math.max(1, glosses.length))
      : buildPrompts(glosses, REPS, [...SIGNER].reduce((a, c) => a + c.charCodeAt(0), 7));
  } else {
    try { await clf.load(); log(`Model loaded (${clf.meta.classes.length} classes)`); if (q.get("selftest")) await selfTest(); }
    catch (e) { S.status = "Model missing: run ml/export_onnx first."; log("Model failed to load: " + e.message); }
  }
  draw(); $("enter").disabled = false; $("enter").onclick = enterXR;
})().catch(e => {                                            // a dead Start button is worse than a partial app: always hand it back
  log("Startup error: " + ((e && e.message) || e));
  const b = $("enter"); if (b) { b.disabled = false; b.onclick = enterXR; }
});
if (globalThis.__LUMEN_TEST__) Object.assign(globalThis.__LUMEN_TEST__, { S, draw, panels, buttons, onClip, chat, follow, net, clf, onFrame, tipDots, kaze });
