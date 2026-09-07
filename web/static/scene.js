/* ===========================================================================
   FaceChain - the 3D background
   ===========================================================================

   A transparent WebGL layer that sits behind the landing page and turns a
   handful of GLB props into slowly drifting brushed-metal objects.

   Three rules govern everything in here:

     1. It is a BACKGROUND. Nothing moves fast enough to pull the eye off the
        copy: a full rotation takes 18-40 seconds and the drift is a few
        hundredths of a unit per second.
     2. It costs nothing when nobody is looking. Hidden tab, scrolled out of
        view, or prefers-reduced-motion - the loop stops.
     3. It is allowed to find nothing. archive/ is gitignored, so a fresh
        clone has zero GLB files. No models, no WebGL, or a corrupt GLB all
        end the same way: one console.info and an empty canvas. The page is
        designed to look finished without it.
   ========================================================================= */

import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/GLTFLoader.js';

/* ---- Tunables ----------------------------------------------------------- */

const TARGET_RADIUS = 1.0;   // every model is scaled to this bounding radius
const DRIFT_AMP     = 0.12;  // max drift along each axis, per model
const GAP_MARGIN    = 0.60;  // clear space demanded between two hulls
const SPIN_MIN      = 18.0;  // seconds for a full turn (slowest)
const SPIN_MAX      = 40.0;  // seconds for a full turn (fastest allowed)

const SIGNAL = 0x00e5ff;     // --signal, the one accent

/* ---------------------------------------------------------------------------
   WHY THE MODELS CAN NEVER TOUCH
   ---------------------------------------------------------------------------
   Each model is normalised to a bounding SPHERE of radius TARGET_RADIUS and
   recentred on that sphere's centre, so "model i occupies the ball of radius
   r around p_i" is exact, not approximate - and because a bounding sphere is
   rotation-invariant, the per-model spin cannot enlarge it either.

   The n slots are equally spaced on a ring of radius R, so the closest pair of
   slots are adjacent ones, separated by the chord

       c = 2 * R * sin(pi / n).

   Every model then drifts inside an axis-aligned box of half-extent
   DRIFT_AMP, so its centre never leaves a ball of radius

       m = DRIFT_AMP * sqrt(3)

   around its slot. By the triangle inequality the centre distance of any pair
   is therefore at least c - 2m, and we want that to leave GAP_MARGIN of clear
   space between two hulls of radius r:

       c - 2m  >=  2r + GAP_MARGIN

   Solving for R gives the ring radius used below. The bound is worst-case and
   holds at every instant of every orbit - it does not depend on the phases,
   the frequencies, or how long the page has been open. Rigidly moving the
   whole group (the tilt and offset applied to `root`) preserves all pairwise
   distances, so it cannot break the invariant either.
   ------------------------------------------------------------------------ */
function ringRadius(n) {
  if (n < 2) return 0;
  const maxDrift = DRIFT_AMP * Math.sqrt(3);
  const needed = 2 * TARGET_RADIUS + GAP_MARGIN + 2 * maxDrift;
  return needed / (2 * Math.sin(Math.PI / n));
}

/* A tiny deterministic PRNG, so a given model list always lays out the same
   way. Randomness that changes on every reload reads as a bug, not as life. */
function hashSeed(str) {
  let h = 2166136261 >>> 0;
  for (let i = 0; i < str.length; i++) {
    h ^= str.charCodeAt(i);
    h = Math.imul(h, 16777619) >>> 0;
  }
  return h;
}
function rng(seed) {
  let s = seed >>> 0 || 1;
  return () => {
    s ^= s << 13; s >>>= 0;
    s ^= s >> 17;
    s ^= s << 5;  s >>>= 0;
    return s / 4294967296;
  };
}

/* An equirectangular studio gradient, pushed through PMREM so the metals have
   something to reflect. Without an environment, MeshStandardMaterial with high
   metalness renders as a black silhouette. */
