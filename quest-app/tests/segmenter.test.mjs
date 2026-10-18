import { Segmenter } from "../src/segmenter.js";
const seg = new Segmenter(); const clips = [];
const hand = (t, y, x) => { const a = new Float32Array(63); a[0] = x; a[1] = y; return a; };
const head = [0, 0, 0];
for (let i = 0; i < 300; i++) {                         // 30 fps: 0.5 s rest, 1.2 s sign, 0.7 s rest, 0.3 s absent
  const t = i / 30; let l = null, r = null;
  if (t < 0.5 || (t >= 1.7 && t < 2.4)) { l = hand(t, -0.8, -0.2); r = hand(t, -0.8, 0.2); }
  else if (t < 1.7) { l = hand(t, -0.2, -0.2 - 0.15 * Math.sin((t - 0.5) * 6)); r = hand(t, -0.2, 0.2 + 0.15 * Math.sin((t - 0.5) * 6)); }
  const c = seg.push({ t, left: l, right: r, head }); if (c) clips.push(c);
}
console.log("clips:", clips.length, clips.map(c => (c.t[c.t.length - 1] - c.t[0]).toFixed(2) + "s"));
if (clips.length !== 1) { console.error("FAIL: expected exactly one clip"); process.exit(1); }
const d = clips[0].t[clips[0].t.length - 1] - clips[0].t[0];
if (d < 1.0 || d > 1.7) { console.error("FAIL: duration off", d); process.exit(1); }

// one hand alone must never start detection, however raised and busy it is
const one = new Segmenter(); let oneClips = 0;
for (let i = 0; i < 300; i++) { const t = i / 30;
  const r = (t >= 0.5 && t < 1.7) ? hand(t, -0.2, 0.2 + 0.15 * Math.sin((t - 0.5) * 6)) : hand(t, -0.8, 0.2);
  if (one.push({ t, left: null, right: r, head })) oneClips++;
}
if (oneClips !== 0) { console.error("FAIL: a single visible hand must not produce a clip"); process.exit(1); }
console.log("PASS segmenter");
