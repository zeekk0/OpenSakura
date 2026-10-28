// avatar.js — three.js stage: VRM character driven by Sakura's emotions + humanoid motion clips.
//
// Clips (GLB, or FBX) are retargeted onto the VRM at load time: every source bone's world
// rotation is taken relative to the source rest pose, corrected for the rest-pose difference
// (T-pose vs A-pose, bone roll) and written to the VRM's normalized humanoid bones. The source
// skeleton is recognised by bone names (see RIGS); any VRM 1.0/0.x with standard humanoid bones works.
import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { VRMLoaderPlugin, VRMUtils } from '@pixiv/three-vrm';
import { Environment } from './environment.js';

// ---------- source rigs ----------
// A rig maps normalised source bone names -> [VRM humanoid bone, source child for the rest direction,
// VRM child for the rest direction]. `norm` turns a loaded node name into a map key (loaders strip
// '.', ':' and '/' from names). `hips`/`head`/`thighs` frame the skeleton, `leg` (hip, knee, ankle)
// sets the unit scale. The first rig whose frame bones all exist wins; clips.json can force one ("rig").
const FINGERS = ['Thumb', 'Index', 'Middle', 'Ring', 'Little'];
const fingerSegs = (f) => (f === 'Thumb' ? ['Metacarpal', 'Proximal', 'Distal'] : ['Proximal', 'Intermediate', 'Distal']);
const SIDES = [['l', 'left'], ['r', 'right']];
// rows: [source key, VRM bone, source child, VRM child]. In source keys '<s>' is l|r and '<side>' is
// left|right; a VRM name starting with '<Side>' gets the side prefix (leftUpperArm).
// fingerKey(s, side, finger, 1..3) names the source finger joints (null: the rig has no fingers).
function rigMap(rows, fingerKey) {
  const map = {};
  for (const [src, vrm, sc, vc] of rows) {
    for (const [s, side] of (src.includes('<') ? SIDES : [['', '']])) {
      const k = (x) => x && x.replace('<s>', s).replace('<side>', side);
      const v = (x) => x && (x.startsWith('<Side>') ? side + x.slice(6) : x);
      map[k(src)] = [v(vrm), k(sc), v(vc)];
    }
  }
  if (fingerKey) for (const [s, side] of SIDES) for (const f of FINGERS) {
    const segs = fingerSegs(f);
    for (let i = 0; i < 3; i++) {
      const next = i < 2 ? fingerKey(s, side, f, i + 2) : null;
      map[fingerKey(s, side, f, i + 1)] = [`${side}${f}${segs[i]}`, next, next ? `${side}${f}${segs[i + 1]}` : null];
    }
  }
  return map;
}
// UE5-mannequin names: the Quaternius Universal Animation Library clips shipped here, Unreal exports.
const UE5 = rigMap([
  ['pelvis', 'hips', 'spine_01', 'spine'],
  ['spine_01', 'spine', 'spine_02', 'chest'],
  ['spine_02', 'chest', 'spine_03', 'upperChest'],
  ['spine_03', 'upperChest', 'neck_01', 'neck'],
  ['neck_01', 'neck', 'head', 'head'],
  ['head', 'head', null, null],
  ['clavicle_<s>', '<Side>Shoulder', 'upperarm_<s>', '<Side>UpperArm'],
  ['upperarm_<s>', '<Side>UpperArm', 'lowerarm_<s>', '<Side>LowerArm'],
  ['lowerarm_<s>', '<Side>LowerArm', 'hand_<s>', '<Side>Hand'],
  ['hand_<s>', '<Side>Hand', 'middle_01_<s>', '<Side>MiddleProximal'],
  ['thigh_<s>', '<Side>UpperLeg', 'calf_<s>', '<Side>LowerLeg'],
  ['calf_<s>', '<Side>LowerLeg', 'foot_<s>', '<Side>Foot'],
  ['foot_<s>', '<Side>Foot', 'ball_<s>', '<Side>Toes'],
  ['ball_<s>', '<Side>Toes', null, null],
], (s, side, f, i) => `${f === 'Little' ? 'pinky' : f.toLowerCase()}_0${i}_${s}`);
// Mixamo names ("mixamorig:LeftArm" etc.).
const MIXAMO = rigMap([
  ['hips', 'hips', 'spine', 'spine'],
  ['spine', 'spine', 'spine1', 'chest'],
  ['spine1', 'chest', 'spine2', 'upperChest'],
  ['spine2', 'upperChest', 'neck', 'neck'],
  ['neck', 'neck', 'head', 'head'],
  ['head', 'head', null, null],
  ['<side>shoulder', '<Side>Shoulder', '<side>arm', '<Side>UpperArm'],
  ['<side>arm', '<Side>UpperArm', '<side>forearm', '<Side>LowerArm'],
  ['<side>forearm', '<Side>LowerArm', '<side>hand', '<Side>Hand'],
  ['<side>hand', '<Side>Hand', '<side>handmiddle1', '<Side>MiddleProximal'],
  ['<side>upleg', '<Side>UpperLeg', '<side>leg', '<Side>LowerLeg'],
  ['<side>leg', '<Side>LowerLeg', '<side>foot', '<Side>Foot'],
  ['<side>foot', '<Side>Foot', '<side>toebase', '<Side>Toes'],
  ['<side>toebase', '<Side>Toes', null, null],
], (s, side, f, i) => `${side}hand${f === 'Little' ? 'pinky' : f.toLowerCase()}${i}`);
// KayKit character rigs (upperarm.l, wrist.l, ...): no clavicles, neck or finger joints.
const KAYKIT = rigMap([
  ['hips', 'hips', 'spine', 'spine'],
  ['spine', 'spine', 'chest', 'chest'],
  ['chest', 'chest', 'head', 'upperChest'],
  ['head', 'head', null, null],
  ['upperarm<s>', '<Side>UpperArm', 'lowerarm<s>', '<Side>LowerArm'],
  ['lowerarm<s>', '<Side>LowerArm', 'wrist<s>', '<Side>Hand'],
  ['wrist<s>', '<Side>Hand', 'hand<s>', '<Side>MiddleProximal'],
  ['upperleg<s>', '<Side>UpperLeg', 'lowerleg<s>', '<Side>LowerLeg'],
  ['lowerleg<s>', '<Side>LowerLeg', 'foot<s>', '<Side>Foot'],
  ['foot<s>', '<Side>Foot', 'toes<s>', '<Side>Toes'],
  ['toes<s>', '<Side>Toes', null, null],
], null);
const RIGS = [
  { id: 'ue5', map: UE5, norm: (n) => n.toLowerCase(),
    hips: 'pelvis', head: 'head', thighs: ['thigh_l', 'thigh_r'], leg: ['thigh_l', 'calf_l', 'foot_l'] },
  { id: 'mixamo', map: MIXAMO, norm: (n) => n.replace(/^mixamorig\d*[:_]?/i, '').toLowerCase(),
    hips: 'hips', head: 'head', thighs: ['leftupleg', 'rightupleg'], leg: ['leftupleg', 'leftleg', 'leftfoot'] },
  { id: 'kaykit', map: KAYKIT, norm: (n) => n.replace(/[.:\s]/g, '').toLowerCase(),
    hips: 'hips', head: 'head', thighs: ['upperlegl', 'upperlegr'], leg: ['upperlegl', 'lowerlegl', 'footl'] },
];
// VRM humanoid hierarchy (normalized rig), used to find the nearest driven ancestor of a bone.
const VRM_PARENT = {
  hips: null, spine: 'hips', chest: 'spine', upperChest: 'chest', neck: 'upperChest', head: 'neck',
  leftShoulder: 'upperChest', leftUpperArm: 'leftShoulder', leftLowerArm: 'leftUpperArm', leftHand: 'leftLowerArm',
  rightShoulder: 'upperChest', rightUpperArm: 'rightShoulder', rightLowerArm: 'rightUpperArm', rightHand: 'rightLowerArm',
  leftUpperLeg: 'hips', leftLowerLeg: 'leftUpperLeg', leftFoot: 'leftLowerLeg', leftToes: 'leftFoot',
  rightUpperLeg: 'hips', rightLowerLeg: 'rightUpperLeg', rightFoot: 'rightLowerLeg', rightToes: 'rightFoot',
};
for (const S of ['left', 'right']) for (const f of FINGERS) {
  const segs = fingerSegs(f);
  segs.forEach((seg, i) => { VRM_PARENT[`${S}${f}${seg}`] = i === 0 ? `${S}Hand` : `${S}${f}${segs[i - 1]}`; });
}
// Torso and shoulders keep the VRM's own modelled posture (source motion is applied on top of it);
// limbs take the source's rest directions (T-pose vs A-pose arms, leg splay).
const KEEP_POSTURE = new Set(['hips', 'spine', 'chest', 'upperChest', 'neck', 'head', 'leftShoulder', 'rightShoulder']);
const XZ_RANGE = 0.3;     // in-place sway the hips may keep (m); anything beyond is clamped so she never walks away

