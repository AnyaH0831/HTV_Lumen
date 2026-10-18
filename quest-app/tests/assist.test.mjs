import assert from "node:assert/strict";
import { altsOf, route, mergeToken, DEDUPE_MS } from "../src/assist.js";
const classes = ["__background__", "PAIN", "EAR", "EYE", "YES", "NO"];
const r = (gloss, decision, probs) => ({ gloss, decision, conf: Math.max(...probs), probs });

// runner-ups: best first, never the chosen word or background, never below 10%
assert.deepEqual(altsOf([0.05, 0.1, 0.5, 0.3, 0.04, 0.01], classes, "EAR"), [{ gloss: "EYE", conf: 0.3 }, { gloss: "PAIN", conf: 0.1 }]);
assert.deepEqual(altsOf([0.6, 0.05, 0.3, 0.03, 0.01, 0.01], classes, "EAR"), [], "background mass is not a candidate");
assert.equal(altsOf(new Float32Array([0, 0.1, 0.5, 0.3, 0, 0]), classes, "EAR").length, 2, "typed arrays work");

const auto = { assist: "auto", confirmOpen: false }, manual = { assist: "manual", confirmOpen: false };
let a = route(r("PAIN", "auto", [0, .9, .05, .05, 0, 0]), classes, auto); assert.deepEqual([a.kind, a.tentative], ["token", false]);
a = route(r("EAR", "confirm", [0, .1, .62, .2, .03, .05]), classes, auto); assert.equal(a.kind, "token"); assert.equal(a.tentative, true); assert.equal(a.alts[0].gloss, "EYE");
assert.equal(route(r("EAR", "reject", [.3, .2, .3, .1, .05, .05]), classes, auto).kind, "retry");
assert.equal(route(r("__background__", "reject", [.9, .02, .02, .02, .02, .02]), classes, auto).kind, "ignore");
assert.equal(route(r("EAR", "confirm", [0, .1, .5, .35, .03, .02]), classes, manual).kind, "manual", "manual mode keeps the old flow");

// while the agent waits for yes/no, signs answer it and never become words
const wait = { assist: "auto", confirmOpen: true };
assert.deepEqual(route(r("YES", "auto", [0, 0, 0, 0, .95, .05]), classes, wait), { kind: "answer", ok: true });
assert.equal(route(r("YES", "confirm", [0, 0, 0, 0, .6, .3]), classes, wait).kind, "retry", "an unsure YES must not approve anything");
assert.deepEqual(route(r("NO", "confirm", [0, 0, 0, 0, .3, .6]), classes, wait), { kind: "answer", ok: false });
assert.equal(route(r("PAIN", "auto", [0, .95, 0, 0, 0, 0]), classes, wait).kind, "retry");
assert.equal(route(r("YES", "auto", [0, 0, 0, 0, .95, .05]), classes, { assist: "manual", confirmOpen: true }).kind, "answer", "signed answers work in manual mode too");

// an uncertain word is accepted only if it is clearly not "no sign" (fewer false words from idle movement)
const eq = (g, d, p) => route(r(g, d, p), classes, auto);
assert.equal(eq("EAR", "confirm", [.05, .1, .6, .2, .03, .02]).kind, "token", "confident enough, little background");
assert.equal(eq("EAR", "confirm", [.05, .1, .5, .3, .03, .02]).kind, "ignore", "below the 0.60 minimum");
assert.equal(eq("EAR", "confirm", [.3, .0, .65, .0, .03, .02]).kind, "ignore", "30% 'not a sign' is too much for a word nobody confirmed");
assert.equal(route(r("EAR", "confirm", [.3, 0, .65, 0, .03, .02]), classes, { ...auto, bgMax: .4 }).kind, "token", "limits are adjustable");
assert.equal(eq("PAIN", "auto", [.2, .75, .02, .01, .01, .01]).kind, "token", "a confident word is not affected");

// ---- the same sign again within the window is one word
const q = []; const t0 = 1000;
assert.equal(mergeToken(q, { gloss: "PAIN", conf: .7 }, t0), false); assert.equal(mergeToken(q, { gloss: "PAIN", conf: .9 }, t0 + 800), true); assert.equal(mergeToken(q, { gloss: "PAIN", conf: .6 }, t0 + 1500), true);
assert.deepEqual([q.length, q[0].count, q[0].conf], [1, 3, .9], "three detections, one word, best confidence kept");
assert.equal(mergeToken(q, { gloss: "HEAD", conf: .9 }, t0 + 1600), false, "a different sign is a new word");
assert.equal(mergeToken(q, { gloss: "PAIN", conf: .9 }, t0 + 1700), false, "PAIN after HEAD is not 'right after' PAIN");
const late = [{ gloss: "NO", conf: .9, t: t0, count: 1 }]; assert.equal(mergeToken(late, { gloss: "NO", conf: .9 }, t0 + DEDUPE_MS + 1), false, "after the window it is a new word");
const taken = [{ gloss: "NO", conf: .9, t: t0, count: 1 }]; assert.equal(mergeToken(taken, { gloss: "NO", conf: .9 }, t0 + 100, DEDUPE_MS, 1), false, "a sign the agent already took is never merged into");
console.log("PASS assist");
