// The conversation card: ONE translucent panel holding the whole exchange, so the wearer never looks back and forth.
//   doctor (speech-to-text) on the left, what the system says for the signer on the right, the live signing line at the bottom,
//   and a state chip on top. No DOM, no three.js: the model, drawing and placement maths are testable in plain Node.
// Close enough to poke (arm's length), a little above the signing space. Same angular size as before, so the text stays readable.
export const CARD = { w: 0.44, h: 0.28, ppm: 2000, alpha: 0.55, dist: 0.50, drop: 0.10, side: 0.20 };

export class Chat {
  constructor(limit = 60) { this.msgs = []; this.limit = limit; this.words = []; this.pending = null; this.ask = null; }
  _add(m) { this.msgs.push(m); if (this.msgs.length > this.limit) this.msgs.shift(); }
  doctor(text) { text = String(text || "").trim(); if (text) this._add({ side: "them", text }); }
  signer(text, flag = false) { text = String(text || "").trim(); if (text) this._add({ side: "me", text, flag }); }
  note(text) {                                              // small centred line; an identical one in a row is not repeated
    const last = this.msgs[this.msgs.length - 1];
    if (last && last.side === "sys" && last.text === text) return;
    this._add({ side: "sys", text });
  }
}

// What the chip at the top of the card says, from the app state. Pure so it can be tested.
export function chipFor(s, nowMs) {
  if (s.confirm) return { text: "Check this" + (s.confirm.until ? ` ${Math.max(0, Math.ceil((s.confirm.until - nowMs) / 1000))}` : ""), tone: "warn" };
  if (s.speech) return { text: `Speaking in ${Math.max(0, Math.ceil((s.speech.until - nowMs) / 1000))}`, tone: "ok" };
  if (s.busy) return { text: "Working it out", tone: "warn" };
  if (!s.hands) return { text: "Show your hands", tone: "bad" };
  if (s.pendingWords > 0) return { text: "Listening", tone: "ok" };
  return { text: "Ready", tone: "dim" };
}

export function wrapText(ctx, text, maxW) {
  const out = []; let cur = "";
  for (const w of String(text).split(/\s+/)) { const t = cur ? cur + " " + w : w; if (ctx.measureText(t).width > maxW && cur) { out.push(cur); cur = w; } else cur = t; }
  if (cur) out.push(cur); return out;
}