// Sakura emotion -> VRM preset expression weights (scaled by intensity)
// VRoid's `happy` and `surprised` presets open the mouth, so smiles lean on `relaxed`
// (closed-mouth smile) and `happy` stays ≤ 0.45; the mouth is the lipsync's job.
const EMOTION_EXPR = {
  neutral: {}, happy: { happy: 0.45, relaxed: 0.75 }, excited: { happy: 0.5, relaxed: 0.6, surprised: 0.3 },
  playful: { happy: 0.4, relaxed: 0.7 }, mischievous: { happy: 0.35, relaxed: 0.5 }, content: { relaxed: 0.9 },
  relieved: { relaxed: 0.8 }, caring: { relaxed: 0.7, happy: 0.2 }, proud: { happy: 0.3, relaxed: 0.5 },
  shy: { relaxed: 0.5, happy: 0.25 }, embarrassed: { relaxed: 0.3, sad: 0.2 }, sad: { sad: 1 }, lonely: { sad: 0.8 },
  disappointed: { sad: 0.6 }, worried: { sad: 0.5, surprised: 0.15 }, fear: { surprised: 0.5, sad: 0.4 },
  angry: { angry: 1 }, annoyed: { angry: 0.6 }, frustrated: { angry: 0.7 }, disgust: { angry: 0.5, sad: 0.2 },
  jealous: { angry: 0.45, sad: 0.3 }, tsun: { angry: 0.4, relaxed: 0.25 }, cold: {}, distant: {}, sarcastic: { relaxed: 0.3, angry: 0.2 },
  surprise: { surprised: 0.7 }, protective: { angry: 0.2 },
};
const MOUTH_OPENING_PRESETS = { happy: 0.45, surprised: 0.6 };   // hard caps, speaking or not
const EXPR_NAMES = ['happy', 'angry', 'sad', 'relaxed', 'surprised'];

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
// hand the main thread back for a moment (a message task: not throttled like timers in background tabs)
const yieldTask = () => new Promise((r) => { const c = new MessageChannel(); c.port1.onmessage = () => r(); c.port2.postMessage(0); });

// Light graphics mode: no antialiasing, pixel ratio 1, no shadows, no depth-of-field pass.
export function isLite() {
  try {
    const q = new URLSearchParams(location.search).get('lite');
    if (q !== null) return q !== '0';
    return localStorage.getItem('sakura.lite') === '1';
  } catch { return false; }
}