function studioEnvironment(renderer) {
  const w = 32, h = 64;
  const data = new Uint8Array(w * h * 4);
  const top = new THREE.Color(0x2b3238);
  const bottom = new THREE.Color(0x08090a);
  const rim = new THREE.Color(SIGNAL);
  for (let y = 0; y < h; y++) {
    const t = y / (h - 1);
    const c = bottom.clone().lerp(top, Math.pow(1 - t, 1.4));
    // a narrow cyan band low on the horizon: the rim light, reflected
    const band = Math.exp(-Math.pow((t - 0.62) * 9, 2)) * 0.35;
    c.lerp(rim, band);
    for (let x = 0; x < w; x++) {
      const i = (y * w + x) * 4;
      data[i] = c.r * 255; data[i + 1] = c.g * 255;
      data[i + 2] = c.b * 255; data[i + 3] = 255;
    }
  }
  const tex = new THREE.DataTexture(data, w, h, THREE.RGBAFormat);
  tex.mapping = THREE.EquirectangularReflectionMapping;
  tex.colorSpace = THREE.SRGBColorSpace;
  tex.needsUpdate = true;
  const pmrem = new THREE.PMREMGenerator(renderer);
  const env = pmrem.fromEquirectangular(tex).texture;
  pmrem.dispose();
  tex.dispose();
  return env;
}

/* ---- The one export ----------------------------------------------------- */

/**
 * @param {HTMLCanvasElement} canvas
 * @param {{ endpoint?: string, fov?: number, distance?: number }} [options]
 * @returns {{ dispose: () => void }} always returns; never throws.
 */
