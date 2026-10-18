// Guided data-collection protocol: one word per block, background clips between blocks, one JSON record per clip.
const r4 = v => Math.round(v * 1e4) / 1e4;
export function toRecord(clip, label, signer, session, group) {
  const hand = h => h.map(f => (f ? Array.from(f, r4) : null));
  return { label, signer, session, group, device: "quest3s", t: clip.t.map(x => Math.round((x - clip.t[0]) * 1e4) / 1e4),
           left: hand(clip.left), right: hand(clip.right) };
}
// The default group is the first one this signer has not finished yet; falls back to the first group.
export function pickGroup(groups) {
  if (!groups || !groups.length) return null;
  return groups.find(g => !(g.sessions > 0)) || groups[0];
}
export function buildPrompts(glosses, reps, seed = 1) {
  let s = seed; const rnd = () => (s = (s * 1664525 + 1013904223) >>> 0) / 2 ** 32;   // deterministic per signer/session
  const out = [];
  const order = [...glosses];
  for (let i = order.length - 1; i > 0; i--) { const j = Math.floor(rnd() * (i + 1)); [order[i], order[j]] = [order[j], order[i]]; }
  for (const label of order) {
    for (let r = 0; r < reps; r++) out.push({ label, rep: r + 1 });
    out.push({ label: "__background__", rep: 1 });
  }
  return out;
}

export function buildBackgroundPrompts(count) {
  return Array.from({ length: Math.max(1, Math.floor(count)) }, () => ({ label: "__background__", rep: 1 }));
}
