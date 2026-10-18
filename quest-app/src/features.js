// JS twin of ml/signbridge_ml/features.py. Any change here must be mirrored there (tests/features.test.mjs proves parity).
// Clip format: { t:[sec...], left:[Float32Array|number[63]|null ...], right:[...] }  (21 MediaPipe-ordered joints x,y,z)
export const T_FIXED = 48, FEATURE_DIM = 152, N_JOINTS = 21;
const VEL_SCALE = 10, DUR_SCALE = 2, EPS = 1e-8;
const WRIST = 0, INDEX_MCP = 5, INDEX_TIP = 8, MIDDLE_MCP = 9, PINKY_MCP = 17;
export const WEBXR_JOINTS = ["wrist","thumb-metacarpal","thumb-phalanx-proximal","thumb-phalanx-distal","thumb-tip",
  "index-finger-metacarpal","index-finger-phalanx-proximal","index-finger-phalanx-intermediate","index-finger-phalanx-distal","index-finger-tip",
  "middle-finger-metacarpal","middle-finger-phalanx-proximal","middle-finger-phalanx-intermediate","middle-finger-phalanx-distal","middle-finger-tip",
  "ring-finger-metacarpal","ring-finger-phalanx-proximal","ring-finger-phalanx-intermediate","ring-finger-phalanx-distal","ring-finger-tip",
  "pinky-finger-metacarpal","pinky-finger-phalanx-proximal","pinky-finger-phalanx-intermediate","pinky-finger-phalanx-distal","pinky-finger-tip"];
export const WEBXR_TO_MP21 = [0,1,2,3,4,6,7,8,9,11,12,13,14,16,17,18,19,21,22,23,24];

function interp(x, xp, fp) {                       // == np.interp (xp nondecreasing)
  const n = xp.length;
  if (n === 1 || x <= xp[0]) return fp[0];
  if (x >= xp[n - 1]) return fp[n - 1];
  let lo = 0, hi = n - 1;
  while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (xp[mid] <= x) lo = mid; else hi = mid; }
  const dx = xp[hi] - xp[lo];
  return dx === 0 ? fp[lo] : fp[lo] + (fp[hi] - fp[lo]) * (x - xp[lo]) / dx;
}
function median(a) { const s = Float64Array.from(a).sort(); const n = s.length; return n % 2 ? s[(n - 1) / 2] : 0.5 * (s[n / 2 - 1] + s[n / 2]); }

function interpHand(hand, ts, td) {
  const T = td.length, out = new Float64Array(T * 63), present = new Uint8Array(T);
  const vi = []; for (let i = 0; i < hand.length; i++) if (hand[i]) vi.push(i);
  if (!vi.length) return { out, present };
  const tv = vi.map(i => ts[i]);
  for (let d = 0; d < 63; d++) { const fp = vi.map(i => hand[i][d]); for (let k = 0; k < T; k++) out[k * 63 + d] = interp(td[k], tv, fp); }
  const m = Array.from(hand, h => (h ? 1 : 0));
  for (let k = 0; k < T; k++) present[k] = interp(td[k], ts, m) >= 0.5 ? 1 : 0;
  return { out, present };
}
const J = (P, k, j) => k * 63 + j * 3;              // offset of joint j at frame k
const norm3 = (x, y, z) => Math.sqrt(x * x + y * y + z * z);