export class Avatar {
  constructor(canvas, opts = {}) {
    this.canvas = canvas;
    this.opts = opts;
    this.onStatus = opts.onStatus || (() => {});
    this.onProgress = opts.onProgress || (() => {});   // (stage, fraction 0..1) — drives the loading screen
    this.clipsBase = opts.clipsBase || 'assets/clips/';
    this.lite = isLite();
    this.renderer = new THREE.WebGLRenderer({ canvas, antialias: !this.lite, alpha: true,
                                              powerPreference: this.lite ? 'low-power' : 'default' });
    this.renderer.setPixelRatio(this.lite ? 1 : Math.min(window.devicePixelRatio, 2));
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(28, 1, 0.1, 50);
    this.camera.position.set(0.0, 1.5, 2.9);
    this.controls = new OrbitControls(this.camera, canvas);
    this.controls.target.set(0, 1.4, 0);            // refined to her actual head once the VRM is loaded
    this._headTmp = new THREE.Vector3();
    this._tmpA = new THREE.Vector3(); this._tmpB = new THREE.Vector3(); this._offTmp = new THREE.Vector3();
    // right-drag up/down slides the focus point along her body: 1 = head, 0 = shoes (no sideways pan)
    this.focusT = 1;
    this._focusDrag = null;
    this.controls.mouseButtons = { LEFT: THREE.MOUSE.ROTATE, MIDDLE: THREE.MOUSE.DOLLY, RIGHT: null };
    canvas.addEventListener('contextmenu', (e) => e.preventDefault());
    canvas.addEventListener('pointerdown', (e) => {
      if (e.button !== 2) return;
      this._focusDrag = { y: e.clientY, t: this.focusT };
      try { canvas.setPointerCapture(e.pointerId); } catch (err) { /* not supported */ }
      e.preventDefault();
    });
    canvas.addEventListener('pointermove', (e) => {
      if (!this._focusDrag) return;
      const travel = Math.max(220, canvas.clientHeight * 0.45);      // px of drag for the full head→shoes run
      this.focusT = Math.max(0, Math.min(1, this._focusDrag.t - (e.clientY - this._focusDrag.y) / travel));
    });
    const endFocusDrag = (e) => { if (this._focusDrag && (e.button === 2 || e.type === 'pointercancel')) this._focusDrag = null; };
    canvas.addEventListener('pointerup', endFocusDrag);
    canvas.addEventListener('pointercancel', endFocusDrag);
    this.controls.enablePan = false;
    this.controls.minDistance = 0.8;
    this.controls.maxDistance = 9;
    this.controls.update();
    this.scene.add(new THREE.HemisphereLight(0xffffff, 0x5a4a6a, 1.35));
    const dir = new THREE.DirectionalLight(0xffffff, 1.7);
    dir.position.set(1.2, 2.4, 2.2);
    this.scene.add(dir);
    const ring = new THREE.Mesh(new THREE.RingGeometry(0.42, 0.5, 48),
      new THREE.MeshBasicMaterial({ color: 0xff8fc2, transparent: true, opacity: 0.25, side: THREE.DoubleSide }));
    ring.rotation.x = -Math.PI / 2; ring.position.y = 0.005;
    this.scene.add(ring);
    this._addGodRay();
    try { this.env = new Environment(this.scene, this.renderer, this.camera, this.lite ? { dof: false, shadows: false } : {}); this.env.focusTarget = this.controls.target; }
    catch (e) { console.warn('[avatar] environment failed to build:', e); this.env = null; }

    this.clock = new THREE.Clock();
    this.vrm = null;
    this.mixer = null;
    this.current = null;
    this.clipCache = new Map();
    this._srcCache = new Map();
    this.manifest = null;
    this.idleName = 'breathing';
    this._chainId = 0;
    this._mood = {};
    this._pulse = {};
    this._pulseAge = 99;
    this._manual = null;
    this.speaking = false;
    this._exprNow = {};
    this._mouth = 0;
    this._mouthTarget = 0;
    this._blinkT = 2 + Math.random() * 3;
    this._blinkV = 0;
    this._resize();
    window.addEventListener('resize', () => this._resize());
    this.renderer.setAnimationLoop(() => this._tick());
    if (new URLSearchParams(location.search).has('debug')) window.__avatar = this;   // console / test access
  }

  /** Volumetric "god ray": a spotlight + two additive light cones with a slow shimmer + a floor glow. */
  _addGodRay() {
    const spot = new THREE.SpotLight(0xffe6f2, 7, 8, 0.42, 0.9, 1.1);
    spot.position.set(0.35, 4.3, 0.9);
    spot.target.position.set(0, 1.0, 0);
    this.scene.add(spot, spot.target);

    this._rayUniforms = {
      uTime: { value: 0 },
      uColor: { value: new THREE.Color(0xffc6e3) },
      uIntensity: { value: 0.5 },
    };
    const rayMat = new THREE.ShaderMaterial({
      uniforms: this._rayUniforms,
      transparent: true, depthWrite: false, blending: THREE.AdditiveBlending, side: THREE.DoubleSide,
      vertexShader: `
        varying vec2 vUv; varying vec3 vN; varying vec3 vV;
        void main() {
          vUv = uv;
          vec4 wp = modelMatrix * vec4(position, 1.0);
          vN = normalize(mat3(modelMatrix) * normal);
          vV = normalize(cameraPosition - wp.xyz);
          gl_Position = projectionMatrix * viewMatrix * wp;
        }`,
      fragmentShader: `
        uniform float uTime; uniform vec3 uColor; uniform float uIntensity;
        varying vec2 vUv; varying vec3 vN; varying vec3 vV;
        void main() {
          float h = vUv.y;                                   // 1 = top of the cone
          float vert = smoothstep(0.0, 0.45, h) * (0.25 + 0.75 * h);
          float facing = pow(abs(dot(normalize(vN), normalize(vV))), 1.4);   // soft silhouette
          float a1 = vUv.x * 6.2831853;
          float rays = 0.72 + 0.18 * sin(a1 * 7.0 + uTime * 0.7) * sin(a1 * 3.0 - uTime * 0.45)
                            + 0.10 * sin(a1 * 13.0 - uTime * 1.1);
          float a = vert * facing * rays * uIntensity;
          gl_FragColor = vec4(uColor * a, a);
        }`,
    });
    const geo = new THREE.CylinderGeometry(0.14, 1.05, 3.9, 72, 1, true);
    const cone = new THREE.Mesh(geo, rayMat);
    cone.position.set(0.12, 2.15, 0.3);
    cone.rotation.z = -0.06; cone.rotation.x = 0.1;
    cone.renderOrder = 5; cone.frustumCulled = false;
    const cone2 = cone.clone();
    cone2.scale.set(0.62, 1.03, 0.62); cone2.rotation.y = 1.1;
    this.scene.add(cone, cone2);
    this._rays = [cone, cone2];

    const glowMat = new THREE.ShaderMaterial({
      transparent: true, depthWrite: false, blending: THREE.AdditiveBlending,
      uniforms: { uColor: { value: new THREE.Color(0xff9ccb) } },
      vertexShader: `varying vec2 vUv; void main(){ vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position,1.0); }`,
      fragmentShader: `uniform vec3 uColor; varying vec2 vUv;
        void main(){ float d = distance(vUv, vec2(0.5)); float a = smoothstep(0.5, 0.05, d) * 0.45; gl_FragColor = vec4(uColor * a, a); }`,
    });
    const glow = new THREE.Mesh(new THREE.CircleGeometry(1.15, 64), glowMat);
    glow.rotation.x = -Math.PI / 2; glow.position.y = 0.008; glow.renderOrder = 1;
    this.scene.add(glow);
  }

