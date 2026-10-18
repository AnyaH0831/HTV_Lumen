// The live conversation card (default UI) driven through the real onFrame with fake head/hand frames, a fake canvas, socket and fetch.
import assert from "node:assert/strict";
import { WEBXR_JOINTS } from "../src/features.js";
import { CARD } from "../src/chat.js";
const texts = new WeakMap();
function makeCtx(store) {
  const state = {};
  return new Proxy(state, { get(t, k) {
      if (k === "fillText") return (text, x, y) => store.push({ t: String(text), x, y });
      if (k === "clearRect") return () => { store.length = 0; };
      if (k === "measureText") return s => ({ width: String(s).length * 14 });
      return k in t ? t[k] : () => {};
    }, set(t, k, v) { t[k] = v; return true; } });
}
const el = () => ({ style: {}, textContent: "", appendChild() {}, set onclick(f) {} });
globalThis.document = { createElement: () => { const store = []; const c = { width: 1, height: 1, style: {}, getContext: () => makeCtx(store) }; texts.set(c, store); return c; }, getElementById: () => el(), body: { appendChild() {} } };
globalThis.location = { search: "?signer=tester", host: "localhost:8000", protocol: "http:" };
const sent = [], posted = []; globalThis.WebSocket = class { constructor() { setTimeout(() => this.onopen && this.onopen(), 0); } send(m) { sent.push(JSON.parse(m)); } close() {} };
globalThis.addEventListener = () => {}; globalThis.innerWidth = 800; globalThis.innerHeight = 600; globalThis.devicePixelRatio = 1; globalThis.Image = class {};
globalThis.fetch = async (url, opts) => { if (opts && opts.method === "POST") { posted.push({ url, body: JSON.parse(opts.body) }); return { ok: true, json: async () => ({}) }; } return { json: async () => ({ glosses: ["PAIN", "EAR", "EYE", "YES", "NO"], phrases: {}, guide: {} }), ok: true }; };
globalThis.__LUMEN_TEST__ = {};
await import("../src/main.js"); await new Promise(r => setTimeout(r, 30));
const T = globalThis.__LUMEN_TEST__, shown = () => texts.get(T.panels.card.canvas).map(t => t.t).join(" | ").replaceAll(" | ", " ");
const classes = ["__background__", "PAIN", "EAR", "EYE", "YES", "NO"]; T.clf.meta = { classes };
T.clf.classify = async () => ({ gloss: "__background__", decision: "reject", conf: .9, probs: [.9, .02, .02, .02, .02, .02] });

// ---- fake XR frames
const yawQ = a => ({ x: 0, y: Math.sin(a / 2), z: 0, w: Math.cos(a / 2) });
function handSource(handedness, wrist) {
  const hand = new Map(WEBXR_JOINTS.map((n, i) => [n, { pose: { transform: { position: { x: wrist[0] + i * 0.003, y: wrist[1] + i * 0.002, z: wrist[2] - i * 0.002 } } } }]));
  return { hand, handedness };
}
let now = 1000;
const frame = (headYaw, hands = [], headPos = [0, 1.6, 0]) => ({ session: { inputSources: hands.map(h => handSource(...h)), enabledFeatures: ["hand-tracking"] },
  getViewerPose: () => ({ transform: { position: { x: headPos[0], y: headPos[1], z: headPos[2] }, orientation: yawQ(headYaw) } }), getJointPose: sp => sp.pose });
const run = (n, headYaw, hands, headPos) => { for (let i = 0; i < n; i++) { now += 11; T.onFrame(now, frame(headYaw, hands, headPos)); } };
const card = () => T.panels.card.mesh, near = (a, b, e = 0.02) => Math.abs(a - b) < e;
const FAR = [["right", [0.9, 1.0, -0.45]]];                      // a hand well away from the card and from the buttons

