// Full 21-joint hand-skeleton overlay for capture/debug (?skeleton=1).
// Same MediaPipe topology as the 63-float hand arrays; positions arrive in the XR
// reference space, so they can be drawn directly (like the fingertip dots).
import * as THREE from "three";
import { C } from "./ui.js";

const BONES = [
  [0, 1], [1, 2], [2, 3], [3, 4],        // thumb
  [0, 5], [5, 6], [6, 7], [7, 8],        // index
  [0, 9], [9, 10], [10, 11], [11, 12],   // middle
  [0, 13], [13, 14], [14, 15], [15, 16], // ring
  [0, 17], [17, 18], [18, 19], [19, 20], // pinky
  [5, 9], [9, 13], [13, 17],             // palm
];
const NJ = 21;

export class HandSkeleton {
  constructor(color = C.ok) {
    const geo = new THREE.SphereGeometry(0.005, 10, 10);
    const mat = new THREE.MeshBasicMaterial({ color });
    this.joints = [];
    for (let i = 0; i < NJ; i++) { const m = new THREE.Mesh(geo, mat); m.visible = false; this.joints.push(m); }
    const bg = new THREE.BufferGeometry();
    bg.setAttribute("position", new THREE.BufferAttribute(new Float32Array(BONES.length * 6), 3));
    this.bones = new THREE.LineSegments(bg, new THREE.LineBasicMaterial({ color, transparent: true, opacity: 0.9 }));
    this.bones.frustumCulled = false;
    this.group = new THREE.Group();
    for (const j of this.joints) this.group.add(j);
    this.group.add(this.bones);
    this.group.visible = false;
  }

  /** h: Float32Array(63) | null, joints in XR reference space. */
  update(h) {
    this.group.visible = !!h;
    if (!h) return;
    for (let i = 0; i < NJ; i++) this.joints[i].position.set(h[i * 3], h[i * 3 + 1], h[i * 3 + 2]);
    const p = this.bones.geometry.attributes.position.array;
    for (let k = 0; k < BONES.length; k++) {
      const [a, b] = BONES[k];
      p[k * 6] = h[a * 3]; p[k * 6 + 1] = h[a * 3 + 1]; p[k * 6 + 2] = h[a * 3 + 2];
      p[k * 6 + 3] = h[b * 3]; p[k * 6 + 4] = h[b * 3 + 1]; p[k * 6 + 5] = h[b * 3 + 2];
    }
    this.bones.geometry.attributes.position.needsUpdate = true;
  }
}
