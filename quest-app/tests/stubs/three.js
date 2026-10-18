// Minimal stand-in for three.js so the headset UI code can run under Node. Objects accept any call; no rendering happens.
const make = () => new Proxy(function () {}, { get: (t, k) => (k === Symbol.toPrimitive ? () => 0 : k in t ? t[k] : make()), set: (t, k, v) => { t[k] = v; return true; }, apply: () => make(), construct: () => make() });
const cls = () => class { constructor() { return make(); } };
export const CanvasTexture = cls(), Mesh = cls(), MeshBasicMaterial = cls(), PerspectiveCamera = cls(), PlaneGeometry = cls(),
  Quaternion = cls(), Scene = cls(), SphereGeometry = cls(), Vector3 = cls(), WebGLRenderer = cls(),
  BufferGeometry = cls(), BufferAttribute = cls(), LineSegments = cls(), LineBasicMaterial = cls(), Group = cls();
export const SRGBColorSpace = "srgb";