// 1. placed within arm's reach, a little above the signing space, facing the wearer; controls hidden
run(1, 0); assert.ok(T.panels.card && T.panels.status === undefined);
assert.ok(near(card().position.x, 0) && near(card().position.y, 1.6 - CARD.drop) && near(card().position.z, -CARD.dist), JSON.stringify(card().position));
assert.ok(CARD.dist <= 0.55, "reachable"); assert.deepEqual([card().lookAtTarget.x, card().lookAtTarget.y, card().lookAtTarget.z], [0, 1.6, 0], "faces the head");
for (const b of ["cancel", "now", "yes", "no", "notsign"]) assert.equal(T.panels[b].mesh.visible, false, b + " hidden until needed");
assert.ok(shown().includes("Show your hands")); assert.ok(T.panels.cancel.mesh.position.y < card().position.y - 0.12, "button row under the card");
assert.ok(T.panels.cancel.mesh.position.z > -0.6 && T.panels.cancel.mesh.position.z < -0.4, "buttons are at arm's length too");

// 2. a signing hand at chest height does not fade the card (it is above); a hand raised in front of it does
run(60, 0, [["right", [0, 1.2, -0.4]]]); assert.ok(T.S.hands && card().material.opacity > 0.95, "chest-height signing: stays opaque " + card().material.opacity);
run(60, 0, [["right", [0, 1.46, -0.4]]]); assert.ok(card().material.opacity < 0.4, "a raised hand fades it: " + card().material.opacity);
run(80, 0, FAR); assert.ok(card().material.opacity > 0.9);

// 3. it does NOT move with you: leaning or a small turn leaves it; only a big turn or walking away re-centres it, and then it settles
const p0 = card().position.clone();
run(100, 0.2, FAR, [0.12, 1.55, 0.1]); assert.ok(near(card().position.x, p0.x, 1e-6) && near(card().position.z, p0.z, 1e-6), "leaning + 11 degree turn: the card stays put");
run(500, 0.9, FAR); assert.ok(near(card().position.x, -Math.sin(0.9) * CARD.dist, 0.05) && near(card().position.z, -Math.cos(0.9) * CARD.dist, 0.05), `big turn: ${card().position.x.toFixed(2)}, ${card().position.z.toFixed(2)}`);
const settled = card().position.clone(); run(150, 0.9, FAR); assert.ok(near(card().position.x, settled.x, 1e-6), "and then it stops");
// ... and it never moves while a fingertip is near it, even if you walk off
run(40, 0.9, [["right", [card().position.x - 0.027, card().position.y - 0.018, card().position.z + 0.018]]]); const held = card().position.clone();
run(200, 0.9, [["right", [card().position.x - 0.027, card().position.y - 0.018, card().position.z + 0.018]]], [0, 1.6, 1.5]);
assert.ok(near(card().position.x, held.x, 1e-6) && near(card().position.z, held.z, 1e-6), "poking: it holds still");
run(300, 0.9, FAR, [0, 1.6, 1.5]); assert.ok(card().position.z > held.z + 0.5, "once you stop poking, it follows you across the room");
run(300, 0, FAR, [0, 1.6, 0]);

// 4. signs go to the agent with runner-ups; no per-word Yes
const sign = async (gloss, decision, probs) => { T.clf.classify = async () => ({ gloss, decision, conf: Math.max(...probs), probs }); await T.onClip({ t: [0, 1], left: [null, null], right: [null, null] }); };
await sign("PAIN", "auto", [0, .9, .05, .03, 0, 0]); await sign("EAR", "confirm", [0, .1, .62, .2, .03, .05]);
const toks = sent.filter(m => m.type === "token"); assert.equal(toks.length, 2); assert.deepEqual([toks[1].gloss, toks[1].decision, toks[1].alts[0].gloss, toks[1].assist], ["EAR", "confirm", "EYE", "auto"]);
run(1, 0, FAR); assert.ok(shown().includes("PAIN") && shown().includes("EAR ?"), shown()); assert.equal(T.panels.cancel.mesh.visible, true); assert.equal(T.panels.yes.mesh.visible, false);

// 5. fewer false words: an uncertain sign that is mostly "not a sign" is ignored, not queued
const before = T.S.tokens.length, n0 = sent.length; await sign("EYE", "confirm", [.35, 0, 0, .6, .03, .02]); await sign("EYE", "confirm", [.05, 0, 0, .5, .3, .15]);
assert.equal(T.S.tokens.length, before); assert.equal(sent.length, n0, "nothing sent to the agent");

