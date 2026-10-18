// ONNX Runtime Web inference + the deployment decision policy (auto-accept / ask to confirm / reject).
import { extractFeatures, T_FIXED, FEATURE_DIM } from "./features.js";

export class Classifier {
  constructor() { this.lat = []; }
  async load(base = "./models/") {
    this.meta = await (await fetch(base + "meta.json")).json();
    ort.env.wasm.numThreads = 1;                                   // no SharedArrayBuffer/COOP headers needed
    ort.env.wasm.wasmPaths = "/vendor/";
    this.session = await ort.InferenceSession.create(base + "signbridge.onnx", { executionProviders: ["wasm"] });
    this.lat = [];
    await this.predictFeatures(new Float32Array(T_FIXED * FEATURE_DIM));   // warm-up
    return this;
  }
  async predictFeatures(feat) {
    const t0 = performance.now();
    const out = await this.session.run({ features: new ort.Tensor("float32", feat, [1, T_FIXED, FEATURE_DIM]) });
    const probs = Array.from(out.probs.data);
    this.lat.push(performance.now() - t0); if (this.lat.length > 200) this.lat.shift();
    return probs;
  }
  async classify(clip) {
    const t0 = performance.now(), feat = extractFeatures(clip), probs = await this.predictFeatures(feat);
    const order = probs.map((p, i) => [p, i]).sort((a, b) => b[0] - a[0]);
    const [p1, i1] = order[0], [p2, i2] = order[1], m = this.meta;
    const gloss = m.classes[i1];
    let decision = "reject";
    if (gloss !== "__background__") {
      if (p1 >= m.tau_auto && p1 - p2 >= (m.margin_auto ?? 0.15)) decision = "auto";
      else if (p1 >= m.tau_confirm) decision = "confirm";
    }
    return { gloss, conf: p1, alt: m.classes[i2], altConf: p2, decision, ms: performance.now() - t0, probs };
  }
  latency() { const s = [...this.lat].sort((a, b) => a - b); return s.length ? { p50: s[s.length >> 1], p95: s[Math.floor(s.length * 0.95)] } : null; }
}
