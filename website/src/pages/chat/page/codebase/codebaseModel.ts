/**
 * A codebase session's model (PoC): the session file, the scan it draws, and the
 * layouts of its lenses. Pure data, no three.js, so it is cheap to import and test.
 *
 * - The session file (a doc's file, JSON) keeps where the model points (`root`),
 *   how it is drawn (`lens`, `colorBy`) and the conductor asks pinned to nodes.
 * - The scan (`/api/codebase/scan`) is every file with its lines, language and
 *   mtime, plus import edges.
 * - A node is a file (`file:<path>`) or a folder (`dir:<path>`, `dir:.` for the
 *   root). Asks, overlay anchors and selection all name nodes.
 * - City: folders are districts (a squarified treemap, raised per depth) and files
 *   are buildings: footprint and height grow with line count, so a long file is a
 *   tower you cannot miss.
 * - Graph: folders as spheres, joined where their files import each other, laid
 *   out by a small force simulation.
 */
import type { CodebaseScanResponse } from '../../../../api/client/chat'

export const CODEBASE_ID_PREFIX = 'c0deb'
export const isCodebaseId = (docId: string) => docId.startsWith(CODEBASE_ID_PREFIX)

export type Lens = 'city' | 'graph'
export type ColorBy = 'language' | 'recency'
export interface CodebaseAsk { id: string; nodeIds: string[] }
export interface CodebaseDoc {
  kind: 'kirocrew-codebase'
  version: 1
  /** The work tree drawn: the session's project at first, a worktree once the conductor makes one. */
  root: string
  lens: Lens
  colorBy: ColorBy
  asks: CodebaseAsk[]
}

export const emptyCodebaseDoc = (): CodebaseDoc => ({ kind: 'kirocrew-codebase', version: 1, root: '', lens: 'city', colorBy: 'language', asks: [] })

export function parseCodebaseDoc(raw: string): CodebaseDoc {
  const doc = emptyCodebaseDoc()
  let data: unknown
  try { data = JSON.parse(raw) } catch { return doc }
  if (!data || typeof data !== 'object') return doc
  const d = data as Record<string, unknown>
  if (typeof d.root === 'string') doc.root = d.root
  if (d.lens === 'city' || d.lens === 'graph') doc.lens = d.lens
  if (d.colorBy === 'language' || d.colorBy === 'recency') doc.colorBy = d.colorBy
  if (Array.isArray(d.asks)) {
    doc.asks = d.asks.filter((a): a is CodebaseAsk =>
      !!a && typeof a === 'object' && typeof (a as CodebaseAsk).id === 'string' && Array.isArray((a as CodebaseAsk).nodeIds)
      && (a as CodebaseAsk).nodeIds.every(n => typeof n === 'string'))
  }
  return doc
}

export const serializeCodebaseDoc = (d: CodebaseDoc) => JSON.stringify(d, null, 1)

// ---- the scan ------------------------------------------------------------------------

// The scan's wire shape lives with its endpoint; re-exported for the model's readers.
export type { CodebaseScanResponse }

export interface FileNode { index: number; path: string; dir: string; lines: number; lang: string; mtime: number }
export interface DirInfo { path: string; files: number; lines: number }

export interface CodeModel {
  root: string
  branch: string
  head: string
  sig: string
  truncated: boolean
  files: FileNode[]
  byPath: Map<string, FileNode>
  /** Per file index: the files it imports, and the files importing it. */
  imports: number[][]
  importedBy: number[][]
  /** Every folder (`.` for the root) with its total files and lines, nested ones included. */
  dirs: Map<string, DirInfo>
}

export const dirOf = (path: string) => { const i = path.lastIndexOf('/'); return i < 0 ? '.' : path.slice(0, i) }
export const fileNode = (path: string) => `file:${path}`
export const dirNode = (path: string) => `dir:${path || '.'}`

export function decodeScan(r: CodebaseScanResponse): CodeModel {
  const langs = r.langs ?? []
  const files: FileNode[] = (r.files ?? []).map(([path, lines, li, mtime], index) => ({ index, path, dir: dirOf(path), lines, lang: langs[li] ?? 'other', mtime }))
  const imports: number[][] = files.map(() => [])
  const importedBy: number[][] = files.map(() => [])
  for (const [a, b] of r.edges ?? []) {
    if (!files[a] || !files[b]) continue
    imports[a].push(b)
    importedBy[b].push(a)
  }
  const dirs = new Map<string, DirInfo>()
  const bump = (path: string, lines: number) => {
    const d = dirs.get(path) ?? { path, files: 0, lines: 0 }
    d.files += 1; d.lines += lines
    dirs.set(path, d)
  }
  for (const f of files) {
    bump('.', f.lines)
    const parts = f.path.split('/')
    for (let k = 1; k < parts.length; k++) bump(parts.slice(0, k).join('/'), f.lines)
  }
  return {
    root: r.root ?? '', branch: r.branch ?? '', head: r.head ?? '', sig: r.sig, truncated: !!r.truncated,
    files, byPath: new Map(files.map(f => [f.path, f])), imports, importedBy, dirs,
  }
}