  _resize() {
    const w = this.canvas.clientWidth || window.innerWidth, h = this.canvas.clientHeight || window.innerHeight;
    this.renderer.setSize(w, h, false);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    this.env?.setSize(w, h);
  }

  // ---------- loading ----------
  async load(vrmUrl = 'assets/avatar.vrm', manifestUrl = 'assets/clips.json') {
    this.onStatus('loading avatar…');
    this.onProgress('avatar', 0);
    const loader = new GLTFLoader();
    loader.register((parser) => new VRMLoaderPlugin(parser));
    const gltf = await loader.loadAsync(vrmUrl, (e) => { if (e.total) this.onProgress('avatar', e.loaded / e.total); });
    this.onProgress('avatar', 1);
    const vrm = gltf.userData.vrm;
    VRMUtils.removeUnnecessaryVertices(gltf.scene);
    VRMUtils.combineSkeletons(gltf.scene);
    if (vrm.meta?.metaVersion === '0') VRMUtils.rotateVRM0(vrm);
    vrm.scene.traverse((o) => { o.frustumCulled = false; });
    if (this.vrm) { this.scene.remove(this.vrm.scene); VRMUtils.deepDispose(this.vrm.scene); }
    this.vrm = vrm;
    this.scene.add(vrm.scene);
    vrm.scene.updateMatrixWorld(true);
    if (this._focusPoint(this._headTmp)) this.controls.target.copy(this._headTmp);   // orbit + focus centre = her head
    if (vrm.lookAt) { vrm.lookAt.target = this.camera; vrm.lookAt.autoUpdate = true; }
    this.mixer = new THREE.AnimationMixer(vrm.scene);
    this.clipCache.clear();
    this._srcCache.clear();
    this._restInfo = this._computeVrmRest();
    try {
      this.manifest = await (await fetch(manifestUrl, { cache: 'no-cache' })).json();
      this.idleName = this.manifest.idle || 'breathing';
    } catch (e) { this.manifest = { clips: [] }; }
    this.onStatus(`avatar ready (${vrm.meta?.metaVersion === '0' ? 'VRM 0.x' : 'VRM 1.0'} · ${this.manifest.clips.length} clips)`);
    this.onProgress('motion', 0);
    await this.getClip(this.idleName, (f) => this.onProgress('motion', f * 0.8));   // download = 80 %, bake = the rest
    this.onProgress('motion', 1);
    this.idle();
    this._prefetch();
    return vrm;
  }

  /** Download + parse the remaining clip files in the background (small GLBs), so an action's first
   *  play only waits for its bake, not the network. Baking itself stays lazy (it costs main-thread time). */
  async _prefetch() {
    const files = [...new Set((this.manifest?.clips || []).map((c) => c.file))];
    for (const f of files) {
      if (this._srcCache.has(f)) continue;
      try { await this._loadSource(f); } catch (e) { /* reported when the clip is actually played */ }
      await sleep(30);
    }
  }

  /** Names offered to the tester and the action LLM (manifest entries marked "hidden" are left out). */
  get clipNames() { return (this.manifest?.clips || []).filter((c) => !c.hidden).map((c) => c.name); }

  _vrmNode(name) { return this.vrm?.humanoid?.getNormalizedBoneNode(name) || null; }
  _vrmRaw(name) { return this.vrm?.humanoid?.getRawBoneNode(name) || null; }

  /** VRM rest pose, captured once right after loading (before any clip has moved her). */
  _computeVrmRest() {
    const pos = {};
    const v = new THREE.Vector3();
    for (const vrmName of Object.keys(VRM_PARENT)) {
      const n = this._vrmRaw(vrmName);
      if (n) { n.getWorldPosition(v); pos[vrmName] = v.clone(); }
    }
    return { pos, hipsLocal: this._vrmNode('hips').position.clone() };
  }

  // nearest VRM ancestor that exists in this model and is driven by the clip
  _drivenAncestor(vrmName, driven) {
    let p = VRM_PARENT[vrmName];
    while (p && !(driven.has(p))) p = VRM_PARENT[p];
    return p || null;
  }

  /** Download + parse one clip file (GLB/glTF via GLTFLoader, FBX via a lazily imported FBXLoader).
   *  Cached per file, so several manifest names can share one file; the rest pose is remembered so
   *  each bake starts from it. */
  _loadSource(file, onProgress) {
    if (this._srcCache.has(file)) return this._srcCache.get(file);
    const url = this.clipsBase + encodeURIComponent(file);
    const prog = (e) => { if (onProgress && e.total) onProgress(e.loaded / e.total); };
    const pending = (async () => {
      let root, clips, format;
      if (/\.fbx$/i.test(file)) {
        const { FBXLoader } = await import('three/addons/loaders/FBXLoader.js');
        root = await new FBXLoader().loadAsync(url, prog);
        clips = root.animations || []; format = 'fbx';
      } else {
        const gltf = await new GLTFLoader().loadAsync(url, prog);
        root = gltf.scene; clips = gltf.animations || []; format = 'glb';
      }
      const rest = [];
      root.traverse((o) => rest.push([o, o.position.clone(), o.quaternion.clone(), o.scale.clone()]));
      return { root, clips, format, rest };
    })();
    this._srcCache.set(file, pending);
    pending.catch(() => this._srcCache.delete(file));
    return pending;
  }

  /** Which RIGS entry describes this skeleton (by bone names). */
  _detectRig(root, forced) {
    const names = new Set();
    root.traverse((o) => { if (o.name) names.add(o.name); });
    const has = (rig, key) => [...names].some((n) => rig.norm(n) === key);
    for (const rig of RIGS) {
      if (forced && rig.id !== forced) continue;
      if ([rig.hips, rig.head, ...rig.thighs].every((k) => has(rig, k))) return rig;
    }
    const sample = [...names].slice(0, 8).join(', ');
    throw new Error(`unknown skeleton (bones: ${sample}…) — add a rig to RIGS in avatar.js`);
  }