export function extractFeatures(clip, T = T_FIXED) {
  const ts = clip.t, n = ts.length, t0 = ts[0], t1 = ts[n - 1];
  const td = new Float64Array(T); for (let k = 0; k < T; k++) td[k] = k === T - 1 ? t1 : t0 + ((t1 - t0) / (T - 1)) * k;
  const P = [], M = [];
  for (const hand of [clip.left, clip.right]) {
    const { out, present } = interpHand(hand, ts, td);
    for (let k = 0; k < T; k++) if (!present[k]) out.fill(0, k * 63, (k + 1) * 63);
    P.push(out); M.push(present);
  }
  const duration = Math.max(t1 - t0, 1e-3);
  const sizes = [], ref = [0, 0, 0]; let cnt = 0;
  for (let h = 0; h < 2; h++) for (let k = 0; k < T; k++) if (M[h][k]) {
    const w = J(0, k, WRIST), m = J(0, k, MIDDLE_MCP), p = P[h];
    sizes.push(norm3(p[m] - p[w], p[m + 1] - p[w + 1], p[m + 2] - p[w + 2]));
    ref[0] += p[w]; ref[1] += p[w + 1]; ref[2] += p[w + 2]; cnt++;
  }
  let S = 1; if (cnt) { S = median(sizes) + EPS; ref[0] /= cnt; ref[1] /= cnt; ref[2] /= cnt; }

  const PER = 73, F = FEATURE_DIM, out = new Float32Array(T * F);
  const wristRel = [new Float64Array(T * 3), new Float64Array(T * 3)];
  for (let h = 0; h < 2; h++) {
    const p = P[h], left = h === 0;
    for (let k = 0; k < T; k++) {
      const w = J(0, k, WRIST), base = k * F + h * PER, m = M[h][k];
      wristRel[h][k * 3] = (p[w] - ref[0]) / S; wristRel[h][k * 3 + 1] = (p[w + 1] - ref[1]) / S; wristRel[h][k * 3 + 2] = (p[w + 2] - ref[2]) / S;
      if (left) wristRel[h][k * 3] *= -1;
      if (!m) continue;
      let y = [p[J(0, k, MIDDLE_MCP)] - p[w], p[J(0, k, MIDDLE_MCP) + 1] - p[w + 1], p[J(0, k, MIDDLE_MCP) + 2] - p[w + 2]];
      let ny = norm3(...y) + EPS; y = y.map(v => v / ny);
      let a = [p[J(0, k, INDEX_MCP)] - p[J(0, k, PINKY_MCP)], p[J(0, k, INDEX_MCP) + 1] - p[J(0, k, PINKY_MCP) + 1], p[J(0, k, INDEX_MCP) + 2] - p[J(0, k, PINKY_MCP) + 2]];
      const ad = a[0] * y[0] + a[1] * y[1] + a[2] * y[2]; a = a.map((v, i) => v - ad * y[i]);
      const na = norm3(...a) + EPS; const x = a.map(v => v / na);
      const z = [x[1] * y[2] - x[2] * y[1], x[2] * y[0] - x[0] * y[2], x[0] * y[1] - x[1] * y[0]];
      for (let j = 1; j < 21; j++) {
        const o = J(0, k, j), d0 = p[o] - p[w], d1 = p[o + 1] - p[w + 1], d2 = p[o + 2] - p[w + 2];
        const c = base + (j - 1) * 3;
        out[c] = (d0 * x[0] + d1 * x[1] + d2 * x[2]) / S; out[c + 1] = (d0 * y[0] + d1 * y[1] + d2 * y[2]) / S;
        out[c + 2] = ((d0 * z[0] + d1 * z[1] + d2 * z[2]) / S) * (left ? -1 : 1);
      }
      const yy = left ? [-y[0], y[1], y[2]] : y, zz = left ? [z[0], -z[1], -z[2]] : z;
      out.set(yy, base + 60); out.set(zz, base + 63);
      out.set([wristRel[h][k * 3], wristRel[h][k * 3 + 1], wristRel[h][k * 3 + 2]], base + 66);
      out[base + 72] = 1;
    }
    for (let k = 0; k < T; k++) {                    // velocity = np.gradient(prel) * VEL_SCALE, zero when absent
      if (!M[h][k]) continue;
      for (let c = 0; c < 3; c++) {
        const g = k === 0 ? wristRel[h][3 + c] - wristRel[h][c] : k === T - 1 ? wristRel[h][k * 3 + c] - wristRel[h][(k - 1) * 3 + c]
                : (wristRel[h][(k + 1) * 3 + c] - wristRel[h][(k - 1) * 3 + c]) / 2;
        out[k * F + h * PER + 69 + c] = g * VEL_SCALE;
      }
    }
  }
  for (let k = 0; k < T; k++) {
    const base = k * F + 2 * PER;
    if (M[0][k] && M[1][k]) {
      const w0 = J(0, k, WRIST), w1 = w0, t0i = J(0, k, INDEX_TIP);
      const d = [(P[1][w1] - P[0][w0]) / S, (P[1][w1 + 1] - P[0][w0 + 1]) / S, (P[1][w1 + 2] - P[0][w0 + 2]) / S];
      out.set(d, base); out[base + 3] = norm3(...d);
      out[base + 4] = norm3(P[1][t0i] - P[0][t0i], P[1][t0i + 1] - P[0][t0i + 1], P[1][t0i + 2] - P[0][t0i + 2]) / S;
    }
    out[base + 5] = duration / DUR_SCALE;
  }
  return out;
}