export function initScene(canvas, options = {}) {
  const endpoint = options.endpoint || '/api/scene';
  const reduced = window.matchMedia &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  let renderer;
  try {
    renderer = new THREE.WebGLRenderer({
      canvas,
      alpha: true,
      antialias: true,
      powerPreference: 'high-performance',
    });
  } catch (err) {
    console.info('[scene] WebGL unavailable, background stays empty:', err && err.message);
    return { dispose() {} };
  }
  if (!renderer.getContext()) {
    console.info('[scene] WebGL unavailable, background stays empty.');
    return { dispose() {} };
  }

  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.setClearAlpha(0);                       // the CSS ground shows through
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.15;

  const scene = new THREE.Scene();                 // no scene.background: transparent
  // Close enough that the ring runs off the top and right of the frame: the
  // props are meant to be cropped by the viewport, not politely contained.
  const DISTANCE = options.distance || 8.4;
  const camera = new THREE.PerspectiveCamera(options.fov || 38, 1, 0.1, 200);
  camera.position.set(0, 0, DISTANCE);
  camera.lookAt(0, 0, 0);

  scene.environment = studioEnvironment(renderer);

  // Lighting for brushed metal: a low ambient so nothing is pure black, one
  // key from the upper front-left, and a cyan rim from behind-right that
  // catches the silhouettes. The rim is the only coloured light in the scene.
  scene.add(new THREE.AmbientLight(0xffffff, 0.18));
  const key = new THREE.DirectionalLight(0xffffff, 1.9);
  key.position.set(-4, 6, 7);
  scene.add(key);
  const rim = new THREE.DirectionalLight(SIGNAL, 0.55);
  rim.position.set(5, -2, -6);
  scene.add(rim);

  const root = new THREE.Group();
  // A rigid tilt + offset. This is what foreshortens the ring into an ellipse
  // on screen; because it is a rotation and a translation it preserves every
  // pairwise distance, so the no-intersection proof above still stands.
  root.rotation.set(-0.55, 0.0, 0.13);
  root.position.set(2.10, 0.35, 0);
  scene.add(root);

  const props = [];        // { pivot, mesh, slot, spin, freq, phase }
  let disposed = false;
  let running = false;
  let visible = true;
  let onScreen = true;
  let frame = 0;
  const clock = new THREE.Clock();

  /* ---- sizing --------------------------------------------------------- */
  function resize() {
    const w = canvas.clientWidth || canvas.parentElement?.clientWidth || window.innerWidth;
    const h = canvas.clientHeight || canvas.parentElement?.clientHeight || window.innerHeight;
    if (!w || !h) return;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    // Narrow viewports crop a horizontal ring badly; pull the camera back so
    // the whole layout stays inside the frustum instead of clipping.
    const fit = Math.min(1.9, Math.max(1, 1.25 / camera.aspect));
    camera.position.z = DISTANCE * fit;
    camera.updateProjectionMatrix();
    if (!running) renderOnce();
  }

  function renderOnce() {
    if (disposed) return;
    try { renderer.render(scene, camera); } catch (_) { /* context lost */ }
  }

  /* ---- the loop ------------------------------------------------------- */
  function tick() {
    if (disposed || !running) return;
    frame = requestAnimationFrame(tick);
    const t = clock.getElapsedTime();
    for (const p of props) {
      // Bounded lissajous drift: each component is a sine, so |offset| along
      // any axis is <= DRIFT_AMP by construction. Nothing here can grow.
      p.pivot.position.set(
        p.slot.x + DRIFT_AMP * Math.sin(t * p.freq.x + p.phase.x),
        p.slot.y + DRIFT_AMP * Math.sin(t * p.freq.y + p.phase.y),
        p.slot.z + DRIFT_AMP * Math.sin(t * p.freq.z + p.phase.z),
      );
      p.mesh.rotation.y = p.spin.y * t + p.phase.x;
      p.mesh.rotation.x = p.spin.x * t;
    }
    renderer.render(scene, camera);
  }

  function wanted() {
    return !disposed && !reduced && props.length > 0 && visible && onScreen;
  }
  function sync() {
    if (wanted() && !running) {
      running = true;
      clock.getDelta();          // swallow the paused interval
      frame = requestAnimationFrame(tick);
    } else if (!wanted() && running) {
      running = false;
      cancelAnimationFrame(frame);
    }
  }

  /* ---- observers ------------------------------------------------------ */
  const onVisibility = () => { visible = !document.hidden; sync(); };
  document.addEventListener('visibilitychange', onVisibility);

  const onResize = () => resize();
  window.addEventListener('resize', onResize);

  let ro = null;
  if (typeof ResizeObserver !== 'undefined') {
    ro = new ResizeObserver(() => resize());
    ro.observe(canvas);
  }

  let io = null;
  if (typeof IntersectionObserver !== 'undefined') {
    io = new IntersectionObserver((entries) => {
      for (const e of entries) onScreen = e.isIntersecting;
      sync();
    }, { threshold: 0 });
    io.observe(canvas);
  }

  resize();

  /* ---- normalise one loaded GLB --------------------------------------- */
  const fallback = new THREE.MeshStandardMaterial({
    color: 0x9a9a9a, metalness: 0.92, roughness: 0.42,
  });

  function normalise(object3d) {
    // Recentre on the bounding sphere's own centre, then scale that sphere to
    // TARGET_RADIUS. Source GLBs come in wildly different units; after this
    // every prop is exactly one unit-radius ball around its pivot.
    const box = new THREE.Box3().setFromObject(object3d);
    if (box.isEmpty()) return null;
    const sphere = box.getBoundingSphere(new THREE.Sphere());
    if (!(sphere.radius > 0) || !isFinite(sphere.radius)) return null;

    const inner = new THREE.Group();
    object3d.position.sub(sphere.center);
    inner.add(object3d);
    inner.scale.setScalar(TARGET_RADIUS / sphere.radius);

    object3d.traverse((n) => {
      if (!n.isMesh) return;
      n.frustumCulled = true;
      if (!n.material) {
        n.material = fallback;
        return;
      }
      const mats = Array.isArray(n.material) ? n.material : [n.material];
      for (const m of mats) {
        if (!m) continue;
        // Desaturate to a machined grey. These props are furniture: the only
        // colour on the page belongs to the accent, and a stray gold or blue
        // GLB would compete with the headline for it.
        if (m.color && m.color.isColor) {
          const lum = 0.2126 * m.color.r + 0.7152 * m.color.g + 0.0722 * m.color.b;
          const g = 0.30 + 0.45 * lum;   // clamp into a mid-grey band
          m.color.setRGB(g, g, g);
        }
        if (m.emissive && m.emissive.isColor) m.emissive.setRGB(0, 0, 0);
        if (m.isMeshStandardMaterial) {
          m.envMapIntensity = 0.85;
          if (m.metalness < 0.5) m.metalness = 0.88;
          m.roughness = Math.min(0.55, Math.max(0.28, m.roughness || 0.4));
        }
      }
    });
    return inner;
  }

  /* ---- load ------------------------------------------------------------ */
  function layout(models) {
    const n = models.length;
    const R = ringRadius(n);
    const seed = hashSeed(models.map((m) => m.url).join('|'));
    const rand = rng(seed);

    models.forEach((m, i) => {
      const a = (i / n) * Math.PI * 2 + 0.35;
      const pivot = new THREE.Group();
      // A true circle in the group's XY plane. The on-screen ellipse comes
      // from tilting `root`, which is a rigid rotation and therefore leaves
      // every pairwise distance - and the proof above - untouched. Squashing
      // the ring here instead would not be rigid, and would quietly shrink
      // the separation between the slots near the minor axis.
      const slot = new THREE.Vector3(Math.cos(a) * R, Math.sin(a) * R, 0);
      pivot.position.copy(slot);
      pivot.add(m.object);
      root.add(pivot);

      const period = SPIN_MIN + rand() * (SPIN_MAX - SPIN_MIN);
      props.push({
        pivot,
        mesh: m.object,
        slot,
        spin: {
          y: (Math.PI * 2) / period * (rand() < 0.5 ? -1 : 1),
          x: (Math.PI * 2) / (period * 3.7),
        },
        freq: {
          x: 0.055 + rand() * 0.05,
          y: 0.048 + rand() * 0.05,
          z: 0.041 + rand() * 0.05,
        },
        phase: { x: rand() * 6.28, y: rand() * 6.28, z: rand() * 6.28 },
      });
    });
  }

  const loader = new GLTFLoader();

  (async () => {
    let urls = [];
    try {
      const res = await fetch(endpoint, { headers: { accept: 'application/json' } });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const body = await res.json();
      urls = Array.isArray(body && body.models) ? body.models : [];
    } catch (err) {
      console.info('[scene] no model index (%s); background stays empty.',
        (err && err.message) || err);
      return;
    }
    if (disposed) return;
    if (!urls.length) {
      console.info('[scene] archive/ holds no .glb files; background stays empty.');
      return;
    }

    const settled = await Promise.all(urls.map((url) => new Promise((resolve) => {
      loader.load(url,
        (gltf) => resolve({ url, object: gltf.scene || gltf.scenes?.[0] || null }),
        undefined,
        (err) => {
          console.info('[scene] skipped %s: %s', url, (err && err.message) || err);
          resolve(null);
        });
    })));
    if (disposed) return;

    const ok = [];
    for (const s of settled) {
      if (!s || !s.object) continue;
      const inner = normalise(s.object);
      if (inner) ok.push({ url: s.url, object: inner });
    }
    if (!ok.length) {
      console.info('[scene] every GLB failed to load; background stays empty.');
      return;
    }

    layout(ok);

    if (reduced) {
      // One static frame, no loop, no drift. The composition still reads.
      renderOnce();
      return;
    }
    sync();
  })();

  /* ---- teardown -------------------------------------------------------- */
  function dispose() {
    if (disposed) return;
    disposed = true;
    running = false;
    cancelAnimationFrame(frame);
    document.removeEventListener('visibilitychange', onVisibility);
    window.removeEventListener('resize', onResize);
    if (ro) ro.disconnect();
    if (io) io.disconnect();
    scene.traverse((n) => {
      if (n.isMesh) {
        n.geometry?.dispose?.();
        const mats = Array.isArray(n.material) ? n.material : [n.material];
        for (const m of mats) m?.dispose?.();
      }
    });
    scene.environment?.dispose?.();
    renderer.dispose();
  }

  return { dispose };
}

/* Auto-boot: the landing page just needs <canvas id="scene">. */
const el = document.getElementById('scene');
if (el) initScene(el);

export default initScene;