  /** Retarget one source clip onto this VRM -> AnimationClip on the VRM's normalized bones. Runs in slices
   *  of a few ms so the avatar keeps moving while a clip bakes on its first play; bakes are queued one at a
   *  time because clips that share a file share its skeleton (see _queueBake). */
  async _bake(src, srcClip, { rig: forcedRig, speed = 1, lockXZ = false } = {}) {
    const vrm = this.vrm;
    const isVRM0 = vrm.meta?.metaVersion === '0';
    const srcRoot = src.root;
    for (const [o, p0, q0, s0] of src.rest) { o.position.copy(p0); o.quaternion.copy(q0); o.scale.copy(s0); }
    const rig = this._detectRig(srcRoot, forcedRig);
    const MAP = rig.map;
    // Wrap the source so its whole world frame can be rotated (Z-up FBX, Blender's -90° X root, ...).
    const wrapper = new THREE.Group();
    wrapper.add(srcRoot);
    wrapper.updateMatrixWorld(true);
    const bones = {};
    srcRoot.traverse((o) => { const k = rig.norm(o.name || ''); if (MAP[k] && !bones[k]) bones[k] = o; });
    // nearest mapped ancestor of every mapped bone, from the loaded hierarchy (no per-rig parent table)
    const srcParent = {};
    for (const [k, o] of Object.entries(bones)) {
      let p = o.parent;
      while (p && p !== wrapper && bones[rig.norm(p.name || '')] !== p) p = p.parent;
      srcParent[k] = p && p !== wrapper ? rig.norm(p.name) : null;
    }
    // Frame alignment from the skeleton itself: up = hips→head, right = left thigh→right thigh.
    // Build the same basis on the VRM rest pose and rotate the source frame onto it.
    const basisQuat = (P, hips, head, thighL, thighR) => {
      const up = P[head].clone().sub(P[hips]).normalize();
      const right = P[thighR].clone().sub(P[thighL]).normalize();
      const fwd = new THREE.Vector3().crossVectors(right, up).normalize();
      right.crossVectors(up, fwd).normalize();
      return new THREE.Quaternion().setFromRotationMatrix(new THREE.Matrix4().makeBasis(right, up, fwd));
    };
    const frameKeys = [rig.hips, rig.head, ...rig.thighs];
    const P0 = {};
    for (const n of frameKeys) P0[n] = bones[n].getWorldPosition(new THREE.Vector3());
    const qF = basisQuat(P0, ...frameKeys);
    const qV = basisQuat(this._restInfo.pos, 'hips', 'head', 'leftUpperLeg', 'rightUpperLeg');
    // Snap to the nearest axis-aligned rotation: rigs differ by up-axis / facing conventions (multiples
    // of 90°); a few degrees on top of that is only spine posture, not a different frame.
    const m = new THREE.Matrix4().makeRotationFromQuaternion(qV.clone().multiply(qF.clone().invert()));
    const ax = [new THREE.Vector3(), new THREE.Vector3(), new THREE.Vector3()];
    m.extractBasis(ax[0], ax[1], ax[2]);
    const snap = (v, not) => {
      const c = [v.x, v.y, v.z].map((x, i) => (i === not ? 0 : x));
      const i = c.reduce((b, x, j) => (Math.abs(x) > Math.abs(c[b]) ? j : b), 0);
      return [new THREE.Vector3().setComponent(i, Math.sign(c[i]) || 1), i];
    };
    const [sx, ix] = snap(ax[0], -1), [sy] = snap(ax[1], ix);
    const sz = new THREE.Vector3().crossVectors(sx, sy);
    wrapper.quaternion.setFromRotationMatrix(new THREE.Matrix4().makeBasis(sx, sy, sz));
    wrapper.updateMatrixWorld(true);
    const frameEuler = new THREE.Euler().setFromQuaternion(wrapper.quaternion);

    // rest pose capture (the source is loaded in its rest / bind pose), now in the VRM's frame
    const restQ = {}, restP = {};
    const q = new THREE.Quaternion(), p = new THREE.Vector3();
    for (const [name, b] of Object.entries(bones)) {
      b.getWorldQuaternion(q); restQ[name] = q.clone();
      b.getWorldPosition(p); restP[name] = p.clone();
    }
    // per-bone correction: rotate VRM rest direction onto the source rest direction
    const corr = {};
    const dF = new THREE.Vector3(), dV = new THREE.Vector3();
    const computeCorr = (name) => {
      if (name in corr) return corr[name];
      const [vrmName, fChild, vChild] = MAP[name];
      if (KEEP_POSTURE.has(vrmName)) { corr[name] = new THREE.Quaternion(); return corr[name]; }
      let c = null;
      if (fChild && vChild && bones[fChild] && restP[name] && this._restInfo.pos[vrmName] && this._restInfo.pos[vChild]) {
        bones[fChild].getWorldPosition(dF); dF.sub(restP[name]);
        dV.copy(this._restInfo.pos[vChild]).sub(this._restInfo.pos[vrmName]);
        if (dF.lengthSq() > 1e-8 && dV.lengthSq() > 1e-8) c = new THREE.Quaternion().setFromUnitVectors(dV.normalize(), dF.normalize());
      }
      if (!c) { const par = srcParent[name]; c = par ? computeCorr(par).clone() : new THREE.Quaternion(); }
      corr[name] = c;
      return c;
    };
    const driven = new Set();
    const pairs = [];
    for (const name of Object.keys(bones)) {
      const vrmName = MAP[name][0];
      if (this._vrmNode(vrmName) && !driven.has(vrmName)) { driven.add(vrmName); pairs.push([name, vrmName]); computeCorr(name); }
    }
    // diagnostics: rest arm angle (90° = T-pose, ~45° = A-pose) and hips height of the source
    const keyOf = (vrmName) => Object.keys(MAP).find((k) => MAP[k][0] === vrmName);
    const ua = keyOf('leftUpperArm'), hd = keyOf('leftHand');
    let restArmDeg = null;
    if (restP[ua] && restP[hd]) {
      const d = restP[hd].clone().sub(restP[ua]).normalize();
      restArmDeg = Math.round(THREE.MathUtils.radToDeg(Math.acos(Math.max(-1, Math.min(1, -d.y)))));
    }
    const fps = 30;
    // enough samples to reach the clip's end (t is clamped below), also when the float duration sits a hair
    // under a whole number of frames — a missing last key would hold the pose at every loop seam
    const n = Math.max(2, Math.ceil(srcClip.duration * fps - 1e-4) + 1);
    const times = new Float32Array(n);
    const rot = {}; for (const [, v] of pairs) rot[v] = new Float32Array(n * 4);
    const hipsPos = new Float32Array(n * 3);
    const mixer = new THREE.AnimationMixer(srcRoot);
    const action = mixer.clipAction(srcClip);
    action.setLoop(THREE.LoopOnce, 1); action.clampWhenFinished = true;     // t = duration is the last frame, not the first
    action.play();
    const target = {}, inv = new THREE.Quaternion(), delta = new THREE.Quaternion(), local = new THREE.Quaternion();
    const hipsRest = this._restInfo.hipsLocal;
    // unit scale from leg length (axis-independent): FBX is often centimetres, GLB metres, VRM metres
    const segLen = (P, a, b) => (P[a] && P[b]) ? P[a].distanceTo(P[b]) : 0;
    const [legA, legB, legC] = rig.leg;
    const legF = segLen(restP, legA, legB) + segLen(restP, legB, legC);
    const legV = segLen(this._restInfo.pos, 'leftUpperLeg', 'leftLowerLeg') + segLen(this._restInfo.pos, 'leftLowerLeg', 'leftFoot');
    let scale = (legF > 1e-6 && legV > 1e-6) ? legV / legF : NaN;
    if (!(scale > 0.001 && scale < 10)) {
      console.warn(`[avatar] ${srcClip.name}: implausible unit scale ${scale}; assuming centimetres`);
      scale = legV > 1e-6 ? legV / 85 : 0.01;
    }
    const hipsBone = bones[rig.hips];
    const hips0 = new THREE.Vector3();
    // Ground contact: per frame, her lowest foot point (VRM leg FK with the baked rotations) is matched
    // to the source's lowest foot point (scaled), so different leg proportions never sink or float her.
    const R0 = this._restInfo.pos;
    const srcFeet = Object.keys(bones).filter((k) => /^(left|right)(Foot|Toes)$/.test(MAP[k][0]));
    const chains = ['left', 'right'].map((S) => ['hips', `${S}UpperLeg`, `${S}LowerLeg`, `${S}Foot`, `${S}Toes`].filter((b) => R0[b]));
    const rotOf = (b) => (driven.has(b) ? b : this._drivenAncestor(b, driven));
    const ground = srcFeet.length && chains.every((c) => c.length >= 4 && c.slice(0, 4).every((b) => rotOf(b))) ? { chains } : null;
    const gp = new THREE.Vector3(), go = new THREE.Vector3();
    let maxDy = 0, maxDxz = 0, gMin = 0, gMax = 0;
    let slice = performance.now();
    for (let i = 0; i < n; i++) {
      if (performance.now() - slice > 6) { await yieldTask(); slice = performance.now(); }
      const t = Math.min(srcClip.duration, i / fps);
      times[i] = t / speed;
      mixer.setTime(t);
      wrapper.updateMatrixWorld(true);
      for (const [name, vrmName] of pairs) {
        bones[name].getWorldQuaternion(q);
        inv.copy(restQ[name]).invert();
        delta.copy(q).multiply(inv);                      // world delta from rest
        target[vrmName] = (target[vrmName] || new THREE.Quaternion()).copy(delta).multiply(corr[name]);
      }
      for (const [, vrmName] of pairs) {
        const par = this._drivenAncestor(vrmName, driven);
        if (par) local.copy(target[par]).invert().multiply(target[vrmName]); else local.copy(target[vrmName]);
        const o = i * 4;
        rot[vrmName][o] = isVRM0 ? -local.x : local.x; rot[vrmName][o + 1] = local.y;
        rot[vrmName][o + 2] = isVRM0 ? -local.z : local.z; rot[vrmName][o + 3] = local.w;
      }
      // hips: full vertical motion; horizontal sway relative to the first frame, clamped so she stays on her spot
      hipsBone.getWorldPosition(p); p.sub(restP[rig.hips]).multiplyScalar(scale);
      if (i === 0) hips0.copy(p);
      const sx = p.x - hips0.x, sz = p.z - hips0.z;
      maxDxz = Math.max(maxDxz, Math.hypot(sx, sz));
      let dy = Math.max(-1.0, Math.min(1.0, p.y));
      const dx = lockXZ ? 0 : Math.max(-XZ_RANGE, Math.min(XZ_RANGE, sx));
      const dz = lockXZ ? 0 : Math.max(-XZ_RANGE, Math.min(XZ_RANGE, sz));
      if (ground) {
        let srcLow = Infinity, vrmLow = Infinity;
        for (const k of srcFeet) { bones[k].getWorldPosition(gp); srcLow = Math.min(srcLow, (gp.y - restP[k].y) * scale); }
        for (const chain of ground.chains) {
          gp.set(R0.hips.x + dx, R0.hips.y + dy, R0.hips.z + dz);
          for (let j = 1; j < chain.length; j++) {
            gp.add(go.copy(R0[chain[j]]).sub(R0[chain[j - 1]]).applyQuaternion(target[rotOf(chain[j - 1])]));
            if (j >= 3) vrmLow = Math.min(vrmLow, gp.y - R0[chain[j]].y);
          }
        }
        const g = Math.max(-0.15, Math.min(0.15, srcLow - vrmLow));
        if (Number.isFinite(g)) { dy = Math.max(-1.0, Math.min(1.0, dy + g)); gMin = Math.min(gMin, g); gMax = Math.max(gMax, g); }
      }
      maxDy = Math.max(maxDy, Math.abs(dy));
      hipsPos[i * 3] = hipsRest.x + (isVRM0 ? -dx : dx);
      hipsPos[i * 3 + 1] = hipsRest.y + dy;
      hipsPos[i * 3 + 2] = hipsRest.z + (isVRM0 ? -dz : dz);
    }
    action.stop(); mixer.uncacheRoot(srcRoot);
    wrapper.remove(srcRoot);
    for (const [o, p0, q0, s0] of src.rest) { o.position.copy(p0); o.quaternion.copy(q0); o.scale.copy(s0); }
    const deg = (r) => Math.round(THREE.MathUtils.radToDeg(r));
    this.lastBake = { clip: srcClip.name, rig: rig.id, format: src.format, scale: +scale.toFixed(5), legF: +legF.toFixed(3),
                      legV: +legV.toFixed(3), maxDy: +maxDy.toFixed(3), maxDxz: +maxDxz.toFixed(3), lockXZ,
                      bones: pairs.length, frames: n, frameFix: [deg(frameEuler.x), deg(frameEuler.y), deg(frameEuler.z)],
                      restArmDeg, srcHipsY: +(restP[rig.hips].y).toFixed(3),
                      groundFix: ground ? [+gMin.toFixed(3), +gMax.toFixed(3)] : null };
    this.onStatus(`baked ${srcClip.name} (${rig.id} ${src.format}): frame fix ${this.lastBake.frameFix.join('/')}° · scale ${this.lastBake.scale} · hips Δy≤${this.lastBake.maxDy}m sway≤${this.lastBake.maxDxz}m${lockXZ ? ' (xz locked)' : ''} · ${pairs.length} bones`);
    const tracks = [];
    for (const [, vrmName] of pairs) {
      tracks.push(new THREE.QuaternionKeyframeTrack(`${this._vrmNode(vrmName).name}.quaternion`, times, rot[vrmName]));
    }
    tracks.push(new THREE.VectorKeyframeTrack(`${this._vrmNode('hips').name}.position`, times, hipsPos));
    return new THREE.AnimationClip(srcClip.name || 'clip', srcClip.duration / speed, tracks);
  }