// ---- colours ---------------------------------------------------------------------------

export const LANG_COLOR: Record<string, string> = {
  python: '#5B8DEF', typescript: '#2FB682', javascript: '#E8C547', css: '#D77FD5', html: '#EF8354',
  markdown: '#A0AEC0', json: '#C49A6C', yaml: '#9B87D6', shell: '#86B95A', config: '#94A3B8',
  rust: '#E0915C', go: '#3DB8D8', java: '#D9645A', kotlin: '#B07FE8', swift: '#F07A4A', ruby: '#D4515D',
  c: '#7C93B5', cpp: '#5F7FB8', image: '#E8A1A6', other: '#B8BEC8', generated: '#5B616B',
}
export const langColor = (lang: string) => LANG_COLOR[lang] ?? LANG_COLOR.other

/** Older files cool, the newest hot: by rank, so one ancient file cannot flatten the scale. */
export function recencyColors(files: FileNode[]): string[] {
  const order = files.map(f => f.index).sort((a, b) => files[a].mtime - files[b].mtime)
  const rank = new Array<number>(files.length)
  order.forEach((idx, r) => { rank[idx] = files.length > 1 ? r / (files.length - 1) : 1 })
  const cold = [0x6b, 0x7a, 0x99], warm = [0xe8, 0xb2, 0x4a], hot = [0xf0, 0x55, 0x3c]
  return files.map(f => {
    const t = rank[f.index]
    const [a, b, u] = t < 0.85 ? [cold, warm, t / 0.85] : [warm, hot, (t - 0.85) / 0.15]
    const c = a.map((v, i) => Math.round(v + (b[i] - v) * u))
    return `#${c.map(v => v.toString(16).padStart(2, '0')).join('')}`
  })
}

// ---- city -------------------------------------------------------------------------------

export interface Rect { x: number; z: number; w: number; d: number }
export interface CityBuilding { index: number; path: string; x: number; z: number; w: number; d: number; base: number; h: number }
export interface CityDistrict { path: string; depth: number; x: number; z: number; w: number; d: number; base: number; h: number }
export interface Point3 { x: number; y: number; z: number }
export interface CityLayout {
  buildings: CityBuilding[]
  districts: CityDistrict[]
  size: number
  /** The top centre of each node, where its overlay items, answers and arcs attach. */
  anchors: Map<string, Point3>
}

const MIN_WEIGHT = 12
const SLAB = 1.4
const PAD = 1.6

/** How tall a file's building is: grows faster than its footprint, so long files stand out. */
export const buildingHeight = (lines: number) => 1.5 + 0.35 * Math.pow(Math.max(lines, 1), 0.8)

/** A file's weight in the treemap. A generated file (a lock file, a locale catalogue)
 *  stays a low, small block whatever its length: it is not a shape to refactor. */
const GENERATED_LINES = 40
export const drawnLines = (f: FileNode) => f.lang === 'generated' ? Math.min(f.lines, GENERATED_LINES) : f.lines

/** Squarified treemap: rects for *weights* (sorted descending) filling *rect*. */
export function squarify(weights: number[], rect: Rect): Rect[] {
  const total = weights.reduce((s, w) => s + w, 0)
  if (!weights.length || total <= 0 || rect.w <= 0 || rect.d <= 0) return weights.map(() => ({ ...rect, w: 0, d: 0 }))
  const scale = (rect.w * rect.d) / total
  const areas = weights.map(w => w * scale)
  const out: Rect[] = []
  const r = { ...rect }
  let row: number[] = []
  const worst = (rw: number[], s: number) => {
    const sum = rw.reduce((a, b) => a + b, 0)
    const max = Math.max(...rw), min = Math.min(...rw)
    return Math.max((s * s * max) / (sum * sum), (sum * sum) / (s * s * min))
  }
  const place = (rw: number[]) => {
    const sum = rw.reduce((a, b) => a + b, 0)
    if (r.w >= r.d) {
      const colW = sum / r.d
      let z = r.z
      for (const a of rw) { const h = a / colW; out.push({ x: r.x, z, w: colW, d: h }); z += h }
      r.x += colW; r.w -= colW
    } else {
      const rowD = sum / r.w
      let x = r.x
      for (const a of rw) { const w = a / rowD; out.push({ x, z: r.z, w, d: rowD }); x += w }
      r.z += rowD; r.d -= rowD
    }
  }
  let i = 0
  while (i < areas.length) {
    const s = Math.min(r.w, r.d)
    if (!row.length || worst([...row, areas[i]], s) <= worst(row, s)) { row.push(areas[i]); i++ }
    else { place(row); row = [] }
  }
  if (row.length) place(row)
  return out
}

