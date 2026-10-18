export class Net {
  constructor(role) { this.role = role; this.h = {}; this.q = []; this.audio = null; this.connect(); }
  connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    this.ws = new WebSocket(`${proto}://${location.host}/ws?role=${this.role}`);
    this.ws.onopen = () => { this.up = true; this.q.splice(0).forEach(m => this.ws.send(m)); this.h.status?.("up"); };
    this.ws.onclose = () => { this.up = false; this.h.status?.("down"); setTimeout(() => this.connect(), 1500); };
    this.ws.onmessage = e => { const m = JSON.parse(e.data); this.h[m.type]?.(m); };
  }
  on(type, fn) { this.h[type] = fn; }
  send(o) { const s = JSON.stringify(o); this.up ? this.ws.send(s) : this.q.push(s); }
  async startDoctorAudio() {
    if (this.audio || !navigator.mediaDevices?.getUserMedia) return false;
    const stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: false, noiseSuppression: false, autoGainControl: true } });
    const AudioCtx = globalThis.AudioContext || globalThis.webkitAudioContext;
    if (!AudioCtx) { stream.getTracks().forEach(t => t.stop()); return false; }
    const ctx = new AudioCtx(), src = ctx.createMediaStreamSource(stream), node = ctx.createScriptProcessor(4096, 1, 1);
    const ratio = ctx.sampleRate / 16000;
    node.onaudioprocess = e => {
      if (!this.up) return;
      const input = e.inputBuffer.getChannelData(0), n = Math.floor(input.length / ratio), pcm = new Int16Array(n);
      for (let i = 0; i < n; i++) { const x = Math.max(-1, Math.min(1, input[Math.floor(i * ratio)] * 1.6)); pcm[i] = x < 0 ? x * 32768 : x * 32767; }
      const bytes = new Uint8Array(pcm.buffer); let raw = "";
      for (let i = 0; i < bytes.length; i++) raw += String.fromCharCode(bytes[i]);
      this.send({ type: "doctor_audio", pcm: btoa(raw) });
    };
    src.connect(node); node.connect(ctx.destination);
    this.audio = { stream, ctx, src, node };
    return true;
  }
  stopDoctorAudio() {
    const a = this.audio; if (!a) return;
    try { a.node.disconnect(); a.src.disconnect(); a.ctx.close(); a.stream.getTracks().forEach(t => t.stop()); } catch (_) {}
    this.audio = null;
  }
  async saveRecording(rec) {
    const r = await fetch("/api/recordings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(rec) });
    return r.ok;
  }
}
