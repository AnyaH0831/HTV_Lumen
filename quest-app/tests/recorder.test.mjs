import { buildPrompts, buildBackgroundPrompts, toRecord, pickGroup } from "../src/recorder.js";
import assert from "node:assert/strict";

const GROUPS = [
  { id: "g1", name: "Basics", glosses: ["HELLO", "PLEASE", "THANK_YOU", "YES", "NO", "HELP"], sessions: 1 },
  { id: "g2", name: "Symptoms", glosses: ["PAIN", "SICK", "HOT", "COLD", "WATER"], sessions: 0 },
];

// the default group is the first unfinished one; all finished -> the first group
assert.equal(pickGroup(GROUPS).id, "g2", "defaults to the first unfinished group");
assert.equal(pickGroup(GROUPS.map(g => ({ ...g, sessions: 3 }))).id, "g1", "all groups done: first group");
assert.equal(pickGroup([]), null, "no groups: null");
assert.equal(pickGroup(null), null);

// a group records each of its words REPS times, with one background break after each word
const prompts = buildPrompts(GROUPS[1].glosses, 5, 42);
const signs = prompts.filter(p => p.label !== "__background__");
assert.equal(signs.length, 25, "5 words x 5 reps");
assert.equal(prompts.length - signs.length, 5, "one background clip after each word");
for (const w of GROUPS[1].glosses)
  assert.equal(signs.filter(p => p.label === w).length, 5, `5 reps of ${w}`);

assert.deepEqual(buildBackgroundPrompts(4), [
  { label: "__background__", rep: 1 }, { label: "__background__", rep: 1 },
  { label: "__background__", rep: 1 }, { label: "__background__", rep: 1 },
], "background-only sessions contain only non-signing prompts");

// each record carries the group so clips can be attributed to a word-group session
const clip = { t: [0, 1], left: [new Float32Array(63), null], right: [new Float32Array(63), null] };
const rec = toRecord(clip, "PAIN", "anya", "s1", "g2");
assert.equal(rec.group, "g2");
assert.equal(rec.label, "PAIN");
assert.equal(rec.signer, "anya");
assert.equal(rec.session, "s1");
assert.deepEqual(rec.left[1], null);
console.log("PASS recorder");
