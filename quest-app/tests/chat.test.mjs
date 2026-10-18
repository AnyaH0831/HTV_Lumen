import assert from "node:assert/strict";
import { Chat, chipFor, drawCard, lazyYaw, lazyAnchor, yawFromQuat, cardPose, handFade, CARD, wrapText } from "../src/chat.js";
const C = { paper: "#fff", dim: "#999", ok: "#0f0", warn: "#fa0", bad: "#f00", line: "#222" };

// ---- conversation model
const c = new Chat(3);
c.doctor("  Where does it hurt?  "); c.signer("Pain, head."); c.note("Not sure."); c.note("Not sure."); c.doctor(""); c.doctor("Since when?");
assert.deepEqual(c.msgs.map(m => m.side + ":" + m.text), ["me:Pain, head.", "sys:Not sure.", "them:Since when?"], "limit keeps the newest, blank lines and repeated notes are dropped");

// ---- state chip
const now = 10000;
assert.deepEqual(chipFor({ confirm: {} }, now), { text: "Check this", tone: "warn" });
assert.deepEqual(chipFor({ speech: { until: now + 2400 } }, now), { text: "Speaking in 3", tone: "ok" });
assert.equal(chipFor({ busy: true }, now).text, "Working it out"); assert.equal(chipFor({ hands: false }, now).text, "Show your hands");
assert.equal(chipFor({ hands: true, pendingWords: 2 }, now).text, "Listening"); assert.equal(chipFor({ hands: true, pendingWords: 0 }, now).text, "Ready");

// ---- drawing: a fake 2D context records the text
function fakeCtx() { const texts = []; let font = "30px system-ui";
  return { texts, set font(v) { font = v; }, get font() { return font }, measureText: s => ({ width: String(s).length * 14 }),
    fillText(t, x, y) { texts.push({ t: String(t), x, y, align: this.textAlign, dashed: false }); }, setLineDash(d) { this._dash = d; },
    beginPath() {}, roundRect() {}, fill() {}, stroke() {}, arc() {}, set fillStyle(v) {}, set strokeStyle(v) {}, set lineWidth(v) {}, set shadowBlur(v) {}, set shadowColor(v) {}, set textBaseline(v) {} }; }
const chat = new Chat(); chat.doctor("Where does it hurt?"); chat.signer("Pain, head."); chat.note("Cancelled. Nothing was said.");
let ctx = fakeCtx();
drawCard(ctx, 800, 511, chat, { words: [{ gloss: "PAIN", conf: .9 }, { gloss: "EAR", conf: .5, tentative: true }], pending: { text: "I have a question.", notes: ["ear read as eye"] }, ask: null, chip: { text: "Speaking in 2", tone: "ok" } }, C);
const all = ctx.texts.map(t => t.t); const at = s => ctx.texts.find(t => t.t.includes(s));
for (const s of ["Speaking in 2", "Where does it hurt?", "Pain, head.", "Cancelled. Nothing was said.", "PAIN", "EAR ?", "I have a question.", "fixed: ear read as eye"]) assert.ok(at(s), `missing: ${s} in ${all}`);
assert.ok(at("Where does it hurt?").x < 400 && at("Pain, head.").x > 400, "doctor on the left, the signer's words on the right");
assert.ok(at("I have a question.").y > at("Pain, head.").y, "the not-yet-spoken sentence is the newest, at the bottom");
ctx = fakeCtx(); drawCard(ctx, 800, 511, new Chat(), { words: [], pending: null, ask: { summary: "I understood: pain, fall. Is this right?" }, chip: { text: "Check this", tone: "warn" } }, C);
assert.ok(ctx.texts.some(t => t.t.includes("Sign YES or NO")) && ctx.texts.some(t => t.t.includes("Sign when you are ready")));
ctx = fakeCtx(); const big = new Chat(); for (let i = 0; i < 40; i++) big.doctor("message number " + i); drawCard(ctx, 800, 511, big, { words: [], chip: { text: "Ready", tone: "dim" } }, C);
assert.ok(ctx.texts.some(t => t.t === "message number 39") && !ctx.texts.some(t => t.t === "message number 0"), "only the newest messages fit; the newest is always shown");
assert.ok(ctx.texts.every(t => t.y >= 0 && t.y < 511), "nothing is drawn outside the card");
assert.ok(wrapText(fakeCtx(), "one two three four five six", 100).length > 1);

// ---- yaw + placement
const yaw = a => ({ x: 0, y: Math.sin(a / 2), z: 0, w: Math.cos(a / 2) });
for (const a of [0, 0.5, -1.2, 3]) assert.ok(Math.abs(Math.atan2(Math.sin(yawFromQuat(yaw(a)) - a), Math.cos(yawFromQuat(yaw(a)) - a))) < 1e-9, "yawFromQuat " + a);
const p0 = cardPose([0, 1.6, 0], 0); assert.ok(Math.abs(p0.pos[2] + CARD.dist) < 1e-9 && Math.abs(p0.pos[1] - (1.6 - CARD.drop)) < 1e-9 && Math.abs(p0.pos[0]) < 1e-9, "in front (-z), below eye level, centred");
const pl = cardPose([0, 1.6, 0], 0, "left"); assert.ok(pl.pos[0] < 0); assert.ok(cardPose([0, 1.6, 0], 0, "right").pos[0] > 0);
const p90 = cardPose([0, 1.6, 0], Math.PI / 2); assert.ok(p90.pos[0] < -CARD.dist + 0.01, "after turning left 90 degrees the card is on the left: -x");