/** Draw the card into a W x H design space (W = 800). `st` = { words:[{gloss,conf,tentative}], pending:{text,until,notes}, ask:{summary}, chip:{text,tone} }. */
export function drawCard(ctx, W, H, chat, st, C) {
  const PAD = 28, tone = { ok: C.ok, warn: C.warn, bad: C.bad, dim: C.dim };
  ctx.textBaseline = "top"; ctx.shadowColor = "rgba(0,0,0,0.55)"; ctx.shadowBlur = 6;
  // chip
  ctx.font = "700 24px system-ui"; const chip = st.chip || { text: "Ready", tone: "dim" }, cw = ctx.measureText(chip.text).width + 36;
  ctx.shadowBlur = 0; ctx.fillStyle = "rgba(255,255,255,0.10)"; ctx.beginPath(); ctx.roundRect(PAD, 18, cw, 38, 19); ctx.fill();
  ctx.fillStyle = tone[chip.tone] || C.dim; ctx.beginPath(); ctx.arc(PAD + 16, 37, 6, 0, 7); ctx.fill();
  ctx.shadowBlur = 6; ctx.fillStyle = C.paper; ctx.fillText(chip.text, PAD + 30, 24);

  // bottom block: ask box (if any) above the live signing line
  let bottom = H - PAD;
  ctx.font = "700 30px system-ui"; ctx.shadowBlur = 0;
  const wordsY = bottom - 50; let x = PAD;
  if (!st.words.length) { ctx.fillStyle = C.dim; ctx.font = "500 26px system-ui"; ctx.fillText("Sign when you are ready", PAD, wordsY + 8); }
  // the NEWEST words always fit: when there are too many, the oldest scroll off the left (a repeated sign shows as "PAIN x3")
  const wordItems = st.words.map(t => { const unsure = !!(t.tentative || t.conf < 0.75); return { unsure, text: t.gloss.replace("_", " ") + (t.count > 1 ? " x" + t.count : "") + (unsure ? " ?" : "") }; });
  ctx.font = "700 28px system-ui"; const widths = wordItems.map(i => ctx.measureText(i.text).width + 42), room = W - 2 * PAD;
  let start = wordItems.length, used = 0; while (start > 0 && used + widths[start - 1] <= room) { start--; used += widths[start]; }
  while (start > 0 && start < wordItems.length - 1 && used + 54 > room) { used -= widths[start]; start++; }             // leave room for the "..." chip
  if (start > 0) { ctx.fillStyle = "rgba(255,255,255,0.14)"; ctx.beginPath(); ctx.roundRect(x, wordsY, 44, 46, 23); ctx.fill(); ctx.fillStyle = C.paper; ctx.fillText("...", x + 8, wordsY + 6); x += 54; }
  for (let i = start; i < wordItems.length; i++) {
    const it = wordItems[i], w = widths[i] - 10; ctx.font = "700 28px system-ui";
    ctx.fillStyle = it.unsure ? "rgba(242,180,65,0.28)" : "rgba(53,194,176,0.28)"; ctx.beginPath(); ctx.roundRect(x, wordsY, w, 46, 23); ctx.fill();
    ctx.fillStyle = C.paper; ctx.fillText(it.text, x + 16, wordsY + 9); x += w + 10;
  }
  bottom = wordsY - 14;
  if (st.ask) {
    ctx.font = "600 28px system-ui"; const lines = wrapText(ctx, st.ask.summary, W - 2 * PAD - 32).slice(0, 3), h = 52 + lines.length * 36;
    ctx.fillStyle = "rgba(242,180,65,0.18)"; ctx.strokeStyle = C.warn; ctx.lineWidth = 3; ctx.beginPath(); ctx.roundRect(PAD, bottom - h, W - 2 * PAD, h, 22); ctx.fill(); ctx.stroke();
    ctx.fillStyle = C.paper; lines.forEach((l, i) => ctx.fillText(l, PAD + 16, bottom - h + 14 + i * 36));
    ctx.fillStyle = C.warn; ctx.font = "700 24px system-ui"; ctx.fillText("Sign YES or NO, or poke a button", PAD + 16, bottom - 36); bottom -= h + 12;
  }

  // messages, newest at the bottom; the pending (not yet spoken) sentence is the newest of all
  const items = chat.msgs.map(m => ({ ...m }));
  if (st.pending) items.push({ side: "me", text: st.pending.text, dashed: true, notes: st.pending.notes || [] });
  const top = 74, maxW = 540; let y = bottom; ctx.shadowBlur = 0;
  for (let i = items.length - 1; i >= 0 && y > top + 40; i--) {
    const m = items[i];
    if (m.side === "sys") { ctx.font = "500 22px system-ui"; const l = wrapText(ctx, m.text, W - 2 * PAD).slice(0, 2); y -= l.length * 28 + 6;
      ctx.fillStyle = C.dim; ctx.textAlign = "center"; l.forEach((t, k) => ctx.fillText(t, W / 2, y + k * 28)); ctx.textAlign = "left"; continue; }
    ctx.font = "500 30px system-ui"; let lines = wrapText(ctx, m.text, maxW - 32);
    if (lines.length > 4) { lines = lines.slice(0, 4); lines[3] = lines[3].replace(/\s*\S*$/, "") + "..."; }
    const w = Math.min(maxW, Math.max(...lines.map(l => ctx.measureText(l).width)) + 32), extra = m.notes && m.notes.length ? 30 : 0, h = lines.length * 38 + 22 + extra;
    y -= h + 12; if (y < top) break;
    const bx = m.side === "me" ? W - PAD - w : PAD;
    ctx.fillStyle = m.side === "me" ? "rgba(53,194,176,0.30)" : "rgba(255,255,255,0.16)"; ctx.beginPath(); ctx.roundRect(bx, y, w, h, 22); ctx.fill();
    if (m.dashed || m.flag) { ctx.strokeStyle = m.flag ? C.warn : C.ok; ctx.lineWidth = 2.5; ctx.setLineDash(m.dashed ? [10, 8] : []); ctx.stroke(); ctx.setLineDash([]); }
    ctx.fillStyle = C.paper; ctx.shadowBlur = 5; lines.forEach((l, k) => ctx.fillText(l, bx + 16, y + 11 + k * 38)); ctx.shadowBlur = 0;
    if (extra) { ctx.font = "500 21px system-ui"; ctx.fillStyle = C.warn; ctx.fillText("fixed: " + m.notes[0].slice(0, 46), bx + 16, y + h - 30); }
  }
}

