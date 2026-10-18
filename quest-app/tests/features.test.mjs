import { readFileSync } from "node:fs";
import { extractFeatures, FEATURE_DIM, T_FIXED } from "../src/features.js";
const clips = JSON.parse(readFileSync(new URL("./parity_features.json", import.meta.url)));
let worst = 0;
clips.forEach((c, i) => {
  const f = extractFeatures(c), ref = c.features.flat();
  if (f.length !== T_FIXED * FEATURE_DIM || ref.length !== f.length) throw new Error("size mismatch");
  let e = 0, at = 0; for (let k = 0; k < f.length; k++) { const d = Math.abs(f[k] - ref[k]); if (d > e) { e = d; at = k; } }
  console.log(`clip ${i}: max |JS - Python| = ${e.toExponential(2)} at frame ${Math.floor(at / FEATURE_DIM)} col ${at % FEATURE_DIM}`);
  worst = Math.max(worst, e);
});
if (worst > 1e-4) { console.error("FAIL parity"); process.exit(1); }
console.log("PASS: JS features match Python (< 1e-4)");
