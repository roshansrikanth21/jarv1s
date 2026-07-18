import { useEffect, useRef } from "react";
import * as THREE from "three";

export type OrbState = "idle" | "listening" | "thinking" | "speaking" | "low";

export type OrbCoords = {
  x: number;
  y: number;
  z: number;
  theta: number;
  phi: number;
  rpm: number;
};

type Props = {
  state: OrbState;
  audioLevel?: number;
  onCoordinates?: (c: OrbCoords) => void;
};

const PURPLE = 0xc8a8ff;
const PURPLE_DIM = 0x7b5cff;
const CORE = 0xf0e4ff;

function jitterPositions(pos: Float32Array, amount: number) {
  for (let i = 0; i < pos.length; i += 3) {
    pos[i] += (Math.random() - 0.5) * amount;
    pos[i + 1] += (Math.random() - 0.5) * amount;
    pos[i + 2] += (Math.random() - 0.5) * amount;
  }
}

function lineShell(radius: number, detail: number, opacity: number, color: number, jitter = 0) {
  const geo = new THREE.IcosahedronGeometry(radius, detail);
  const edges = new THREE.EdgesGeometry(geo);
  if (jitter > 0) jitterPositions(edges.attributes.position.array as Float32Array, jitter);
  const mat = new THREE.LineBasicMaterial({
    color,
    transparent: true,
    opacity,
    blending: THREE.AdditiveBlending,
    depthWrite: false,
  });
  return new THREE.LineSegments(edges, mat);
}

function eyeRing(radiusX: number, radiusY: number) {
  const curve = new THREE.EllipseCurve(0, 0, radiusX, radiusY, 0, Math.PI * 2, false, 0);
  const pts = curve.getPoints(160).map((p) => new THREE.Vector3(p.x, p.y * 0.22, p.y));
  const geo = new THREE.BufferGeometry().setFromPoints(pts);
  const mat = new THREE.LineBasicMaterial({
    color: PURPLE,
    transparent: true,
    opacity: 0.85,
    blending: THREE.AdditiveBlending,
    depthWrite: false,
  });
  return new THREE.Line(geo, mat);
}

function slashLine() {
  const pts = [new THREE.Vector3(-1.15, 0.95, 0.15), new THREE.Vector3(1.05, -0.85, -0.1)];
  const geo = new THREE.BufferGeometry().setFromPoints(pts);
  const mat = new THREE.LineBasicMaterial({
    color: CORE,
    transparent: true,
    opacity: 0.55,
    blending: THREE.AdditiveBlending,
    depthWrite: false,
  });
  return new THREE.Line(geo, mat);
}

function corePoints(count: number) {
  const positions = new Float32Array(count * 3);
  for (let i = 0; i < count; i++) {
    const u = Math.random();
    const v = Math.random();
    const theta = 2 * Math.PI * u;
    const phi = Math.acos(2 * v - 1);
    const r = 0.08 + Math.random() * 0.22;
    positions[i * 3] = r * Math.sin(phi) * Math.cos(theta);
    positions[i * 3 + 1] = r * Math.sin(phi) * Math.sin(theta);
    positions[i * 3 + 2] = r * Math.cos(phi);
  }
  const geo = new THREE.BufferGeometry();
  geo.setAttribute("position", new THREE.BufferAttribute(positions, 3));
  const mat = new THREE.PointsMaterial({
    color: CORE,
    size: 0.028,
    transparent: true,
    opacity: 0.9,
    blending: THREE.AdditiveBlending,
    depthWrite: false,
    sizeAttenuation: true,
  });
  return new THREE.Points(geo, mat);
}

