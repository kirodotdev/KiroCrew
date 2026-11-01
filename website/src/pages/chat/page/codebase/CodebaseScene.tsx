/**
 * The 3D view of a codebase session (PoC), drawn with three.js.
 *
 * City: folders are stacked districts and files are buildings (`layoutCity`).
 * Graph: folders as spheres, joined where their files import each other, settling
 * under a force layout (`graphOf` / `stepGraph`).
 *
 * - Orbit with the left mouse, pan with the right (or Shift-drag), scroll to zoom.
 * - Click picks a node, Shift-click adds to the pick, a click on empty space clears
 *   it, a double-click flies to the node.
 * - Picked files draw their imports as arcs: out in the accent colour, in amber.
 * - A rescan animates: buildings grow, shrink or move to their new place, new ones
 *   rise from the ground, removed ones sink, and the files that changed glow.
 * - Anything in `children` carrying `data-node` is pinned to that node's top on
 *   screen every frame, placed by its `data-place`, so the conductor's overlay and
 *   answers follow the model as it moves.
 */
import { useEffect, useRef, useState, type MutableRefObject, type ReactNode } from 'react'
import * as THREE from 'three'
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js'
import {
  dirNode, fileNode, graphOf, langColor, layoutCity, nodeLabel, recencyColors, stepGraph,
  type CityBuilding, type CityLayout, type CodeModel, type ColorBy, type GraphLayout, type Lens, type Point3,
} from './codebaseModel'

export interface SceneApi {
  /** Fly the camera so these nodes fill the view. */
  focus: (nodeIds: string[]) => void
  /** Frame the whole model. */
  fit: () => void
  /** Where a node's top is on screen right now, or null when it is not drawn. */
  screenOf: (nodeId: string) => { x: number; y: number } | null
}

interface Props {
  model: CodeModel
  lens: Lens
  colorBy: ColorBy
  selected: string[]
  onPick: (nodeId: string | null, additive: boolean) => void
  /** Paths whose line count changed (or that are new) in the latest rescan: they glow. */
  changed: Set<string>
  apiRef: MutableRefObject<SceneApi | null>
  children?: ReactNode
}

type Box = { x: number; z: number; w: number; d: number; base: number; h: number }

const TWEEN_MS = 900
const GLOW_MS = 4000
const ease = (t: number) => 1 - Math.pow(1 - t, 3)
const lerp = (a: number, b: number, t: number) => a + (b - a) * t

function cssVar(el: Element, name: string, fallback: string): string {
  const v = getComputedStyle(el).getPropertyValue(name).trim()
  return v || fallback
}

/** True when the theme's text is lighter than its background: draw for a dark room. */
function isDarkTheme(el: Element): boolean {
  const c = new THREE.Color(cssVar(el, '--bg', '#ffffff'))
  return c.r * 0.3 + c.g * 0.59 + c.b * 0.11 < 0.5
}