  /** clips.json entry for a name. Entry fields: file (GLB/glTF/FBX in assets/clips), name, clip (animation
   *  name inside a multi-clip file, matched exactly or by the part after a '|', e.g. "Idle_Loop" for
   *  FBXLoader's "Armature|Idle_Loop"; default: the first), rig (force a RIGS id), speed (playback rate > 0),
   *  once (play once, hold the last pose), fade (cross-fade seconds, 0 = cut), lockXZ (no horizontal hip
   *  sway: set it for clips with real root motion such as walk cycles; otherwise the hips keep the clip's
   *  own sway, clamped to ±XZ_RANGE of its first frame), hidden (keep out of clipNames). */
  _entry(name) {
    const clips = this.manifest?.clips || [];
    return clips.find((c) => c.name === name) || clips.find((c) => c.name.includes(name) || c.file.includes(name)) || null;
  }

  /** Run bakes one after another (a bake poses the shared source skeleton across several slices). */
  _queueBake(fn) {
    const run = (this._bakeTail || Promise.resolve()).then(fn, fn);
    this._bakeTail = run.catch(() => {});
    return run;
  }

  /** Baked clip for a manifest name (downloads + retargets on first use). */
  async getClip(name, onProgress) {
    if (!this.vrm || !this.manifest) return null;
    const entry = this._entry(name);
    if (!entry) return null;
    if (this.clipCache.has(entry.name)) return this.clipCache.get(entry.name);
    const pending = (async () => {
      const src = await this._loadSource(entry.file, onProgress);
      const srcClip = entry.clip
        ? src.clips.find((c) => c.name === entry.clip) || src.clips.find((c) => c.name.split('|').pop() === entry.clip)
        : src.clips[0];
      if (!srcClip) throw new Error(`no animation ${entry.clip ? `"${entry.clip}" ` : ''}in ${entry.file}`);
      let speed = Number(entry.speed ?? 1);
      if (!(speed > 0)) { console.warn(`[avatar] ${entry.name}: speed must be > 0, using 1`); speed = 1; }
      const clip = await this._queueBake(() => this._bake(src, srcClip, { rig: entry.rig, speed, lockXZ: !!entry.lockXZ }));
      clip.name = entry.name;
      return clip;
    })();
    this.clipCache.set(entry.name, pending);
    try { const c = await pending; this.clipCache.set(entry.name, c); return c; }
    catch (e) { this.clipCache.delete(entry.name); this.onStatus(`clip ${entry.name} failed: ${e.message}`); return null; }
  }