interface DirTree { path: string; dirs: Map<string, DirTree>; files: FileNode[]; weight: number }

function buildTree(files: FileNode[]): DirTree {
  const root: DirTree = { path: '.', dirs: new Map(), files: [], weight: 0 }
  for (const f of files) {
    let node = root
    const parts = f.path.split('/')
    for (let k = 0; k < parts.length - 1; k++) {
      const path = parts.slice(0, k + 1).join('/')
      let next = node.dirs.get(parts[k])
      if (!next) { next = { path, dirs: new Map(), files: [], weight: 0 }; node.dirs.set(parts[k], next) }
      node = next
    }
    node.files.push(f)
  }
  const weigh = (n: DirTree): number => {
    let w = n.files.reduce((s, f) => s + Math.max(drawnLines(f), MIN_WEIGHT), 0)
    for (const c of n.dirs.values()) w += weigh(c)
    // Room for the district's own border, so small folders are not squeezed flat.
    n.weight = w * 1.12 + 60
    return n.weight
  }
  weigh(root)
  return root
}

export function layoutCity(model: CodeModel): CityLayout {
  const tree = buildTree(model.files)
  const size = Math.sqrt(tree.weight)
  const buildings: CityBuilding[] = []
  const districts: CityDistrict[] = []
  const anchors = new Map<string, Point3>()
  const visit = (node: DirTree, rect: Rect, depth: number) => {
    const base = depth * SLAB
    districts.push({ path: node.path, depth, x: rect.x + rect.w / 2, z: rect.z + rect.d / 2, w: rect.w, d: rect.d, base, h: SLAB })
    anchors.set(dirNode(node.path), { x: rect.x + rect.w / 2, y: base + SLAB, z: rect.z + rect.d / 2 })
    const pad = Math.min(PAD, rect.w / 6, rect.d / 6)
    const inner = { x: rect.x + pad, z: rect.z + pad, w: rect.w - 2 * pad, d: rect.d - 2 * pad }
    const items: ({ dir: DirTree } | { file: FileNode })[] = [
      ...[...node.dirs.values()].map(dir => ({ dir })),
      ...node.files.map(file => ({ file })),
    ]
    const weight = (it: (typeof items)[number]) => 'dir' in it ? it.dir.weight : Math.max(drawnLines(it.file), MIN_WEIGHT)
    // Ties break on the path in byte order, so the layout is the same in every
    // locale; a collator would order '/', '.' and case by the host's rules.
    const pathOf = (it: (typeof items)[number]) => 'dir' in it ? it.dir.path : it.file.path
    items.sort((a, b) => weight(b) - weight(a) || (pathOf(a) < pathOf(b) ? -1 : pathOf(a) > pathOf(b) ? 1 : 0))
    const rects = squarify(items.map(weight), inner)
    items.forEach((it, k) => {
      const r = rects[k]
      if ('dir' in it) { visit(it.dir, r, depth + 1); return }
      const gap = Math.min(r.w, r.d) * 0.14
      const w = Math.max(r.w - 2 * gap, 0.2), d = Math.max(r.d - 2 * gap, 0.2)
      const top = base + SLAB
      const h = buildingHeight(drawnLines(it.file))
      const x = r.x + r.w / 2, z = r.z + r.d / 2
      buildings.push({ index: it.file.index, path: it.file.path, x, z, w, d, base: top, h })
      anchors.set(fileNode(it.file.path), { x, y: top + h, z })
    })
  }
  visit(tree, { x: -size / 2, z: -size / 2, w: size, d: size }, 0)
  return { buildings, districts, size, anchors }
}

// ---- graph ------------------------------------------------------------------------------

export interface GraphNode { id: string; path: string; lines: number; files: number; r: number; x: number; y: number; z: number; vx: number; vy: number; vz: number }
export interface GraphLayout { nodes: GraphNode[]; links: { a: number; b: number; weight: number }[] }