// 6. repeats: the same sign right after itself is ONE word (shown as x3), while the server also gets the detections to merge
T.S.tokens = []; sent.length = 0;
await sign("PAIN", "auto", [0, .9, .05, .03, 0, 0]); await sign("PAIN", "auto", [0, .8, .1, .03, 0, 0]); await sign("PAIN", "auto", [0, .95, .02, .03, 0, 0]);
assert.equal(T.S.tokens.length, 1); assert.deepEqual([T.S.tokens[0].count, T.S.tokens[0].conf], [3, .95]); run(1, 0, FAR); assert.ok(shown().includes("PAIN x3"), shown());
await sign("EAR", "auto", [0, .02, .9, .05, 0, 0]); await sign("PAIN", "auto", [0, .9, .05, .03, 0, 0]); assert.deepEqual(T.S.tokens.map(t => t.gloss), ["PAIN", "EAR", "PAIN"], "PAIN after EAR is a new word");

// 7. "Not sign": appears for a few seconds after a detection; pressing it removes the word and saves the clip as a negative
run(1, 0, FAR); assert.equal(T.panels.notsign.mesh.visible, true, "offered right after a detection");
sent.length = 0; posted.length = 0; T.panels.notsign.onPress(); await new Promise(r => setTimeout(r, 10));
assert.deepEqual(T.S.tokens.map(t => t.gloss), ["PAIN", "EAR"], "the false word is removed"); assert.equal(sent.at(-1).type, "undo");
assert.equal(posted.length, 1); assert.deepEqual([posted[0].url, posted[0].body.label, posted[0].body.signer], ["/api/recordings", "__background__", "tester"]);
run(1, 0, FAR); assert.ok(shown().includes("not a sign"), shown()); assert.equal(T.panels.notsign.mesh.visible, false, "one press per detection");

// 8. the live signing line always shows the newest words
T.S.tokens = Array.from({ length: 14 }, (_, i) => ({ gloss: "W" + i, conf: .9, count: 1 })); T.draw(); assert.ok(shown().includes("W13") && !shown().includes("W0 "), shown());
T.S.tokens = [];

// 9. THE STUCK-QUESTION BUG: a question that ends any way other than the wearer answering must never leave the headset in "answer only" mode
T.net.h.confirm_request({ id: "c1", summary: "I understood: pain, fall. Is this right?", seconds: 25 }); run(1, 0, FAR);
assert.ok(shown().includes("Check this") && T.panels.yes.mesh.visible && !T.panels.cancel.mesh.visible, shown());
T.net.h.confirm_closed({ id: "c1" }); await sign("PAIN", "auto", [0, .9, .05, .03, 0, 0]); assert.equal(T.S.confirm, null); assert.equal(T.S.tokens.length, 1, "after the question closed elsewhere, signs are words again");
T.net.h.confirm_request({ id: "c2", summary: "x", seconds: 25 }); T.net.h.agent_done(); assert.equal(T.S.confirm, null, "agent_done closes any question");
T.S.tokens = []; T.net.h.confirm_request({ id: "c3", summary: "x", seconds: 25 }); T.S.confirm.until = performance.now() - 5000; run(1, 0, FAR);
assert.equal(T.S.confirm, null, "a question whose time ran out disappears even if the server message is lost"); await sign("PAIN", "auto", [0, .9, .05, .03, 0, 0]); assert.equal(T.S.tokens.length, 1);
T.net.h.confirm_request({ id: "c4", summary: "x", seconds: 25 }); T.net.h.confirm_closed({ id: "other" }); assert.ok(T.S.confirm, "closing a different question does not close this one"); T.net.h.confirm_closed({ id: "c4" });
// ... and signing YES still answers a real question
T.net.h.confirm_request({ id: "c5", summary: "Is this right?", seconds: 25 }); const nn = sent.length; await sign("YES", "confirm", [0, 0, 0, 0, .6, .3]); assert.equal(sent.length, nn, "an unsure YES approves nothing");
await sign("YES", "auto", [0, 0, 0, 0, .95, .03]); assert.deepEqual([sent.at(-1).type, sent.at(-1).id, sent.at(-1).ok], ["confirm", "c5", true]);
// a busy flag or countdown that never got its 'done' message is cleared by the watchdogs
T.S.tokens = []; T.net.h.agent_start({ count: 1 }); T.S.busySince = performance.now() - 100000; run(1, 0, FAR); assert.equal(T.S.busy, false, "busy watchdog");
T.S.speech = { id: "x", text: "t", notes: [], until: performance.now() - 30000 }; run(1, 0, FAR); assert.equal(T.S.speech, null, "speech watchdog");