// ------------------------------------------------------------------ placement maths (plain arrays [x, y, z])
const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2], sub = (a, b) => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
const addS = (a, b, s) => [a[0] + b[0] * s, a[1] + b[1] * s, a[2] + b[2] * s];

/** Head yaw (rotation about +y, 0 = facing -z) from a unit quaternion {x,y,z,w}. */
export function yawFromQuat(q) {
  const fx = -2 * (q.x * q.z + q.w * q.y), fz = -(1 - 2 * (q.x * q.x + q.y * q.y));      // forward = q * (0,0,-1)
  return Math.atan2(-fx, -fz);
}
const fwdOf = yaw => [-Math.sin(yaw), 0, -Math.cos(yaw)], rightOf = yaw => [Math.cos(yaw), 0, -Math.sin(yaw)];

/** Lazy follow: the card holds still until you turn more than `enter` rad away, then glides to face you and stops. state = {yaw, active}. */
export function lazyYaw(state, headYaw, dt, enter = 0.30, exit = 0.04, rate = 2.2) {
  let d = headYaw - state.yaw; d = Math.atan2(Math.sin(d), Math.cos(d));
  if (!state.active && Math.abs(d) > enter) state.active = true;
  if (state.active) { state.yaw += d * (1 - Math.exp(-rate * dt)); if (Math.abs(d) < exit) state.active = false; }
  return state.yaw;
}

/** Where the card (and the row of buttons under it) sit: below eye level, in front, optionally to one side. */
export function cardPose(head, yaw, side = "center") {
  const f = fwdOf(yaw), r = rightOf(yaw), lateral = side === "left" ? -CARD.side : side === "right" ? CARD.side : 0;
  let pos = addS(head, f, CARD.dist); pos = addS(pos, r, lateral); pos = addS(pos, [0, 1, 0], -CARD.drop);
  return { pos, right: r, up: [0, 1, 0], forward: f, buttons: addS(pos, [0, 1, 0], -(CARD.h / 2 + 0.06)) };
}

/** True if any hand point sits between the eye and the card, in the line of sight to the card's rectangle (the card should fade so you can sign). */
export function handFade(eye, pose, points, margin = 0.05) {
  for (const p of points) {
    const d = sub(p, eye), den = dot(d, pose.forward); if (den <= 1e-6) continue;
    const t = dot(sub(pose.pos, eye), pose.forward) / den; if (t <= 1) continue;          // the point must be nearer than the card
    const hit = addS(eye, d, t), v = sub(hit, pose.pos);
    if (Math.abs(dot(v, pose.right)) < CARD.w / 2 + margin && Math.abs(dot(v, pose.up)) < CARD.h / 2 + margin) return true;
  }
  return false;
}

/** Anchored follow: the card stays where it was put (so you can reach and poke it) and only re-centres after a real change:
 *  a big head turn, walking away, or a posture change. It never moves while a fingertip is near it (`frozen`).
 *  state = { pos: [x, y, z], yaw, active }. */
export function lazyAnchor(state, head, yaw, dt, frozen = false, o = {}) {
  const { enterYaw = 0.55, enterDist = 0.40, enterHeight = 0.25, exit = 0.04, rate = 2.2 } = o;
  const dy = Math.atan2(Math.sin(yaw - state.yaw), Math.cos(yaw - state.yaw)), dx = head[0] - state.pos[0], dz = head[2] - state.pos[2], dh = head[1] - state.pos[1];
  const away = Math.hypot(dx, dz);
  if (frozen) { state.active = false; return state; }
  if (!state.active && (Math.abs(dy) > enterYaw || away > enterDist || Math.abs(dh) > enterHeight)) state.active = true;
  if (state.active) {
    const k = 1 - Math.exp(-rate * dt); state.yaw += dy * k; state.pos[0] += dx * k; state.pos[1] += dh * k; state.pos[2] += dz * k;
    if (Math.abs(dy) < exit && away < 0.03 && Math.abs(dh) < 0.03) state.active = false;
  }
  return state;
}