/** The furthest a node moves in one step of the layout. */
const MAX_STEP = 8

/** A deterministic start position per path, so the graph settles the same way each time. */
function seeded(path: string, salt: number): number {
  let h = 2166136261 ^ salt
  for (let i = 0; i < path.length; i++) h = Math.imul(h ^ path.charCodeAt(i), 16777619)
  return ((h >>> 0) / 4294967295) * 2 - 1
}

/** Folders holding files, joined where their files import each other. */
export function graphOf(model: CodeModel): GraphLayout {
  const own = new Map<string, { lines: number; files: number }>()
  for (const f of model.files) {
    const o = own.get(f.dir) ?? { lines: 0, files: 0 }
    o.lines += f.lines; o.files += 1
    own.set(f.dir, o)
  }
  // Only folders that import or are imported: the rest (docs, fixtures, assets)
  // have no relationships to show and would bury the ones that do.
  const linked = new Set<string>()
  model.files.forEach((f, i) => {
    for (const t of model.imports[i]) {
      const other = model.files[t].dir
      if (other !== f.dir) { linked.add(f.dir); linked.add(other) }
    }
  })
  const ids = [...own.keys()].filter(p => !linked.size || linked.has(p)).sort()
  const at = new Map(ids.map((p, i) => [p, i]))
  const spread = 18 * Math.cbrt(ids.length + 1)
  const nodes: GraphNode[] = ids.map(path => {
    const o = own.get(path)!
    return {
      id: dirNode(path), path, lines: o.lines, files: o.files, r: 1.2 + Math.cbrt(o.lines) * 0.45,
      x: seeded(path, 1) * spread, y: seeded(path, 2) * spread, z: seeded(path, 3) * spread, vx: 0, vy: 0, vz: 0,
    }
  })
  const weights = new Map<string, number>()
  model.files.forEach((f, i) => {
    for (const t of model.imports[i]) {
      const a = at.get(f.dir), b = at.get(model.files[t].dir)
      if (a === undefined || b === undefined || a === b) continue
      const key = a < b ? `${a},${b}` : `${b},${a}`
      weights.set(key, (weights.get(key) ?? 0) + 1)
    }
  })
  const links = [...weights].map(([k, weight]) => { const [a, b] = k.split(',').map(Number); return { a, b, weight } })
  return { nodes, links }
}

/** One step of the force layout; returns how much the nodes moved, to know when it has settled. */
export function stepGraph(g: GraphLayout, alpha: number): number {
  const n = g.nodes
  const deg = n.map(() => 0)
  for (const l of g.links) { deg[l.a]++; deg[l.b]++ }
  const repel = 260
  for (let i = 0; i < n.length; i++) {
    const a = n[i]
    for (let j = i + 1; j < n.length; j++) {
      const b = n[j]
      let dx = a.x - b.x, dy = a.y - b.y, dz = a.z - b.z
      let d2 = dx * dx + dy * dy + dz * dz
      if (d2 < 0.01) { dx = 0.1; dy = 0; dz = 0; d2 = 0.01 }
      if (d2 > 90000) continue
      const f = (repel * alpha) / d2
      const inv = 1 / Math.sqrt(d2)
      a.vx += dx * inv * f; a.vy += dy * inv * f; a.vz += dz * inv * f
      b.vx -= dx * inv * f; b.vy -= dy * inv * f; b.vz -= dz * inv * f
    }
  }
  for (const l of g.links) {
    const a = n[l.a], b = n[l.b]
    const dx = b.x - a.x, dy = b.y - a.y, dz = b.z - a.z
    const d = Math.sqrt(dx * dx + dy * dy + dz * dz) || 0.01
    const rest = 14 + a.r + b.r
    // Divided by the busier end's link count, as d3-force does: a hub with
    // hundreds of links would otherwise sum hundreds of pulls and fly apart.
    const k = (0.3 * Math.min(1 + Math.log(l.weight), 4) * alpha) / Math.min(deg[l.a], deg[l.b]) / 4
    const f = (d - rest) * k / d
    a.vx += dx * f; a.vy += dy * f; a.vz += dz * f
    b.vx -= dx * f; b.vy -= dy * f; b.vz -= dz * f
  }
  let moved = 0
  for (const p of n) {
    p.vx -= p.x * 0.004 * alpha; p.vy -= p.y * 0.004 * alpha; p.vz -= p.z * 0.004 * alpha
    p.vx *= 0.6; p.vy *= 0.6; p.vz *= 0.6
    // A cap on one step's travel, so no force can launch a node off the map.
    const speed = Math.hypot(p.vx, p.vy, p.vz)
    if (speed > MAX_STEP) { const s = MAX_STEP / speed; p.vx *= s; p.vy *= s; p.vz *= s }
    p.x += p.vx; p.y += p.vy; p.z += p.vz
    moved += Math.abs(p.vx) + Math.abs(p.vy) + Math.abs(p.vz)
  }
  return n.length ? moved / n.length : 0
}