export function CoreOrb3D({ state, audioLevel = 0, onCoordinates }: Props) {
  const mountRef = useRef<HTMLDivElement>(null);
  const stateRef = useRef(state);
  const levelRef = useRef(audioLevel);
  const pointerRef = useRef({ x: 0, y: 0 });

  stateRef.current = state;
  levelRef.current = audioLevel;

  useEffect(() => {
    const mount = mountRef.current;
    if (!mount) return;

    const w = mount.clientWidth || 480;
    const h = mount.clientHeight || 480;

    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(42, w / h, 0.1, 100);
    camera.position.z = 3.35;

    const renderer = new THREE.WebGLRenderer({
      antialias: true,
      alpha: true,
      powerPreference: "high-performance",
    });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.setSize(w, h);
    renderer.setClearColor(0x000000, 0);
    mount.appendChild(renderer.domElement);

    const root = new THREE.Group();
    scene.add(root);

    const shells = [
      lineShell(1.02, 4, 0.42, PURPLE),
      lineShell(0.86, 3, 0.55, PURPLE_DIM, 0.018),
      lineShell(0.7, 2, 0.38, PURPLE, 0.028),
      lineShell(0.54, 2, 0.28, PURPLE_DIM, 0.04),
    ];
    shells.forEach((s, i) => {
      s.rotation.x = 0.35 + i * 0.12;
      s.rotation.z = i * 0.2;
      root.add(s);
    });

    const eye = eyeRing(1.08, 0.42);
    root.add(eye);

    const slash = slashLine();
    slash.rotation.z = 0.15;
    root.add(slash);

    const core = corePoints(420);
    root.add(core);

    const inner = lineShell(0.26, 3, 0.75, CORE, 0.01);
    root.add(inner);

    let targetRx = 0;
    let targetRy = 0;
    let rpm = 0;
    let raf = 0;
    let t0 = performance.now();
    let paused = document.hidden;

    const onVis = () => {
      paused = document.hidden;
    };
    document.addEventListener("visibilitychange", onVis);

    const onMove = (e: PointerEvent) => {
      const rect = mount.getBoundingClientRect();
      pointerRef.current = {
        x: ((e.clientX - rect.left) / rect.width - 0.5) * 2,
        y: ((e.clientY - rect.top) / rect.height - 0.5) * 2,
      };
      targetRy = pointerRef.current.x * 0.85;
      targetRx = pointerRef.current.y * 0.55;
    };

    mount.addEventListener("pointermove", onMove);

    const animate = (now: number) => {
      raf = requestAnimationFrame(animate);
      if (paused) return;
      const dt = Math.min(0.05, (now - t0) / 1000);
      t0 = now;

      const st = stateRef.current;
      const lvl = levelRef.current;
      const baseSpeed =
        st === "thinking"
          ? 1.35
          : st === "speaking"
            ? 0.95
            : st === "listening"
              ? 0.65
              : st === "low"
                ? 0.25
                : 0.42;
      rpm = baseSpeed * 12;

      root.rotation.y += dt * baseSpeed;
      root.rotation.x += (targetRx - root.rotation.x) * 0.06;
      root.rotation.z += (targetRy * 0.35 - root.rotation.z) * 0.05;

      shells.forEach((s, i) => {
        s.rotation.y -= dt * (0.25 + i * 0.08) * (i % 2 === 0 ? 1 : -1);
        if (st === "thinking") s.rotation.x += dt * 0.4 * (i + 1);
      });
      eye.rotation.y = root.rotation.y * 0.5;
      slash.rotation.y = -root.rotation.y * 0.25;
      core.rotation.y += dt * 1.6;
      inner.rotation.x -= dt * 1.1;

      const pulse = st === "speaking" ? 1 + Math.sin(now * 0.008) * 0.06 : 1;
      const listen = st === "listening" ? 1 + lvl * 0.18 : 1;
      const scale = pulse * listen;
      root.scale.setScalar(scale);

      const glow =
        st === "speaking"
          ? 0.95
          : st === "listening"
            ? 0.75 + lvl * 0.25
            : st === "thinking"
              ? 0.82
              : 0.55;
      shells.forEach((s) => {
        const m = s.material as THREE.LineBasicMaterial;
        m.opacity = glow * (0.5 + Math.sin(now * 0.003 + s.id) * 0.08);
      });

      renderer.render(scene, camera);

      const dir = new THREE.Vector3(0, 0, 1).applyQuaternion(root.quaternion);
      onCoordinates?.({
        x: dir.x,
        y: dir.y,
        z: dir.z,
        theta: THREE.MathUtils.radToDeg(root.rotation.y),
        phi: THREE.MathUtils.radToDeg(root.rotation.x),
        rpm,
      });
    };

    raf = requestAnimationFrame(animate);

    const ro = new ResizeObserver(() => {
      const nw = mount.clientWidth;
      const nh = mount.clientHeight;
      if (!nw || !nh) return;
      camera.aspect = nw / nh;
      camera.updateProjectionMatrix();
      renderer.setSize(nw, nh);
    });
    ro.observe(mount);

    return () => {
      cancelAnimationFrame(raf);
      document.removeEventListener("visibilitychange", onVis);
      mount.removeEventListener("pointermove", onMove);
      ro.disconnect();
      renderer.dispose();
      shells.forEach((s) => {
        s.geometry.dispose();
        (s.material as THREE.Material).dispose();
      });
      eye.geometry.dispose();
      (eye.material as THREE.Material).dispose();
      slash.geometry.dispose();
      (slash.material as THREE.Material).dispose();
      core.geometry.dispose();
      (core.material as THREE.Material).dispose();
      inner.geometry.dispose();
      (inner.material as THREE.Material).dispose();
      if (renderer.domElement.parentNode === mount) mount.removeChild(renderer.domElement);
    };
  }, [onCoordinates]);

  return <div ref={mountRef} className="pr-orb-canvas" aria-hidden />;
}
