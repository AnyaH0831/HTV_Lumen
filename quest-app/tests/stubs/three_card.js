// A small stand-in for three.js with REAL vector maths (positions, lookAt targets, opacity) so card placement can be tested in Node.
export class Vector3 {
  constructor(x = 0, y = 0, z = 0) { this.x = x; this.y = y; this.z = z; }
  set(x, y, z) { this.x = x; this.y = y; this.z = z; return this; }
  copy(v) { return this.set(v.x, v.y, v.z); } clone() { return new Vector3(this.x, this.y, this.z); }
  add(v) { return this.set(this.x + v.x, this.y + v.y, this.z + v.z); }
  addScaledVector(v, s) { return this.set(this.x + v.x * s, this.y + v.y * s, this.z + v.z * s); }
  setScalar(s) { return this.set(s, s, s); } distanceTo(v) { return Math.hypot(this.x - v.x, this.y - v.y, this.z - v.z); }
  length() { return Math.hypot(this.x, this.y, this.z); } normalize() { const l = this.length() || 1; return this.set(this.x / l, this.y / l, this.z / l); }
  applyQuaternion(q) { const { x, y, z } = this, ix = q.w * x + q.y * z - q.z * y, iy = q.w * y + q.z * x - q.x * z, iz = q.w * z + q.x * y - q.y * x, iw = -q.x * x - q.y * y - q.z * z;
    return this.set(ix * q.w + iw * -q.x + iy * -q.z - iz * -q.y, iy * q.w + iw * -q.y + iz * -q.x - ix * -q.z, iz * q.w + iw * -q.z + ix * -q.y - iy * -q.x); }
}
export class Quaternion { constructor(x = 0, y = 0, z = 0, w = 1) { Object.assign(this, { x, y, z, w }); } }
class Obj { constructor() { this.position = new Vector3(); this.scale = new Vector3(1, 1, 1); this.visible = true; this.lookAtTarget = null; } lookAt(v) { this.lookAtTarget = v.clone(); } }
export class Mesh extends Obj { constructor(geometry, material) { super(); this.geometry = geometry; this.material = material; } }
export class MeshBasicMaterial { constructor(o = {}) { Object.assign(this, { opacity: 1 }, o); } }
export class CanvasTexture { constructor(image) { this.image = image; } }
export class PlaneGeometry { constructor(w, h) { this.w = w; this.h = h; } }
export class SphereGeometry {} export class Scene { add() {} } export class PerspectiveCamera {}
export class BufferGeometry { setAttribute() {} } export class BufferAttribute {} export class LineSegments extends Obj { constructor() { super(); this.geometry = new BufferGeometry(); } }
export class LineBasicMaterial {} export class Group extends Obj { add() {} }
export class WebGLRenderer {
  constructor() { this.xr = { enabled: false, getReferenceSpace: () => ({}), setReferenceSpaceType() {}, async setSession() {} }; this.domElement = { style: {} }; }
  setPixelRatio() {} setSize() {} setClearColor() {} setAnimationLoop() {} render() {}
}
export const SRGBColorSpace = "srgb";
