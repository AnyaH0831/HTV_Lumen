const TOKEN_KEY = "kaze_device";
const stored = key => typeof localStorage?.getItem === "function" ? localStorage.getItem(key) : null;

export function kazeSettings() {
  return { enabled: stored("lumen_kaze_enabled") === "1",
           minutes: Math.max(1, Math.min(1440, +(stored("lumen_kaze_minutes") || 15))) };
}

export class KazeClient {
  constructor() { this.token = stored(TOKEN_KEY) || ""; this.state = null; this.timer = null; this.onState = null; }
  headers() { return { "Content-Type": "application/json", Authorization: `Bearer ${this.token}` }; }
  async pairDemo() {
    const r = await fetch("/api/kaze/demo/pair", { method: "POST" });
    const data = await r.json().catch(() => ({}));
    if (!r.ok || !data.device_token) throw new Error(data.error || "Quest safety check-ins are unavailable on this server.");
    this.token = data.device_token;
    if (typeof localStorage?.setItem === "function") localStorage.setItem(TOKEN_KEY, this.token);
  }
  async call(path, method = "GET", body) {
    if (!this.token) throw new Error("Enable “Start safety check-ins with Lumen” on the launch screen, then restart live mode.");
    const r = await fetch(`/api/kaze/device${path}`, { method, headers: this.headers(), ...(body ? { body: JSON.stringify(body) } : {}) });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || `KAZE request failed (${r.status})`);
    return data;
  }
  async refresh() {
    try { this.state = await this.call("/state"); this.onState?.(this.state); return this.state; }
    catch (e) { this.onState?.({ state: "error", error: e.message }); return null; }
  }
  async start(minutes) {
    if (!this.token) await this.pairDemo();
    let profile;
    try { profile = await this.call("/profile"); }
    catch (e) { this.token = ""; await this.pairDemo(); profile = await this.call("/profile"); }
    if (!profile || !profile.name) throw new Error("KAZE device pairing was not accepted.");
    const existing = await this.call("/state");
    if (existing.state === "alert_sent") await this.call("/watch/stop", "POST", { pin: "1234" });
    await this.call("/watch/start", "POST", { config: { checkin_interval_s: Math.round(minutes * 60), response_window_s: 60 } });
    await this.refresh();
    this.timer = setInterval(() => this.refresh(), 1000);
  }
  async checkin() { const out = await this.call("/checkin", "POST", { method: "tap" }); await this.refresh(); return out; }
  stop() { if (this.timer) clearInterval(this.timer); this.timer = null; }
}