// ---- lazy follow: holds still for small turns, glides after a big one, then stops
const st = { yaw: 0, active: false };
for (let i = 0; i < 60; i++) lazyYaw(st, 0.2, 1 / 60); assert.equal(st.yaw, 0, "a 11 degree turn does not move the card");
for (let i = 0; i < 300; i++) lazyYaw(st, 1.0, 1 / 60); assert.ok(Math.abs(st.yaw - 1.0) < 0.05 && !st.active, "a big turn: it follows, then settles");
const small = { yaw: 3.0, active: false }; for (let i = 0; i < 120; i++) lazyYaw(small, -3.0, 1 / 60); assert.equal(small.yaw, 3.0, "a 16 degree turn across the +-pi seam is inside the dead zone");
const wrap = { yaw: 2.8, active: false }; for (let i = 0; i < 300; i++) lazyYaw(wrap, -2.8, 1 / 60);
assert.ok(Math.abs(Math.atan2(Math.sin(wrap.yaw + 2.8), Math.cos(wrap.yaw + 2.8))) < 0.1, "a 39 degree turn across the seam goes the short way and settles");
assert.ok(Math.abs(wrap.yaw) > 2.5 || Math.abs(wrap.yaw - 2.8) < 1, "it did not swing the long way round through 0");

// ---- fade when a hand is between the eye and the card
const eye = [0, 1.6, 0], pose = cardPose(eye, 0);
assert.equal(handFade(eye, pose, [[0, 1.45, -0.4]]), true, "a hand raised in front of the card hides part of it");
assert.equal(handFade(eye, pose, [[0, 1.3, -0.45]]), false, "a hand signing at chest height is BELOW the card: it does not need to fade");
assert.equal(handFade(eye, pose, [[0.9, 1.3, -0.45]]), false, "a hand far off to the side does not");
assert.equal(handFade(eye, pose, [[0, 1.3, -1.5]]), false, "something BEHIND the card does not count");
assert.equal(handFade(eye, pose, [[0, 2.4, -0.4]]), false, "a hand above the card does not"); assert.equal(handFade(eye, pose, []), false);

// ---- reachable: close (arm's length) and a little above the signing space
assert.ok(CARD.dist <= 0.55 && CARD.dist >= 0.4, "within arm's reach"); assert.ok(CARD.drop <= 0.15, "higher than before (0.30 below the eyes)");
assert.ok(0.36 / 0.5 < CARD.w / CARD.dist * 1.1 && CARD.w / CARD.dist > 0.8, "about the same angular size as the old far card, so text stays readable");

// ---- anchored follow: it does NOT move with you; it re-centres only after a real change, and never while you are poking it
const eyeP = [0, 1.6, 0]; const an = { pos: [...eyeP], yaw: 0, active: false };
for (let i = 0; i < 200; i++) lazyAnchor(an, [0.1, 1.55, 0.08], 0.2, 1 / 60); assert.deepEqual(an.pos, eyeP); assert.equal(an.yaw, 0, "leaning, ducking and a 11 degree turn leave the card where it is");
for (let i = 0; i < 200; i++) lazyAnchor(an, [0.0, 1.6, 0.7], 0.0, 1 / 60, true); assert.deepEqual(an.pos, eyeP, "frozen while a fingertip is near it, even if you walked off");
for (let i = 0; i < 400; i++) lazyAnchor(an, [0.0, 1.6, 0.7], 0.0, 1 / 60); assert.ok(Math.abs(an.pos[2] - 0.7) < 0.05 && !an.active, "after walking away it follows, then settles");
const turn = { pos: [...eyeP], yaw: 0, active: false }; for (let i = 0; i < 400; i++) lazyAnchor(turn, eyeP, 1.2, 1 / 60); assert.ok(Math.abs(turn.yaw - 1.2) < 0.06, "a big turn re-centres it");
const mid = { pos: [...eyeP], yaw: 0, active: false }; for (let i = 0; i < 30; i++) lazyAnchor(mid, [0.9, 1.6, 0.9], 1.4, 1 / 60); const y0 = mid.yaw; for (let i = 0; i < 60; i++) lazyAnchor(mid, [0.9, 1.6, 0.9], 1.4, 1 / 60, true); assert.equal(mid.yaw, y0, "poking freezes it mid-glide");

// ---- the live signing line always shows the NEWEST words, collapses repeats, and marks what was scrolled off
ctx = fakeCtx(); const many = Array.from({ length: 14 }, (_, i) => ({ gloss: "W" + i, conf: .9 }));
drawCard(ctx, 800, 509, new Chat(), { words: many, pending: null, ask: null, chip: { text: "Ready", tone: "dim" } }, C);
const shownWords = ctx.texts.map(t => t.t); assert.ok(shownWords.includes("W13"), "the newest word is visible"); assert.ok(!shownWords.includes("W0"), "the oldest scrolled off"); assert.ok(shownWords.includes("..."), "and the card says so");
ctx = fakeCtx(); drawCard(ctx, 800, 509, new Chat(), { words: [{ gloss: "PAIN", conf: .9, count: 3 }, { gloss: "EAR", conf: .9, count: 1, tentative: true }], pending: null, ask: null, chip: { text: "Ready", tone: "dim" } }, C);
assert.ok(ctx.texts.some(t => t.t === "PAIN x3") && ctx.texts.some(t => t.t === "EAR ?"), ctx.texts.map(t => t.t).join("|"));

// ---- a question shows how long is left, so it can never feel stuck
assert.equal(chipFor({ confirm: { until: now + 17200 } }, now).text, "Check this 18"); assert.equal(chipFor({ confirm: { until: now - 5 } }, now).text, "Check this 0");
console.log("PASS chat");