// ---- what the conductor reads -------------------------------------------------------------

const top = <T,>(items: T[], n: number, by: (t: T) => number) => [...items].sort((a, b) => by(b) - by(a)).slice(0, n)

/** The whole codebase in a few dozen lines: the conductor reads the files themselves. */
export function summarizeCodebase(m: CodeModel): string {
  const total = m.dirs.get('.')?.lines ?? 0
  const dirs = [...m.dirs.values()].filter(d => d.path !== '.' && d.path.split('/').length <= 2)
  const lines = [
    `Root: ${m.root} (branch ${m.branch || '?'} at ${m.head || 'no commit'}). ${m.files.length} files, ${total} lines${m.truncated ? ' (the tree is larger; only the first files are drawn)' : ''}.`,
    'Largest folders (files, lines):',
    ...top(dirs, 20, d => d.lines).map(d => `- ${d.path}/ (${d.files}, ${d.lines})`),
    'Tallest buildings, the longest files (lines, imports out / in):',
    ...top(m.files, 25, f => f.lines).map(f => `- ${f.path} (${f.lines}, ${m.imports[f.index].length} / ${m.importedBy[f.index].length})`),
    'Most imported files:',
    ...top(m.files, 10, f => m.importedBy[f.index].length).map(f => `- ${f.path} (imported by ${m.importedBy[f.index].length})`),
  ]
  return lines.join('\n')
}

const listPaths = (m: CodeModel, idx: number[], n: number) =>
  idx.length ? `${idx.slice(0, n).map(i => m.files[i].path).join(', ')}${idx.length > n ? ` (+${idx.length - n} more)` : ''}` : 'none'

/** What the person pointed at, with what the model knows about each node. */
export function describeNodes(m: CodeModel, nodeIds: string[]): string {
  return nodeIds.map(id => {
    if (id.startsWith('file:')) {
      const f = m.byPath.get(id.slice(5))
      if (!f) return `- ${id}: no longer in the tree`
      return `- ${id}: ${f.lines} lines of ${f.lang}. Imports: ${listPaths(m, m.imports[f.index], 12)}. Imported by: ${listPaths(m, m.importedBy[f.index], 12)}.`
    }
    const path = id.slice(4)
    const d = m.dirs.get(path)
    if (!d) return `- ${id}: no longer in the tree`
    const inside = m.files.filter(f => path === '.' || f.path.startsWith(`${path}/`))
    return `- ${id}: folder, ${d.files} files, ${d.lines} lines. Largest: ${top(inside, 8, f => f.lines).map(f => `${f.path} (${f.lines})`).join(', ')}.`
  }).join('\n')
}

/** The short label of a node, for chips and tooltips. */
export const nodeLabel = (id: string) => {
  const path = id.replace(/^(file|dir):/, '')
  if (path === '.') return 'root'
  const parts = path.split('/')
  // A folder keeps its parent's name too: two `test/` folders must read apart.
  return id.startsWith('dir:') ? `${parts.slice(-2).join('/')}/` : parts[parts.length - 1]
}

// ---- what the conductor changes ------------------------------------------------------------

const ROOT_RE = /```codebase-root[^\n]*\n([\s\S]*?)\n```/g
const FOCUS_RE = /```codebase-focus[^\n]*\n([\s\S]*?)\n```/g

/** The last absolute path a reply's `codebase-root` blocks name, if any. */
export function rootIn(text: string): string | null {
  let out: string | null = null
  for (const m of text.matchAll(ROOT_RE)) {
    const path = m[1].trim()
    if (path.startsWith('/') && !path.includes('\n')) out = path
  }
  return out
}

/** The nodes a reply's last `codebase-focus` block names. */
export function focusIn(text: string): string[] | null {
  let out: string[] | null = null
  for (const m of text.matchAll(FOCUS_RE)) {
    try {
      const v: unknown = JSON.parse(m[1])
      if (Array.isArray(v)) out = v.filter((x): x is string => typeof x === 'string' && /^(file|dir):/.test(x))
    } catch { /* not JSON: ignored */ }
  }
  return out
}
