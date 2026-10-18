// In-headset UI: canvas-textured panels and fingertip "poke" buttons (no controllers needed).
import * as THREE from "three";
export const C = { ink: "#0E1A24", paper: "#F3EFE6", dim: "#8FA3B0", ok: "#35C2B0", warn: "#F2B441", bad: "#F26B5B", line: "#22384A" };
const FONT = 'system-ui, -apple-system, "Segoe UI", Roboto, sans-serif';

function roundRect(ctx, x, y, w, h, r) { ctx.beginPath(); ctx.roundRect(x, y, w, h, r); }
export function wrap(ctx, text, maxW) {
  const words = String(text).split(/\s+/), lines = []; let cur = "";
  for (const w of words) { const t = cur ? cur + " " + w : w; if (ctx.measureText(t).width > maxW && cur) { lines.push(cur); cur = w; } else cur = t; }
  if (cur) lines.push(cur); return lines;
}

export class Panel {
  constructor(w, h, ppm = 1400, alpha = 0.92) {
    this.w = w; this.h = h; this.alpha = alpha;
    this.canvas = Object.assign(document.createElement("canvas"), { width: Math.round(w * ppm), height: Math.round(h * ppm) });
    this.ctx = this.canvas.getContext("2d"); this.tex = new THREE.CanvasTexture(this.canvas); this.tex.colorSpace = THREE.SRGBColorSpace;
    this.mesh = new THREE.Mesh(new THREE.PlaneGeometry(w, h), new THREE.MeshBasicMaterial({ map: this.tex, transparent: true }));
    this.px = this.canvas.width / 800;                     // design in an 800-px-wide space
  }
  draw(fn, bg = C.ink) {
    const { ctx, canvas } = this; ctx.save(); ctx.clearRect(0, 0, canvas.width, canvas.height); ctx.scale(this.px, this.px);
    const W = 800, H = 800 * this.h / this.w; ctx.globalAlpha = this.alpha; ctx.fillStyle = bg; roundRect(ctx, 0, 0, W, H, 28); ctx.fill();
    ctx.globalAlpha = 1; fn(ctx, W, H); ctx.restore(); this.tex.needsUpdate = true;
  }
  place(pos, lookAt) { this.mesh.position.copy(pos); this.mesh.lookAt(lookAt); }
}

export class Button extends Panel {
  constructor(label, onPress, color = C.ok) {
    super(0.13, 0.07, 1600, 1); this.label = label; this.onPress = onPress; this.color = color; this.hitRadius = 0.022; this.enabled = true; this.armed = true; this.flash = 0; this.render();
  }
  render() {
    this.draw((ctx, W, H) => {
      ctx.fillStyle = this.enabled ? this.color : C.line; roundRect(ctx, 6, 6, W - 12, H - 12, 24); ctx.fill();
      ctx.fillStyle = this.enabled ? C.ink : C.dim; ctx.font = `700 ${H * 0.34}px ${FONT}`; ctx.textAlign = "center"; ctx.textBaseline = "middle";
      ctx.fillText(this.label, W / 2, H / 2 + 2);
    }, "rgba(0,0,0,0)");
  }
  set(enabled) { if (enabled !== this.enabled) { this.enabled = enabled; this.render(); } }
}

export class CircleButton extends Panel {
  constructor(label, onPress, color = C.ok) { super(0.16, 0.16, 1600, 1); this.label = label; this.onPress = onPress; this.color = color; this.hitRadius = 0.06; this.enabled = true; this.armed = true; this.flash = 0; this.render(); }
  render() {
    this.draw((ctx, W, H) => {
      ctx.fillStyle = this.enabled ? this.color : C.line; ctx.beginPath(); ctx.arc(W / 2, H / 2, Math.min(W, H) * 0.43, 0, Math.PI * 2); ctx.fill();
      ctx.fillStyle = this.enabled ? C.ink : C.dim; ctx.font = `700 ${H * 0.18}px ${FONT}`; ctx.textAlign = "center"; ctx.textBaseline = "middle";
      ctx.fillText(this.label, W / 2, H / 2 + 2);
    }, "rgba(0,0,0,0)");
  }
  set(enabled) { if (enabled !== this.enabled) { this.enabled = enabled; this.render(); } }
}

export class Buttons {
  constructor() { this.list = []; }
  add(b) { this.list.push(b); return b; }
  update(tips, now) {                                    // tips: THREE.Vector3[] index-finger tips in world space
    for (const b of this.list) {
      let d = 9; for (const t of tips) d = Math.min(d, t.distanceTo(b.mesh.position));
      b.mesh.scale.setScalar(d < 0.05 ? 1.08 : 1);
      if (d > 0.07) b.armed = true;
      else if (d < b.hitRadius && b.armed && b.enabled && now - b.flash > 0.6) { b.armed = false; b.flash = now; b.onPress(); }
    }
  }
}
