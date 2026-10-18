// Routing for live mode, kept free of DOM/WebXR so it can be unit-tested. Decides what one classified sign means.
// In "auto" assist every recognised sign (even an uncertain one) is sent to the agent together with its runner-up
// candidates, so the agent can repair mistakes from context. In "manual" assist the old yes/redo flow is used.
export const MIN_ALT = 0.10;
export const TENT_MIN = 0.60;      // an uncertain ("confirm"-level) word needs at least this confidence to be accepted without a click ...
export const BG_MAX = 0.25;        // ... and the model may give "not a sign" at most this much probability
export const DEDUPE_MS = 2500;     // the same sign again within this window is one word

export function altsOf(probs, classes, chosen, n = 3) {
  return Array.from(probs, (p, i) => ({ gloss: classes[i], conf: p }))
    .filter(x => x.gloss !== "__background__" && x.gloss !== chosen && x.conf >= MIN_ALT)
    .sort((a, b) => b.conf - a.conf).slice(0, n - 1).map(x => ({ gloss: x.gloss, conf: +x.conf.toFixed(3) }));
}

// r: classifier result {gloss, conf, decision, probs}; st: {assist: "auto"|"manual", confirmOpen: bool}
export function route(r, classes, st) {
  if (st.confirmOpen) {                                      // the agent is waiting for a yes/no: signs answer it, they never become words
    if (r.gloss === "YES" && r.decision === "auto") return { kind: "answer", ok: true };
    if (r.gloss === "NO" && r.decision !== "reject") return { kind: "answer", ok: false };   // declining is always cheap to accept
    return { kind: "retry" };
  }
  if (r.gloss === "__background__") return { kind: "ignore" };
  if (r.decision === "reject") return { kind: "retry" };
  if (st.assist === "auto") {
    if (r.decision === "confirm") {                          // an uncertain word is accepted only if it is clearly not "no sign"
      const bg = classes.indexOf("__background__"), pBg = bg >= 0 && r.probs ? r.probs[bg] : 0;
      if (r.conf < (st.tentMin ?? TENT_MIN) || pBg > (st.bgMax ?? BG_MAX)) return { kind: "ignore" };
    }
    return { kind: "token", tentative: r.decision === "confirm", alts: altsOf(r.probs, classes, r.gloss) };
  }
  return { kind: "manual" };
}

/** Add a recognised sign to the waiting list, or merge it into the previous one if it is the same sign again within the window.
 *  Only signs the agent has not taken yet (index >= minIndex) can be merged into. Returns true if merged. */
export function mergeToken(list, tok, nowMs, windowMs = DEDUPE_MS, minIndex = 0) {
  const last = list.length > minIndex ? list[list.length - 1] : null;
  if (last && last.gloss === tok.gloss && nowMs - (last.t ?? -1e9) < windowMs) {
    last.count = (last.count || 1) + 1; last.conf = Math.max(last.conf, tok.conf); last.t = nowMs; last.tentative = last.tentative && tok.tentative; return true;
  }
  list.push({ ...tok, t: nowMs, count: 1 }); return false;
}