export default function CodebaseScene({ model, lens, colorBy, selected, onPick, changed, apiRef, children }: Props) {
  const host = useRef<HTMLDivElement>(null)
  const layer = useRef<HTMLDivElement>(null)
  const labels = useRef<HTMLDivElement>(null)
  const [failed, setFailed] = useState(false)
  const [hover, setHover] = useState<{ id: string; x: number; y: number } | null>(null)

  // Everything three.js owns, kept across renders.
  const three = useRef<{
    renderer: THREE.WebGLRenderer; scene: THREE.Scene; camera: THREE.PerspectiveCamera; controls: OrbitControls
    world: THREE.Group; arcs: THREE.LineSegments; outline: THREE.LineSegments; raycaster: THREE.Raycaster
    accent: THREE.Color; amber: THREE.Color; dark: boolean
  } | null>(null)

  // What is drawn now, for picking, anchoring and the next tween.
  const drawn = useRef<{
    lens: Lens
    city?: { layout: CityLayout; mesh: THREE.InstancedMesh; districts: THREE.InstancedMesh; boxes: Box[]; paths: string[]; colors: THREE.Color[]; tweenFrom: Box[]; tweenTo: Box[]; tweenStart: number; removedFrom: number }
    graph?: { layout: GraphLayout; mesh: THREE.InstancedMesh; links: THREE.LineSegments; colors: THREE.Color[]; alpha: number }
    anchors: Map<string, Point3>
    glowStart: number
  }>({ lens: 'city', anchors: new Map(), glowStart: 0 })
  const lastBoxes = useRef<Map<string, Box>>(new Map())
  const flight = useRef<{ from: THREE.Vector3; to: THREE.Vector3; tFrom: THREE.Vector3; tTo: THREE.Vector3; start: number } | null>(null)
  const props = useRef({ model, selected, changed, onPick })
  props.current = { model, selected, changed, onPick }

  // ---- set up once ------------------------------------------------------------------------
  useEffect(() => {
    const el = host.current
    if (!el) return
    let renderer: THREE.WebGLRenderer
    try {
      renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true })
    } catch {
      setFailed(true)
      return
    }
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2))
    renderer.shadowMap.enabled = true
    renderer.shadowMap.type = THREE.PCFSoftShadowMap
    renderer.domElement.style.display = 'block'
    el.prepend(renderer.domElement)
    const dark = isDarkTheme(el)
    const scene = new THREE.Scene()
    const bg = new THREE.Color(cssVar(el, '--bg', dark ? '#1e1e1e' : '#fafafa'))
    scene.fog = new THREE.Fog(bg, 1, 1)
    const camera = new THREE.PerspectiveCamera(42, 1, 0.5, 40000)
    const controls = new OrbitControls(camera, renderer.domElement)
    controls.enableDamping = true
    controls.dampingFactor = 0.08
    controls.screenSpacePanning = false
    scene.add(new THREE.HemisphereLight(0xffffff, dark ? 0x30343c : 0xb8c0cc, dark ? 1.1 : 1.3))
    const sun = new THREE.DirectionalLight(0xffffff, dark ? 1.6 : 1.9)
    sun.castShadow = true
    sun.shadow.mapSize.set(2048, 2048)
    sun.shadow.bias = -0.0004
    scene.add(sun, sun.target)
    const world = new THREE.Group()
    scene.add(world)
    const accent = new THREE.Color(cssVar(el, '--accent', '#4f8cff'))
    const amber = new THREE.Color('#f0a35c')
    const arcs = new THREE.LineSegments(new THREE.BufferGeometry(), new THREE.LineBasicMaterial({ vertexColors: true, transparent: true, opacity: 0.95, depthTest: false }))
    arcs.renderOrder = 10
    const outline = new THREE.LineSegments(new THREE.BufferGeometry(), new THREE.LineBasicMaterial({ color: accent, depthTest: false }))
    outline.renderOrder = 11
    // Often empty: an empty geometry has no bounds to cull by.
    arcs.frustumCulled = false
    outline.frustumCulled = false
    scene.add(arcs, outline)
    three.current = { renderer, scene, camera, controls, world, arcs, outline, raycaster: new THREE.Raycaster(), accent, amber, dark }

    const resize = () => {
      const w = el.clientWidth || 1, h = el.clientHeight || 1
      renderer.setSize(w, h, false)
      renderer.domElement.style.width = `${w}px`
      renderer.domElement.style.height = `${h}px`
      camera.aspect = w / h
      camera.updateProjectionMatrix()
    }
    resize()
    const ro = new ResizeObserver(resize)
    ro.observe(el)

    let raf = 0
    let frame = 0
    const tmp = new THREE.Object3D()
    const loop = () => {
      raf = requestAnimationFrame(loop)
      frame++
      const now = performance.now()
      const d = drawn.current
      // The camera's flight to a node.
      const f = flight.current
      if (f) {
        const t = Math.min(1, (now - f.start) / 700)
        camera.position.lerpVectors(f.from, f.to, ease(t))
        controls.target.lerpVectors(f.tFrom, f.tTo, ease(t))
        if (t >= 1) flight.current = null
      }
      controls.update()
      // A rescan's tween, and the glow on what changed.
      const city = d.city
      if (city && city.tweenStart) {
        const t = Math.min(1, (now - city.tweenStart) / TWEEN_MS)
        const e = ease(t)
        for (let i = 0; i < city.boxes.length; i++) {
          const a = city.tweenFrom[i], b = city.tweenTo[i]
          const box = { x: lerp(a.x, b.x, e), z: lerp(a.z, b.z, e), w: lerp(a.w, b.w, e), d: lerp(a.d, b.d, e), base: lerp(a.base, b.base, e), h: lerp(a.h, b.h, e) }
          city.boxes[i] = box
          tmp.position.set(box.x, box.base, box.z)
          tmp.scale.set(Math.max(box.w, 0.001), Math.max(box.h, 0.001), Math.max(box.d, 0.001))
          tmp.updateMatrix()
          city.mesh.setMatrixAt(i, tmp.matrix)
        }
        city.mesh.instanceMatrix.needsUpdate = true
        city.mesh.computeBoundingSphere()
        if (t >= 1) city.tweenStart = 0
      }
      if (city && d.glowStart && props.current.changed.size) {
        const g = Math.min(1, (now - d.glowStart) / GLOW_MS)
        const pulse = (1 - g) * (0.55 + 0.45 * Math.sin(now / 160))
        const c = new THREE.Color()
        city.paths.forEach((p, i) => {
          if (!props.current.changed.has(p)) return
          c.copy(city.colors[i]).lerp(accent, Math.max(0, pulse))
          city.mesh.setColorAt(i, c)
        })
        if (city.mesh.instanceColor) city.mesh.instanceColor.needsUpdate = true
        if (g >= 1) { d.glowStart = 0; paintCity() }
      }
      // The graph settling.
      const graph = d.graph
      if (graph && graph.alpha > 0.015) {
        stepGraph(graph.layout, graph.alpha)
        graph.alpha *= 0.985
        placeGraph()
      }
      positionLayer()
      if (frame % 3 === 0) positionLabels()
      renderer.render(scene, camera)
    }
    raf = requestAnimationFrame(loop)
    return () => {
      cancelAnimationFrame(raf)
      ro.disconnect()
      controls.dispose()
      renderer.dispose()
      renderer.domElement.remove()
      three.current = null
    }
    // paintCity / placeGraph / positionLayer / positionLabels read refs only.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // ---- drawing ------------------------------------------------------------------------------

  function project(p: Point3): { x: number; y: number } | null {
    const t = three.current, el = host.current
    if (!t || !el) return null
    const v = new THREE.Vector3(p.x, p.y, p.z).project(t.camera)
    if (v.z > 1 || v.z < -1) return null
    return { x: (v.x * 0.5 + 0.5) * el.clientWidth, y: (-v.y * 0.5 + 0.5) * el.clientHeight }
  }

  function positionLayer() {
    const root = layer.current
    if (!root) return
    const placed: { el: HTMLElement; x: number; y: number; w: number; h: number }[] = []
    for (const el of root.querySelectorAll<HTMLElement>('[data-node]')) {
      const at = drawn.current.anchors.get(el.dataset.node ?? '')
      const s = at ? project(at) : null
      if (!s) { el.style.visibility = 'hidden'; continue }
      const w = el.offsetWidth, h = el.offsetHeight
      const place = el.dataset.place
      let x = s.x - w / 2, y = s.y - h - 8
      if (place === 'right') { x = s.x + 12; y = s.y - h / 2 }
      else if (place === 'bottom') { y = s.y + 10 }
      else if (place === 'over') { y = s.y - h / 2 }
      else if (place === 'corner') { x = s.x - w / 2; y = s.y - h - 4 }
      // Nudged apart: an item overlapping one already placed moves up past it.
      for (const p of placed) {
        if (x < p.x + p.w && x + w > p.x && y < p.y + p.h && y + h > p.y) y = p.y - h - 4
      }
      placed.push({ el, x, y, w, h })
      el.style.visibility = 'visible'
      el.style.transform = `translate(${Math.round(x)}px, ${Math.round(y)}px)`
    }
  }

  function positionLabels() {
    const root = labels.current, t = three.current, el = host.current
    const city = drawn.current.city
    if (!root || !t || !el) return
    const shown: { text: string; x: number; y: number }[] = []
    if (drawn.current.lens === 'city' && city) {
      for (const dist of city.layout.districts) {
        if (dist.depth === 0 || shown.length >= 40) continue
        const a = project({ x: dist.x - dist.w / 2, y: dist.base + dist.h, z: dist.z })
        const b = project({ x: dist.x + dist.w / 2, y: dist.base + dist.h, z: dist.z })
        if (!a || !b || Math.hypot(a.x - b.x, a.y - b.y) < 110 + dist.depth * 30) continue
        const c = project({ x: dist.x, y: dist.base + dist.h, z: dist.z + dist.d / 2 })
        if (c) shown.push({ text: nodeLabel(dirNode(dist.path)), x: c.x, y: c.y })
      }
    } else if (drawn.current.graph) {
      const nodes = [...drawn.current.graph.layout.nodes].sort((p, q) => q.lines - p.lines).slice(0, 16)
      for (const n of nodes) {
        const s = project({ x: n.x, y: n.y - n.r, z: n.z })
        if (s) shown.push({ text: n.path === '.' ? 'root' : n.path, x: s.x, y: s.y + 4 })
      }
    }
    while (root.children.length < shown.length) {
      const div = document.createElement('div')
      div.className = 'pointer-events-none absolute left-0 top-0 whitespace-nowrap rounded px-1.5 py-0.5 text-[11px] font-medium'
      div.style.background = 'color-mix(in srgb, var(--bg-elevated) 80%, transparent)'
      div.style.color = 'var(--text)'
      root.appendChild(div)
    }
    ;[...root.children].forEach((node, i) => {
      const div = node as HTMLElement
      const s = shown[i]
      if (!s) { div.style.display = 'none'; return }
      div.style.display = ''
      div.textContent = s.text
      div.style.transform = `translate(${Math.round(s.x - div.offsetWidth / 2)}px, ${Math.round(s.y + 2)}px)`
    })
  }

  function colorsFor(m: CodeModel, by: ColorBy): THREE.Color[] {
    const hex = by === 'recency' ? recencyColors(m.files) : m.files.map(f => langColor(f.lang))
    return hex.map(h => new THREE.Color(h))
  }

  /** The instance colours with the pick and hover applied. */
  function paintCity() {
    const city = drawn.current.city, t = three.current
    if (!city || !t) return
    const sel = new Set(props.current.selected)
    const c = new THREE.Color()
    const white = new THREE.Color('#ffffff')
    city.paths.forEach((p, i) => {
      c.copy(city.colors[i])
      if (sel.has(fileNode(p))) c.lerp(white, 0.45)
      city.mesh.setColorAt(i, c)
    })
    if (city.mesh.instanceColor) city.mesh.instanceColor.needsUpdate = true
  }

  function placeGraph() {
    const g = drawn.current.graph
    if (!g) return
    const tmp = new THREE.Object3D()
    const pos = g.links.geometry.getAttribute('position') as THREE.BufferAttribute
    g.layout.nodes.forEach((n, i) => {
      tmp.position.set(n.x, n.y, n.z)
      tmp.scale.setScalar(n.r)
      tmp.updateMatrix()
      g.mesh.setMatrixAt(i, tmp.matrix)
      drawn.current.anchors.set(n.id, { x: n.x, y: n.y + n.r, z: n.z })
    })
    g.layout.links.forEach((l, i) => {
      const a = g.layout.nodes[l.a], b = g.layout.nodes[l.b]
      pos.setXYZ(i * 2, a.x, a.y, a.z)
      pos.setXYZ(i * 2 + 1, b.x, b.y, b.z)
    })
    pos.needsUpdate = true
    g.mesh.instanceMatrix.needsUpdate = true
    g.mesh.computeBoundingSphere()
    g.links.geometry.computeBoundingSphere()
  }

  function clearWorld() {
    const t = three.current
    if (!t) return
    for (const child of [...t.world.children]) {
      t.world.remove(child)
      const mesh = child as THREE.Mesh
      mesh.geometry?.dispose()
      const mat = mesh.material as THREE.Material | THREE.Material[] | undefined
      if (Array.isArray(mat)) mat.forEach(m => m.dispose())
      else mat?.dispose()
    }
  }

  function frameAll(size: number, graph = false) {
    const t = three.current
    if (!t) return
    const s = Math.max(size, 20)
    t.controls.target.set(0, 0, 0)
    if (graph) t.camera.position.set(s * 0.9, s * 0.6, s * 1.1)
    else t.camera.position.set(s * 0.55, s * 0.62, s * 0.85)
    t.camera.near = Math.max(0.5, s / 2000)
    t.camera.far = s * 20
    t.camera.updateProjectionMatrix()
    const fog = t.scene.fog as THREE.Fog
    fog.near = s * 1.2
    fog.far = s * 4
    t.controls.maxDistance = s * 4
    t.controls.maxPolarAngle = graph ? Math.PI : Math.PI * 0.47
  }

  function buildCity(m: CodeModel, by: ColorBy, animate: boolean) {
    const t = three.current
    if (!t) return
    clearWorld()
    const layout = layoutCity(m)
    const colors = colorsFor(m, by)
    const prev = lastBoxes.current
    // Removed files sink where they stood, so the tween has somewhere to take them.
    const now = new Set(layout.buildings.map(b => b.path))
    const removed = animate ? [...prev.entries()].filter(([p]) => !now.has(p)) : []
    const list: (CityBuilding | { path: string; box: Box })[] = [...layout.buildings, ...removed.map(([path, box]) => ({ path, box }))]
    const geo = new THREE.BoxGeometry(1, 1, 1)
    geo.translate(0, 0.5, 0)
    const mat = new THREE.MeshStandardMaterial({ roughness: 0.62, metalness: 0.04 })
    const mesh = new THREE.InstancedMesh(geo, mat, Math.max(list.length, 1))
    mesh.count = list.length
    mesh.castShadow = true
    mesh.receiveShadow = true
    const tweenFrom: Box[] = [], tweenTo: Box[] = [], boxes: Box[] = [], paths: string[] = [], cols: THREE.Color[] = []
    const gray = new THREE.Color('#888888')
    for (const item of list) {
      if ('box' in item) {
        tweenFrom.push(item.box)
        tweenTo.push({ ...item.box, h: 0, w: item.box.w * 0.6, d: item.box.d * 0.6 })
        cols.push(gray)
      } else {
        const to: Box = { x: item.x, z: item.z, w: item.w, d: item.d, base: item.base, h: item.h }
        const before = animate ? prev.get(item.path) : undefined
        tweenFrom.push(before ?? (animate ? { ...to, h: 0 } : to))
        tweenTo.push(to)
        cols.push(colors[item.index])
      }
      boxes.push({ ...tweenFrom[tweenFrom.length - 1] })
      paths.push(item.path)
    }
    const tmp = new THREE.Object3D()
    boxes.forEach((b, i) => {
      tmp.position.set(b.x, b.base, b.z)
      tmp.scale.set(Math.max(b.w, 0.001), Math.max(b.h, 0.001), Math.max(b.d, 0.001))
      tmp.updateMatrix()
      mesh.setMatrixAt(i, tmp.matrix)
      mesh.setColorAt(i, cols[i])
    })
    mesh.computeBoundingSphere()
    // Districts: a slab per folder, a shade lighter (or darker) per level.
    const dgeo = new THREE.BoxGeometry(1, 1, 1)
    dgeo.translate(0, 0.5, 0)
    const dmat = new THREE.MeshStandardMaterial({ roughness: 0.9, metalness: 0 })
    const districts = new THREE.InstancedMesh(dgeo, dmat, Math.max(layout.districts.length, 1))
    districts.count = layout.districts.length
    districts.receiveShadow = true
    const base = new THREE.Color(t.dark ? '#2b3039' : '#d5d9e0')
    const step = new THREE.Color(t.dark ? '#4a5262' : '#f4f6f9')
    layout.districts.forEach((dist, i) => {
      tmp.position.set(dist.x, dist.base, dist.z)
      tmp.scale.set(dist.w, dist.h, dist.d)
      tmp.updateMatrix()
      districts.setMatrixAt(i, tmp.matrix)
      districts.setColorAt(i, base.clone().lerp(step, Math.min(dist.depth / 5, 1)))
    })
    districts.computeBoundingSphere()
    t.world.add(districts, mesh)
    // Shadows from a sun high to one side, covering the whole city.
    const sun = t.scene.children.find(c => c instanceof THREE.DirectionalLight) as THREE.DirectionalLight | undefined
    if (sun) {
      const s = layout.size
      sun.position.set(s * 0.4, s * 1.2, s * 0.25)
      sun.target.position.set(0, 0, 0)
      Object.assign(sun.shadow.camera, { left: -s * 0.75, right: s * 0.75, top: s * 0.75, bottom: -s * 0.75, near: 1, far: s * 3 })
      sun.shadow.camera.updateProjectionMatrix()
    }
    drawn.current.city = { layout, mesh, districts, boxes, paths, colors: cols, tweenFrom, tweenTo, tweenStart: animate ? performance.now() : 0, removedFrom: layout.buildings.length }
    drawn.current.graph = undefined
    drawn.current.anchors = layout.anchors
    drawn.current.lens = 'city'
    lastBoxes.current = new Map(layout.buildings.map(b => [b.path, { x: b.x, z: b.z, w: b.w, d: b.d, base: b.base, h: b.h }]))
    paintCity()
  }

  /** How far the graph reaches from its centre, to frame it whole. */
  function graphSize(): number {
    let reach = 20
    for (const n of drawn.current.graph?.layout.nodes ?? []) reach = Math.max(reach, Math.abs(n.x), Math.abs(n.y), Math.abs(n.z))
    return reach * 1.6
  }

  function buildGraph(m: CodeModel, by: ColorBy) {
    const t = three.current
    if (!t) return
    clearWorld()
    const layout = graphOf(m)
    // A folder's colour: its main language, or its newest file.
    const fileColors = by === 'recency' ? recencyColors(m.files) : m.files.map(f => langColor(f.lang))
    const best = new Map<string, { lines: number; color: string; mtime: number }>()
    m.files.forEach((f, i) => {
      const b = best.get(f.dir)
      const pick = by === 'recency' ? !b || f.mtime > b.mtime : !b || f.lines > b.lines
      if (pick) best.set(f.dir, { lines: f.lines, color: fileColors[i], mtime: f.mtime })
    })
    const colors = layout.nodes.map(n => new THREE.Color(best.get(n.path)?.color ?? '#999999'))
    const mesh = new THREE.InstancedMesh(new THREE.SphereGeometry(1, 24, 16), new THREE.MeshStandardMaterial({ roughness: 0.45, metalness: 0.05 }), Math.max(layout.nodes.length, 1))
    mesh.count = layout.nodes.length
    colors.forEach((c, i) => mesh.setColorAt(i, c))
    const lgeo = new THREE.BufferGeometry()
    lgeo.setAttribute('position', new THREE.BufferAttribute(new Float32Array(layout.links.length * 6), 3))
    const links = new THREE.LineSegments(lgeo, new THREE.LineBasicMaterial({ color: t.dark ? 0xb4bccb : 0x4b5463, transparent: true, opacity: 0.5 }))
    links.frustumCulled = false
    t.world.add(links, mesh)
    drawn.current.graph = { layout, mesh, links, colors, alpha: 1 }
    drawn.current.city = undefined
    drawn.current.anchors = new Map()
    drawn.current.lens = 'graph'
    // Settle most of the way before the first frame, then let the rest animate in.
    for (let i = 0; i < 60; i++) stepGraph(layout, 1)
    placeGraph()
  }

  /** Arcs for the picked files' imports, and an outline round each picked node. */
  function drawPickExtras() {
    const t = three.current
    if (!t) return
    const m = props.current.model
    const anchors = drawn.current.anchors
    const pts: number[] = [], cols: number[] = []
    const arc = (a: Point3, b: Point3, c: THREE.Color) => {
      const dist = Math.hypot(a.x - b.x, a.z - b.z)
      const mid = { x: (a.x + b.x) / 2, y: Math.max(a.y, b.y) + dist * 0.35 + 4, z: (a.z + b.z) / 2 }
      let prev = a
      for (let k = 1; k <= 16; k++) {
        const s = k / 16, u = 1 - s
        const p = { x: u * u * a.x + 2 * u * s * mid.x + s * s * b.x, y: u * u * a.y + 2 * u * s * mid.y + s * s * b.y, z: u * u * a.z + 2 * u * s * mid.z + s * s * b.z }
        pts.push(prev.x, prev.y, prev.z, p.x, p.y, p.z)
        cols.push(c.r, c.g, c.b, c.r, c.g, c.b)
        prev = p
      }
    }
    let count = 0
    const outline: number[] = []
    for (const id of props.current.selected) {
      if (id.startsWith('file:')) {
        const f = m.byPath.get(id.slice(5))
        if (!f) continue
        const from = drawn.current.lens === 'city' ? anchors.get(id) : anchors.get(dirNode(f.dir))
        if (!from) continue
        for (const [list, color] of [[m.imports[f.index], t.accent], [m.importedBy[f.index], t.amber]] as const) {
          for (const j of list) {
            if (count++ > 400) break
            const other = m.files[j]
            const to = drawn.current.lens === 'city' ? anchors.get(fileNode(other.path)) : anchors.get(dirNode(other.dir))
            if (to && to !== from) arc(from, to, color)
          }
        }
      }
      // An outline: the building's box, the district's rim, or a ring round the sphere.
      const city = drawn.current.city
      if (city && id.startsWith('file:')) {
        const i = city.paths.indexOf(id.slice(5))
        const b = i >= 0 ? city.tweenTo[i] : null
        if (b) boxEdges(outline, b.x, b.base, b.z, b.w * 1.06, b.h * 1.02, b.d * 1.06)
      } else if (city && id.startsWith('dir:')) {
        const dist = city.layout.districts.find(d => dirNode(d.path) === id)
        if (dist) boxEdges(outline, dist.x, dist.base, dist.z, dist.w, dist.h + 0.3, dist.d)
      }
    }
    const ageo = new THREE.BufferGeometry()
    ageo.setAttribute('position', new THREE.Float32BufferAttribute(pts, 3))
    ageo.setAttribute('color', new THREE.Float32BufferAttribute(cols, 3))
    t.arcs.geometry.dispose()
    t.arcs.geometry = ageo
    const ogeo = new THREE.BufferGeometry()
    ogeo.setAttribute('position', new THREE.Float32BufferAttribute(outline, 3))
    t.outline.geometry.dispose()
    t.outline.geometry = ogeo
    // The graph's picked spheres brighten instead of an outline.
    const g = drawn.current.graph
    if (g) {
      const sel = new Set(props.current.selected)
      const white = new THREE.Color('#ffffff')
      g.layout.nodes.forEach((n, i) => g.mesh.setColorAt(i, sel.has(n.id) ? g.colors[i].clone().lerp(white, 0.5) : g.colors[i]))
      if (g.mesh.instanceColor) g.mesh.instanceColor.needsUpdate = true
    }
  }

  function boxEdges(out: number[], x: number, base: number, z: number, w: number, h: number, d: number) {
    const xs = [x - w / 2, x + w / 2], ys = [base, base + h], zs = [z - d / 2, z + d / 2]
    const c = (i: number, j: number, k: number) => [xs[i], ys[j], zs[k]]
    const edges: [number[], number[]][] = []
    for (const j of [0, 1]) for (const k of [0, 1]) edges.push([c(0, j, k), c(1, j, k)])
    for (const i of [0, 1]) for (const k of [0, 1]) edges.push([c(i, 0, k), c(i, 1, k)])
    for (const i of [0, 1]) for (const j of [0, 1]) edges.push([c(i, j, 0), c(i, j, 1)])
    for (const [a, b] of edges) out.push(...a, ...b)
  }

  // Rebuild when the model, lens or colouring changes. The first draw of a lens
  // frames the camera; a rescan of the same lens animates in place.
  const builtFor = useRef<{ lens: Lens; sig: string; colorBy: ColorBy; root: string } | null>(null)
  useEffect(() => {
    if (!three.current) return
    const prev = builtFor.current
    const sameRoot = prev?.root === model.root
    if (lens === 'city') {
      buildCity(model, colorBy, !!prev && prev.lens === 'city' && sameRoot && prev.sig !== model.sig)
      if (!prev || prev.lens !== 'city' || !sameRoot) frameAll(drawn.current.city?.layout.size ?? 100)
    } else {
      buildGraph(model, colorBy)
      if (!prev || prev.lens !== 'graph' || !sameRoot) frameAll(graphSize(), true)
    }
    if (prev && prev.sig !== model.sig && sameRoot) drawn.current.glowStart = performance.now()
    builtFor.current = { lens, sig: model.sig, colorBy, root: model.root }
    setHover(null)
    drawPickExtras()
    // buildCity / buildGraph / frameAll / drawPickExtras read refs only.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [model, lens, colorBy])

  useEffect(() => {
    paintCity()
    drawPickExtras()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected])

  // ---- picking --------------------------------------------------------------------------------

  function pickAt(clientX: number, clientY: number): string | null {
    const t = three.current, el = host.current
    if (!t || !el) return null
    const r = el.getBoundingClientRect()
    const ndc = new THREE.Vector2(((clientX - r.left) / r.width) * 2 - 1, -((clientY - r.top) / r.height) * 2 + 1)
    t.raycaster.setFromCamera(ndc, t.camera)
    const d = drawn.current
    if (d.city) {
      const hit = t.raycaster.intersectObject(d.city.mesh, false)[0]
      if (hit?.instanceId !== undefined && hit.instanceId < d.city.removedFrom) return fileNode(d.city.paths[hit.instanceId])
      const dist = t.raycaster.intersectObject(d.city.districts, false)[0]
      if (dist?.instanceId !== undefined) {
        const node = d.city.layout.districts[dist.instanceId]
        return node && node.depth > 0 ? dirNode(node.path) : null
      }
    }
    if (d.graph) {
      const hit = t.raycaster.intersectObject(d.graph.mesh, false)[0]
      if (hit?.instanceId !== undefined) return d.graph.layout.nodes[hit.instanceId]?.id ?? null
    }
    return null
  }

  function flyTo(ids: string[]) {
    const t = three.current
    if (!t) return
    const pts = ids.map(id => drawn.current.anchors.get(id)).filter((p): p is Point3 => !!p)
    if (!pts.length) return
    const box = new THREE.Box3()
    for (const p of pts) box.expandByPoint(new THREE.Vector3(p.x, p.y, p.z))
    // A building is framed whole, from the ground to its roof: framing only its
    // anchor flies into the side of a tall one.
    const city = drawn.current.city
    for (const id of ids) {
      if (city && id.startsWith('file:')) {
        const i = city.paths.indexOf(id.slice(5))
        const b = i >= 0 ? city.tweenTo[i] : null
        if (b) box.expandByPoint(new THREE.Vector3(b.x - b.w / 2, b.base, b.z - b.d / 2)).expandByPoint(new THREE.Vector3(b.x + b.w / 2, b.base + b.h, b.z + b.d / 2))
      } else if (city && id.startsWith('dir:')) {
        const d = city.layout.districts.find(x => dirNode(x.path) === id)
        if (d) box.expandByPoint(new THREE.Vector3(d.x - d.w / 2, d.base, d.z - d.d / 2)).expandByPoint(new THREE.Vector3(d.x + d.w / 2, d.base + d.h, d.z + d.d / 2))
      }
    }
    const center = box.getCenter(new THREE.Vector3())
    const radius = Math.max(box.getSize(new THREE.Vector3()).length() / 2, 15)
    const fov = (t.camera.fov * Math.PI) / 180
    const distance = (radius / Math.sin(fov / 2)) * 1.25
    const dir = t.camera.position.clone().sub(t.controls.target).normalize()
    flight.current = {
      from: t.camera.position.clone(), to: center.clone().add(dir.multiplyScalar(distance)),
      tFrom: t.controls.target.clone(), tTo: center, start: performance.now(),
    }
  }

  apiRef.current = {
    focus: flyTo,
    fit: () => {
      const d = drawn.current
      if (d.city) frameAll(d.city.layout.size)
      else frameAll(graphSize(), true)
    },
    screenOf: id => { const p = drawn.current.anchors.get(id); return p ? project(p) : null },
  }

  const down = useRef<{ x: number; y: number } | null>(null)
  const hoverTimer = useRef(0)

  const info = (id: string) => {
    if (id.startsWith('file:')) {
      const f = model.byPath.get(id.slice(5))
      return f ? `${f.path} · ${f.lines} lines · imports ${model.imports[f.index].length} · imported by ${model.importedBy[f.index].length}` : id
    }
    const d = model.dirs.get(id.slice(4))
    return d ? `${d.path}/ · ${d.files} files · ${d.lines} lines` : id
  }

  return (
    <div
      ref={host}
      className="absolute inset-0 overflow-hidden"
      data-testid="codebase-scene"
      onPointerDown={e => { down.current = { x: e.clientX, y: e.clientY } }}
      onPointerUp={e => {
        const start = down.current
        down.current = null
        if (!start || Math.hypot(e.clientX - start.x, e.clientY - start.y) > 4) return
        if ((e.target as HTMLElement).closest('[data-scene-ui]')) return
        props.current.onPick(pickAt(e.clientX, e.clientY), e.shiftKey)
      }}
      onDoubleClick={e => {
        if ((e.target as HTMLElement).closest('[data-scene-ui]')) return
        const id = pickAt(e.clientX, e.clientY)
        if (id) flyTo([id])
      }}
      onPointerMove={e => {
        if (down.current || (e.target as HTMLElement).closest('[data-scene-ui]')) { setHover(null); return }
        const x = e.clientX, y = e.clientY
        window.clearTimeout(hoverTimer.current)
        hoverTimer.current = window.setTimeout(() => {
          const r = host.current?.getBoundingClientRect()
          const id = pickAt(x, y)
          setHover(id && r ? { id, x: x - r.left, y: y - r.top } : null)
        }, 40)
      }}
      onPointerLeave={() => { window.clearTimeout(hoverTimer.current); setHover(null) }}
    >
      {failed && (
        <div className="absolute inset-0 flex items-center justify-center text-sm text-muted">3D drawing is not available in this browser.</div>
      )}
      <div ref={labels} className="pointer-events-none absolute inset-0" />
      <div ref={layer} className="pointer-events-none absolute inset-0 [&>*]:pointer-events-auto [&>*]:absolute [&>*]:left-0 [&>*]:top-0 [&>*]:invisible" data-testid="codebase-layer">
        {children}
      </div>
      {hover && (
        <div className="pointer-events-none absolute max-w-[420px] truncate rounded-md border border-border bg-bg-elevated px-2 py-1 text-xs text-text shadow-md"
          style={{ left: hover.x + 14, top: hover.y + 14, zIndex: 40 }} data-testid="codebase-hover">
          {info(hover.id)}
        </div>
      )}
    </div>
  )
}