// 10. the server announces the sentence: it appears on the right, counts down, can be cancelled; once spoken it stays in the chat
T.S.tokens = [{ gloss: "A", conf: 1, count: 1 }, { gloss: "B", conf: 1, count: 1 }]; sent.length = 0;
T.net.h.caption({ text: "Where does it hurt?" }); T.net.h.agent_start({ count: 2 }); T.net.h.speech_pending({ id: "s1", text: "Pain, ear.", notes: ["eye read as ear"], seconds: 3 }); run(1, 0, FAR);
assert.ok(shown().includes("Where does it hurt?") && shown().includes("Pain, ear.") && shown().includes("Speaking in 3") && shown().includes("fixed: eye read as ear"), shown());
assert.equal(T.panels.now.mesh.visible, true); T.panels.cancel.onPress(); assert.deepEqual(sent.slice(-2).map(m => m.type), ["cancel_speech", "clear"]); assert.equal(T.S.speech, null);
T.net.h.agent_done(); T.net.h.speech_pending({ id: "s2", text: "Sick.", notes: [], seconds: 3 }); T.net.h.speech_done({ id: "s2", text: "Sick." }); run(1, 0, FAR);
assert.ok(shown().includes("Sick.") && !shown().includes("Speaking in"), shown());
T.S.tokens = [{ gloss: "A", conf: 1 }, { gloss: "B", conf: 1 }, { gloss: "C", conf: 1 }]; T.net.h.agent_start({ count: 2 }); T.net.h.agent_done(); assert.deepEqual(T.S.tokens.map(t => t.gloss), ["C"], "signs made while it worked are kept");
T.net.h.notice({ text: "Please sign again, a little slower." }); run(1, 0, FAR); assert.ok(shown().includes("Please sign again"));
for (let i = 0; i < 80; i++) T.net.h.caption({ text: "line " + i + " of a long conversation that wraps onto several lines of the card" });
run(1, 0, FAR); assert.ok(texts.get(T.panels.card.canvas).every(t => t.y > -1 && t.y < 520), "all text inside the card");

// 11. KAZE check-in: one circular I'M SAFE button, thick ring around it, beside the card — always findable in live mode
assert.equal(T.panels.kazeButton.mesh.visible, true, "always available in live mode, even before a watch starts");
T.kaze.onState({ state: "watching", next_checkin_in: 42 }); run(1, 0, FAR);
assert.equal(T.panels.kazeButton.mesh.visible, true);
assert.equal(T.panels.card.mesh.visible, true, "conversation card stays up");
assert.equal(T.panels.safe.mesh.visible, false, "the under-card duplicate stays off");
assert.ok(Number.isFinite(T.panels.kazeButton.mesh.position.x) && Number.isFinite(T.panels.kazeButton.mesh.position.z), "button has a real world position");
assert.ok(Math.abs(T.panels.kazeButton.mesh.position.x - card().position.x) > 0.15, "HUD sits beside the card, not on top of it");
const kazeShown = () => texts.get(T.panels.kazeButton.canvas).map(t => t.t).join(" ");
assert.ok(kazeShown().includes("I'M") && kazeShown().includes("SAFE") && kazeShown().includes("42"), kazeShown());
T.kaze.onState({ state: "checkin_due", respond_within: 7 }); run(1, 0, FAR);
assert.equal(T.panels.kazeButton.mesh.visible, true); assert.equal(T.panels.kazeButton.enabled, true);
assert.ok(kazeShown().includes("SAFE") && kazeShown().includes("7"), kazeShown());
T.kaze.onState({ state: "alert_pending", alert_in: 5 }); T.draw();
assert.equal(T.panels.kazeButton.enabled, false, "alert states need the PIN, not a tap");
T.kaze.onState({ state: "idle" }); T.draw();
assert.equal(T.panels.kazeButton.mesh.visible, true, "still there when the watch stops");
assert.ok(kazeShown().includes("SAFE") && !/\d/.test(kazeShown()), "no countdown while not watching: " + kazeShown());
console.log("PASS ui card smoke"); process.exit(0);