  // ---------- playback ----------
  // The mixer fills any weight below 1 with the bones' binding pose (the VRM's T-pose), so every switch is
  // a cross-fade whose two weights sum to 1; an action is never faded in from 0 on its own.
  _play(clip, { loop = true, fade = 0.35 } = {}) {
    if (!this.mixer || !clip) return null;
    let action = this.mixer.clipAction(clip);
    if (action === this.current) {
      // already playing: a loop just keeps going (chain [Talk, Talk], idle() while idling) ...
      if (loop) { action.setLoop(THREE.LoopRepeat, Infinity); action.clampWhenFinished = false; return action; }
      // ... a one-shot starts over, cross-fading from its held last pose into a twin action
      action = this.mixer.clipAction(this._twin(clip));
    }
    action.reset();
    action.setLoop(loop ? THREE.LoopRepeat : THREE.LoopOnce, Infinity);
    action.clampWhenFinished = !loop;
    action.enabled = true;
    action.setEffectiveWeight(1);
    if (this.current && fade > 0) { this.current.fadeOut(fade); action.fadeIn(fade); }
    else if (this.current) this.current.stop();       // fade 0: hard cut
    action.play();                                    // first clip ever: straight in at full weight
    this.current = action;
    return action;
  }

  /** A second action for the same clip (mixer.clipAction returns one action per clip). */
  _twin(clip) {
    this._twins ??= new WeakMap();
    if (!this._twins.has(clip)) this._twins.set(clip, clip.clone());
    return this._twins.get(clip);
  }

  /** Back to the idle loop (a no-op when it is already playing); cancels any running chain. */
  async idle() {
    this._chainId++;
    const clip = await this.getClip(this.idleName);
    if (clip) this._play(clip, { loop: true, fade: 0.6 });
  }

  /** Play an action chain [{name, duration}], then return to idle. Manifest entries marked "once" play a
   *  single time (never cut short) and hold their last pose; the rest loop to fill the duration.
   *  "fade" sets the cross-fade into the clip (seconds). */
  async playChain(actions = []) {
    const id = ++this._chainId;
    for (const a of actions) {
      if (id !== this._chainId) return;
      const clip = await this.getClip(a.name);
      if (id !== this._chainId) return;
      if (!clip) { this.onStatus(`no clip for ${a.name}`); continue; }
      const entry = this._entry(a.name) || {};
      const once = !!entry.once;
      let dur = Math.max(0.4, Number(a.duration) || clip.duration);
      if (once) dur = Math.max(dur, clip.duration);
      const fade = entry.fade != null && Number(entry.fade) >= 0 ? Number(entry.fade) : 0.35;
      this._play(clip, { loop: !once, fade });
      this.onStatus(`▶ ${a.name} (${dur.toFixed(1)}s)`);
      await sleep(dur * 1000);
    }
    if (id === this._chainId) this.idle();
  }

  async playOnce(name, duration) {
    return this.playChain([{ name, duration: duration || 0 }]);
  }

