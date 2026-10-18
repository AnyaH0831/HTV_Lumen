// Pure logic for the recording coach, kept free of DOM/WebXR so it can be unit-tested without a headset.
export const STEPS = ["1  Rest", "2  Sign once", "3  Wait for Saved"];
export const REST_MS = 3000;
export const CUE_MS = 2400;          // "get ready" countdown before each clip, ending with a beep
export const BG_TIPS = [
  "Rest your hands. Do not sign.",
  "Adjust your headset or scratch your face.",
  "Wave or point at something. Do not sign.",
  "Gesture the way you do when talking. No signing.",
  "Reach for a cup or your phone.",
  "Clap once or rub your hands together.",
];
export const INTRO = [
  "A word appears here and the sign appears on the right card.",
  "Sign it once at a normal speed with both hands visible, then rest your hands.",
  "Wait for Saved before the next one.",
  "On a No sign prompt, just move your hands naturally. Do not sign.",
  "Bad clip? Poke Discard within 2 seconds.",
];
// Which of the three steps is active: 0 rest, 1 sign, 2 wait. -1 = none (intro or finished).
export function currentStep(s, nowMs) {
  if (s.intro || s.done) return -1;
  if (s.pendingRec) return 2;
  return nowMs < (s.restUntil || 0) ? 0 : 1;
}
export function bgTip(idx) { return BG_TIPS[idx % BG_TIPS.length]; }
// Countdown state for a cue that began at startMs: while counting, `seconds` ticks down; then `go` is true
// (the moment to sign, and the moment to beep). Pure, so the headset can show it without a headset here.
export function cue(startMs, nowMs, ms = CUE_MS) {
  const left = startMs + ms - nowMs;
  return left > 0 ? { counting: true, seconds: Math.ceil(left / 1000), go: false } : { counting: false, seconds: 0, go: true };
}
// Pick the reference-card content for a prompt; never invents text for a sign without a guide entry.
export function cardFor(prompt, guide, phrases) {
  if (!prompt) return null;
  if (prompt.label === "__background__") return { kind: "none", title: "No sign", tip: bgTip(prompt.rep + prompt.label.length) };
  const g = guide[prompt.label] || {};
  return { kind: "sign", title: prompt.label.replace("_", " "), meaning: phrases[prompt.label] || "", how: g.how || "",
           verified: g.verified === true, image: g.image || null };
}
