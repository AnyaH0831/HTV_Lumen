// Streaming sign segmenter. Feed it frames; it returns a finished clip when a sign ends.
// A sign = BOTH hands raised into signing space (above hip level, relative to the head) with motion;
// detection only starts once both hands are visible, and ends when a hand drops / disappears, or the
// hands stay still. All thresholds are in metres, seconds, m/s.
export const DEFAULTS = { raiseBelowHead: 0.55, startHold: 0.12, endAbsent: 0.25, stillSpeed: 0.05, stillHold: 0.5,
  minDur: 0.35, maxDur: 3.0, preRoll: 0.15, speedEma: 0.4 };

export class Segmenter {
  constructor(opts = {}) { this.o = { ...DEFAULTS, ...opts }; this.reset(); }
  reset() { this.ring = []; this.state = "idle"; this.cand = null; this.frames = []; this.prev = null; this.speed = 0;
            this.lastActive = 0; this.lastMoving = 0; this.stillSince = null; }

  _active(f) {
    if (!f.left || !f.right) return false;      // detection only starts once BOTH hands are visible
    return Math.max(f.left[1], f.right[1]) > f.head[1] - this.o.raiseBelowHead;
  }
  _speed(f) {                                                        // mean wrist speed of present hands, EMA-smoothed
    if (!this.prev) return 0;
    const dt = f.t - this.prev.t; if (dt <= 0) return this.speed;
    let s = 0, n = 0;
    for (const k of ["left", "right"]) if (f[k] && this.prev[k]) {
      s += Math.hypot(f[k][0] - this.prev[k][0], f[k][1] - this.prev[k][1], f[k][2] - this.prev[k][2]) / dt; n++;
    }
    return n ? (this.speed = this.o.speedEma * (s / n) + (1 - this.o.speedEma) * this.speed) : this.speed;
  }

  /** f = { t, left: Float32Array(63)|null, right: ..., head:[x,y,z] } -> clip | null */
  push(f) {
    if (this.prev && f.t <= this.prev.t) return null;                // ignore non-monotonic timestamps
    const o = this.o, active = this._active(f), v = this._speed(f); this.prev = f;
    this.ring.push(f); while (this.ring.length && f.t - this.ring[0].t > 1.0) this.ring.shift();

    if (this.state === "idle") {
      if (!active) { this.cand = null; return null; }
      if (this.cand === null) this.cand = f.t;
      if (f.t - this.cand >= o.startHold) {
        this.state = "active"; this.frames = this.ring.filter(r => r.t >= this.cand - o.preRoll);
        this.lastActive = this.lastMoving = f.t; this.stillSince = null;
      }
      return null;
    }
    this.frames.push(f);
    if (active) this.lastActive = f.t;
    if (v > o.stillSpeed) { this.lastMoving = f.t; this.stillSince = null; } else if (this.stillSince === null) this.stillSince = f.t;
    const dur = f.t - this.frames[0].t;
    const ended = (!active && f.t - this.lastActive >= o.endAbsent) || (this.stillSince !== null && f.t - this.stillSince >= o.stillHold) || dur >= o.maxDur;
    if (!ended) return null;
    const tEnd = Math.min(f.t, Math.max(this.lastMoving, this.lastActive - o.endAbsent) + 0.15);
    const fr = this.frames.filter(r => r.t <= tEnd);
    this.state = "idle"; this.cand = null; this.frames = []; this.stillSince = null;
    if (fr.length < 6 || fr[fr.length - 1].t - fr[0].t < o.minDur) return null;
    return { t: fr.map(r => r.t), left: fr.map(r => r.left), right: fr.map(r => r.right), head: fr.map(r => r.head) };
  }
}