  // ---------- face ----------
  /**
   * Mood vs. moment: the backend's emotion state is a slow mood (decays 0.02/min), so the face
   * keeps only a soft baseline of it (MOOD_WEIGHT). A full-strength "pulse" fires when a reply
   * arrives and holds while she speaks, then fades — so she reacts, but never freezes mid-laugh.
   */
  setEmotion(emotion, intensity = 0.5, { pulse = true } = {}) {
    const base = EMOTION_EXPR[emotion] || {};
    const k = Math.max(0.25, Math.min(1, 0.35 + Number(intensity) * 0.75));
    const t = {};
    for (const n of EXPR_NAMES) t[n] = (base[n] || 0) * k;
    this._mood = t;
    if (pulse) { this._pulse = t; this._pulseAge = 0; }
    this._manual = null;
  }

  /** State-tab tester: hold one preset until the next setEmotion(). */
  setExpression(name, weight = 1) {
    const t = {}; for (const n of EXPR_NAMES) t[n] = n === name ? weight : 0;
    this._manual = t;
  }

  _faceTargets(dt) {
    const MOOD_WEIGHT = 0.4, PULSE_HOLD_S = 2.0, PULSE_FADE_S = 4.0, MAX = 0.8;
    if (this.speaking) this._pulseAge = 0; else this._pulseAge = (this._pulseAge ?? 99) + dt;
    const age = this._pulseAge;
    const pf = age < PULSE_HOLD_S ? 1 : Math.max(0, 1 - (age - PULSE_HOLD_S) / PULSE_FADE_S);
    const out = {};
    for (const n of EXPR_NAMES) {
      if (this._manual) { out[n] = this._manual[n] || 0; continue; }
      const mood = (this._mood?.[n] || 0) * MOOD_WEIGHT;
      const pulse = (this._pulse?.[n] || 0) * (1 - MOOD_WEIGHT) * pf;
      out[n] = Math.min(MAX, mood + pulse);
    }
    for (const [n, cap] of Object.entries(MOUTH_OPENING_PRESETS)) out[n] = Math.min(out[n] || 0, cap);
    return out;
  }

  setMouth(level) { this._mouthTarget = Math.max(0, Math.min(1, level)); }

  _tick() {
    const dt = Math.min(0.1, this.clock.getDelta());
    if (this._rayUniforms) this._rayUniforms.uTime.value += dt;
    this.env?.update(dt);
    if (this.mixer) this.mixer.update(dt);
    const em = this.vrm?.expressionManager;
    if (em) {
      const targets = this._faceTargets(dt);
      for (const n of EXPR_NAMES) {
        const cur = this._exprNow[n] || 0, tgt = targets[n] || 0;
        const nv = cur + (tgt - cur) * Math.min(1, dt * 4);
        this._exprNow[n] = nv;
        em.setValue(n, nv);
      }
      // lipsync — fast attack, slightly slower release; jaw never past ~half open
      const rate = this._mouthTarget > this._mouth ? 22 : 14;
      this._mouth += (this._mouthTarget - this._mouth) * Math.min(1, dt * rate);
      const m = this.speaking ? this._mouth : 0;
      em.setValue('aa', Math.min(0.5, m * 0.55));
      em.setValue('oh', Math.min(0.15, m * 0.15));
      // blink
      this._blinkT -= dt;
      if (this._blinkT <= 0) { this._blinkV = 1; this._blinkT = 2.2 + Math.random() * 4; }
      if (this._blinkV > 0) { this._blinkV = Math.max(0, this._blinkV - dt * 7); em.setValue('blink', Math.sin(Math.min(1, this._blinkV) * Math.PI)); }
      else em.setValue('blink', 0);
    }
    if (this.vrm) this.vrm.update(dt);
    // keep the orbit centre on her body (head…shoes per focusT); eased so breathing doesn't jitter the view,
    // snappy while the user is right-dragging the focus
    if (this._focusPoint(this._headTmp)) this.controls.target.lerp(this._headTmp, Math.min(1, dt * (this._focusDrag ? 14 : 1.5)));
    this.controls.update();
    this._keepCameraAboveFloor();
    if (this.env) this.env.render(); else this.renderer.render(this.scene, this.camera);
  }

  /** Focus point on her body: x/z of the head, y between her shoes (focusT=0) and eye level (focusT=1). */
  _focusPoint(out) {
    const h = this.vrm?.humanoid;
    const head = h?.getRawBoneNode('head');
    if (!head) return false;
    head.getWorldPosition(out);
    const headY = out.y + 0.06;
    let feetY = 0.1;
    const lf = h.getRawBoneNode('leftFoot'), rf = h.getRawBoneNode('rightFoot');
    if (lf && rf) { lf.getWorldPosition(this._tmpA); rf.getWorldPosition(this._tmpB); feetY = (this._tmpA.y + this._tmpB.y) / 2 + 0.03; }
    out.y = feetY + (headY - feetY) * this.focusT;
    return true;
  }

  /** Never let the orbit camera sink into the street. First choice: pull it in toward her along the
   *  same line of sight (low angles still work, just closer). If that would violate minDistance
   *  (focus already near the ground), keep the distance and raise the camera to floor level instead. */
  _keepCameraAboveFloor() {
    const FLOOR = 0.15;
    const cam = this.camera.position, tgt = this.controls.target;
    if (cam.y >= FLOOR) return;
    const off = this._offTmp.copy(cam).sub(tgt);
    const d = off.length();
    const dyWanted = FLOOR - tgt.y;
    let done = false;
    if (off.y < -1e-4) {
      const t = dyWanted / off.y;                   // fraction of target→camera that reaches floor height
      if (t > 0 && t < 1 && d * t >= this.controls.minDistance) { cam.copy(tgt).addScaledVector(off, t); done = true; }
    }
    if (!done) {
      const dyc = Math.max(-d + 1e-3, Math.min(d - 1e-3, dyWanted));
      const horiz = Math.sqrt(Math.max(0, d * d - dyc * dyc));
      const hl = Math.hypot(off.x, off.z);
      const hx = hl > 1e-6 ? off.x / hl : 0, hz = hl > 1e-6 ? off.z / hl : 1;
      cam.set(tgt.x + hx * horiz, tgt.y + dyc, tgt.z + hz * horiz);
    }
    this.camera.lookAt(tgt);
  }
}
