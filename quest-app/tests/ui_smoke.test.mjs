// Runs the real headset UI code (src/main.js) in record mode with a fake canvas that records every fillText,
// then checks what a person wearing the headset would be shown. No headset, no rendering. Run via `npm test`.
import assert from "node:assert/strict";

const texts = new WeakMap(); let drawnImages = 0;
function makeCtx(store) {
  const state = {};
  return new Proxy(state, {
    get(t, k) {
      if (k === "fillText") return (text, x, y) => store.push({ text: String(text), x, y, fill: t.fillStyle });
      if (k === "drawImage") return () => { drawnImages++; };
      if (k === "clearRect") return () => { store.length = 0; };
      if (k === "measureText") return s => ({ width: String(s).length * 14 });
      return k in t ? t[k] : () => {};
    },
    set(t, k, v) { t[k] = v; return true; },
  });
}
const els = {};
globalThis.document = { createElement: () => { const store = []; const c = { width: 1, height: 1, style: {}, getContext: () => makeCtx(store) }; texts.set(c, store); return c; },
  getElementById: id => els[id] || (els[id] = { style: {}, textContent: "", disabled: id === "enter", appendChild() {}, onclick: null }),
  body: { appendChild() {} } };
globalThis.location = { search: "?mode=record&signer=alice&session=s1&reps=1", host: "localhost:8000", protocol: "http:" };
globalThis.WebSocket = class { constructor() { setTimeout(() => this.onopen && this.onopen(), 0); } send() {} close() {} };
globalThis.addEventListener = () => {}; globalThis.innerWidth = 800; globalThis.innerHeight = 600; globalThis.devicePixelRatio = 1;
globalThis.Image = class { constructor() { this.complete = false; this.naturalWidth = 0; } set src(u) { this.url = u; if (u.includes("PAIN")) { this.complete = true; this.naturalWidth = 640; this.naturalHeight = 480; } } };
const vocab = { glosses: ["PAIN", "HELLO"], phrases: { PAIN: "pain", HELLO: "hello" },
  guide: { PAIN: { how: "Point both index fingers toward each other and jab them together.", verified: false, image: "/signs/PAIN.jpg" },
           HELLO: { how: "Flat hand to the forehead, then outward.", verified: true } } };
const groups = { groups: [{ id: "g1", name: "Symptoms", glosses: ["PAIN", "HELLO"], needed: 1,
                            sessions: 0, done: false, clips: { PAIN: 3, HELLO: 0 } }], nextSession: "s1" };
globalThis.fetch = async url => ({ json: async () => String(url).includes("/api/groups") ? groups : vocab, ok: true });
globalThis.__LUMEN_TEST__ = {};

await import("../src/main.js"); await new Promise(r => setTimeout(r, 30));
const T = globalThis.__LUMEN_TEST__, shown = p => texts.get(T.panels[p].canvas).map(t => t.text).join(" | ");

// Regression: the HTML Start button ships disabled and must be handed back by main(). A "grayed-out
// Start" on the headset means main() threw before this line, so assert it here.
assert.equal(els.enter.disabled, false, "the Start button is enabled after startup");
assert.equal(typeof els.enter.onclick, "function", "the Start button has its XR handler attached");

// the first unfinished group is picked and named on the intro screen
assert.equal(T.S.group.id, "g1");
assert.ok(shown("status").includes("Group: Symptoms (2 words)"), "the picked word group is named: " + shown("status"));

// 1. Intro: instructions are visible, Start is the only usable button.
assert.ok(shown("status").includes("Before you start") && shown("status").includes("Poke Start"), shown("status"));
assert.ok(/Do not sign/.test(shown("status")) && /Discard/.test(shown("status")), "intro explains no-sign clips and Discard");
assert.ok(shown("caption").includes("poke Start"), shown("caption"));
assert.equal(T.panels.start.enabled, true); assert.equal(T.panels.skip.enabled, false); assert.equal(T.panels.discard.enabled, false);
// clips are ignored until the person has read the intro and pressed Start
await T.onClip({ t: [0, 1], left: [null, null], right: [null, null] }); assert.equal(T.S.pendingRec, null);

// 2. After Start: the word, its meaning, a how-to card and the step strip are all shown.
T.panels.start.onPress();
const first = T.S.prompts[T.S.idx].label; assert.ok(["PAIN", "HELLO"].includes(first), first);
const st = shown("status"), card = shown("agent"), strip = shown("caption");
assert.ok(st.includes(first.replace("_", " ")) && /Clip 1 of/.test(st), st);
assert.ok(card.includes("How to sign") && card.includes(`Means: ${first.toLowerCase()}`), card);
assert.ok(strip.includes("2  Sign once") && strip.includes("1  Rest") && strip.includes("3  Wait for Saved"), strip);
assert.ok(/Get ready: \d|Sign now/.test(st), "a get-ready cue is shown before the clip: " + st);
assert.equal(T.panels.start.mesh.visible, false); assert.equal(T.panels.skip.enabled, true);
// Force each sign so both cards are always checked (the random order would otherwise hide one branch).
const orig = T.S.prompts[T.S.idx];
T.S.prompts[T.S.idx] = { label: "PAIN", rep: 1 }; T.draw(); drawnImages = 0; T.draw();
let c = shown("agent");
assert.ok(c.includes("Draft description"), "unverified signs are labelled: " + c); assert.ok(c.includes("jab"), c);
assert.ok(drawnImages > 0, "photo drawn when one exists"); assert.ok(!c.includes("No photo yet"));
T.S.prompts[T.S.idx] = { label: "HELLO", rep: 1 }; T.draw(); c = shown("agent");
assert.ok(c.includes("Checked by a signer") && !c.includes("Draft description") && c.includes("No photo yet"), c);
T.S.guide.HELLO = undefined; T.draw(); c = shown("agent");
const flat = c.replaceAll(" | ", " ");   // long sentences wrap across several drawn lines
assert.ok(flat.includes("No instructions yet. Ask your coordinator to show you this sign."), "no invented text when a guide entry is missing: " + c);
T.S.prompts[T.S.idx] = orig; T.S.guide.HELLO = vocab.guide.HELLO; T.draw();

// 3. A captured clip highlights step 3 (the active pill is drawn dark on the teal fill).
T.S.pendingRec = {}; T.draw();
const pill = texts.get(T.panels.caption.canvas).find(t => t.text.includes("3  Wait for Saved")); assert.equal(pill.fill, "#0E1A24", "step 3 highlighted");
T.S.pendingRec = null;

// 4. Background prompts show a "No sign" card with a tip instead of a sign description.
T.S.prompts[T.S.idx] = { label: "__background__", rep: 1 }; T.draw();
assert.ok(/No sign/.test(shown("agent")) && /Do not sign|No signing/.test(shown("agent")), shown("agent"));
// 5. Finished: thank-you, no stale instructions.
T.S.idx = T.S.prompts.length; T.draw();
assert.ok(shown("agent").includes("All clips recorded") && shown("caption").includes("All done"));
console.log("PASS ui smoke (record mode)"); process.exit(0);
