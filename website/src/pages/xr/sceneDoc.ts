/**
 * The scene document: what the world holds, as data. It sits between the
 * Crew's data and the renderer. The renderer draws whatever the document
 * says and nothing else, so changing the document changes the world at once,
 * with no reload and no new code. A patch is the only way to change it.
 *
 * The interaction layer (zoom, point and talk, pages, the press route) does
 * not read a scene's contents. It works on Bots, zones and pages, so it is
 * the same in every scene, the empty one included.
 *
 * Shape, kept small on purpose so it can be merged with the format the XR
 * Bot's tools will use:
 *
 *   SceneDoc   { id, env, zones[], objects[], pages[] }
 *   SceneZone  { id, owner, cell }            a Bot's own ground, by spiral cell (`land.ts`)
 *   SceneObject{ id, kind, anchor, zone, pos, yaw, scale, label, bind, page, more }
 *   Binding    { bot, field }                 ties an object to one real field of one Bot
 *
 * An object is drawn by its `kind`, from a fixed list: a document can pick a
 * kind, it cannot bring code. `anchor` says what the object holds on to:
 * `land` objects stand in their zone and scale with the land; `seat` objects
 * stay around the person and never scale. Text in a document (`label`) is
 * shown as text, never run.
 *
 * `boundValue` is the one place that reads a Bot's field for an object.
 */
import { plainNumber, type XrGlance } from './glance'
import type { PlotMap } from './land'
import type { XrBot } from './model'
import type { PageKind, PageRec } from './pages'
import { plotPlan, reportKey } from './plots'

export const OBJECT_KINDS = ['home', 'work', 'ask', 'goal', 'clock', 'built', 'block', 'marker', 'screen'] as const
export type ObjectKind = (typeof OBJECT_KINDS)[number]
export type Anchor = 'land' | 'seat'

export interface Binding { bot: string; field: string }

export interface SceneObject {
  id: string
  kind: ObjectKind
  anchor: Anchor
  /** The zone it stands in ('' for none). A `land` object's `pos` is measured from the zone's middle. */
  zone: string
  /** Metres: x to the right, y up, z toward the person. */
  pos: [number, number, number]
  /** Degrees, turning to the left seen from above. */
  yaw: number
  scale: number
  label: string
  bind: Binding | null
  /** The page a pinch on it pulls out, or null. */
  page: { kind: PageKind; slot: string } | null
  /** How many more things like it are not drawn. */
  more: number
}

export interface SceneZone { id: string; owner: string; cell: number }
export interface SceneEnv { ground: 'none' | 'grid'; horizon: boolean }

export interface SceneDoc {
  id: string
  env: SceneEnv
  zones: SceneZone[]
  objects: SceneObject[]
  /** The pages a scene opens with, when the person has laid out none of their own. */
  pages: PageRec[]
}

export type ScenePatch =
  | { op: 'add'; object: Partial<SceneObject> & { id: string; kind: string } }
  | { op: 'set'; id: string; pos?: readonly number[]; yaw?: number; scale?: number; label?: string; kind?: string; zone?: string; anchor?: string }
  | { op: 'bind'; id: string; bind: Binding | null }
  | { op: 'remove'; id: string }
  | { op: 'env'; env: Partial<SceneEnv> }

/** Limits a document is held to, whoever wrote it. */
export const SCENE_MAX_OBJECTS = 400
const ID_MAX = 120
const LABEL_MAX = 200
const REACH_M = 60

const isKind = (k: unknown): k is ObjectKind => (OBJECT_KINDS as readonly string[]).includes(String(k))
const isAnchor = (a: unknown): a is Anchor => a === 'land' || a === 'seat'
const isId = (v: unknown): v is string => typeof v === 'string' && v.length > 0 && v.length <= ID_MAX
const fin = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)
const clampM = (v: number): number => Math.max(-REACH_M, Math.min(REACH_M, v))

function vec3(v: unknown): [number, number, number] | null {
  if (!Array.isArray(v) || v.length !== 3 || !v.every(fin)) return null
  return [clampM(v[0] as number), clampM(v[1] as number), clampM(v[2] as number)]
}

function binding(b: unknown): Binding | null | undefined {
  if (b === null) return null
  const v = b as Partial<Binding> | undefined
  return v && isId(v.bot) && isId(v.field) ? { bot: v.bot, field: v.field } : undefined
}

const scaleOf = (v: unknown): number | null => (fin(v) && v > 0 ? Math.max(0.05, Math.min(20, v)) : null)

/** The page a pinch on an object pulls out: one of the dashboard's own surfaces, by kind. Undefined when it is not one. */
function pageOf(v: unknown): SceneObject['page'] | undefined {
  const p = v as { kind?: unknown; slot?: unknown } | undefined
  if (!p || !['chat', 'dash', 'work', 'hub'].includes(String(p.kind))) return undefined
  return { kind: p.kind as PageKind, slot: typeof p.slot === 'string' ? p.slot.slice(0, 200) : '' }
}

/** Where the person's real screen is taken to be until they move it: to the right of the desk, turned to the seat. */
const SCREEN: SceneObject = {
  id: 'screen', kind: 'screen', anchor: 'seat', zone: '', pos: [0.52, -0.24, -0.6], yaw: -40, scale: 1, label: '', bind: null, page: { kind: 'hub', slot: '' }, more: 0,
}

export interface PatchResult { doc: SceneDoc; ok: boolean; why: string }

/**
 * Apply one patch. A patch that is not well formed, names an object that is
 * not there, or would break a limit changes nothing and says why. The
 * document passed in is never changed: a new one comes back.
 */
export function applyPatch(doc: SceneDoc, p: ScenePatch): PatchResult {
  const no = (why: string): PatchResult => ({ doc, ok: false, why })
  const at = 'id' in p ? doc.objects.findIndex(o => o.id === p.id) : -1
  const swap = (o: SceneObject): PatchResult => ({ doc: { ...doc, objects: doc.objects.map((x, i) => (i === at ? o : x)) }, ok: true, why: '' })
  switch (p.op) {
    case 'add': {
      const o = p.object
      if (!o || !isId(o.id)) return no('bad id')
      if (!isKind(o.kind)) return no('unknown kind')
      if (doc.objects.some(x => x.id === o.id)) return no('id taken')
      if (doc.objects.length >= SCENE_MAX_OBJECTS) return no('too many objects')
      const pos = o.pos === undefined ? [0, 0, 0] as [number, number, number] : vec3(o.pos)
      const bind = o.bind === undefined ? null : binding(o.bind)
      const page = o.page === undefined || o.page === null ? null : pageOf(o.page)
      if (!pos) return no('bad pos')
      if (bind === undefined) return no('bad bind')
      if (page === undefined) return no('bad page')
      const object: SceneObject = {
        id: o.id, kind: o.kind, anchor: isAnchor(o.anchor) ? o.anchor : 'land', zone: typeof o.zone === 'string' ? o.zone.slice(0, ID_MAX) : '',
        pos, yaw: fin(o.yaw) ? o.yaw : 0, scale: scaleOf(o.scale) ?? 1, label: typeof o.label === 'string' ? o.label.slice(0, LABEL_MAX) : '',
        bind, page, more: 0,
      }
      return { doc: { ...doc, objects: [...doc.objects, object] }, ok: true, why: '' }
    }
    case 'set': {
      if (at < 0) return no('no such object')
      const cur = doc.objects[at]
      const next = { ...cur }
      if (p.pos !== undefined) { const v = vec3(p.pos); if (!v) return no('bad pos'); next.pos = v }
      if (p.yaw !== undefined) { if (!fin(p.yaw)) return no('bad yaw'); next.yaw = p.yaw }
      if (p.scale !== undefined) { const v = scaleOf(p.scale); if (v === null) return no('bad scale'); next.scale = v }
      if (p.label !== undefined) { if (typeof p.label !== 'string') return no('bad label'); next.label = p.label.slice(0, LABEL_MAX) }
      if (p.kind !== undefined) { if (!isKind(p.kind)) return no('unknown kind'); next.kind = p.kind }
      if (p.zone !== undefined) { if (typeof p.zone !== 'string') return no('bad zone'); next.zone = p.zone.slice(0, ID_MAX) }
      if (p.anchor !== undefined) { if (!isAnchor(p.anchor)) return no('bad anchor'); next.anchor = p.anchor }
      return swap(next)
    }
    case 'bind': {
      if (at < 0) return no('no such object')
      const bind = binding(p.bind)
      if (bind === undefined) return no('bad bind')
      return swap({ ...doc.objects[at], bind })
    }
    case 'remove':
      if (at < 0) return no('no such object')
      return { doc: { ...doc, objects: doc.objects.filter((_, i) => i !== at) }, ok: true, why: '' }
    case 'env': {
      const e = p.env ?? {}
      const ground = e.ground === undefined ? doc.env.ground : e.ground
      if (ground !== 'none' && ground !== 'grid') return no('bad ground')
      return { doc: { ...doc, env: { ground, horizon: e.horizon === undefined ? doc.env.horizon : e.horizon === true } }, ok: true, why: '' }
    }
    default:
      return no('unknown op')
  }
}

/** Apply patches in order. One that does not apply is skipped; the rest still apply. */
export function applyPatches(doc: SceneDoc, patches: readonly ScenePatch[]): SceneDoc {
  return patches.reduce((d, p) => applyPatch(d, p).doc, doc)
}

export type BoundState = 'idle' | 'working' | 'needs' | 'failed' | 'done'

export interface BoundValue {
  /** False when the Bot or the field is not there: the object is then drawn as not bound. */
  found: boolean
  label: string
  state: BoundState
  /** A share from 0 to 1.2, for fields that are a reading against a target. Null when there is none. */
  level: number | null
  /** Epoch seconds, for fields that have a time (a job's next run, a report's date). 0 when there is none. */
  at: number
}

const NOT_BOUND: BoundValue = { found: false, label: '', state: 'idle', level: null, at: 0 }

/**
 * What a bound object shows, read from the Bot's real fields.
 *
 *   state            the Bot's state and name
 *   line             the last line in the Bot's chat
 *   needs            how many asks wait on the person
 *   thing:<slot>     one piece of work: its title and state ('done' when it rests)
 *   need:<id>        one ask
 *   goal:<n>         key result n: its reading as a share of its target
 *   job:<id>         one scheduled job: its name and next run
 *   report:<at>:<t>  one finished report
 */
export function boundValue(bind: Binding | null, bot: XrBot | undefined, glance: XrGlance | null | undefined): BoundValue {
  if (!bind || !bot) return NOT_BOUND
  const f = bind.field
  const rest = f.slice(f.indexOf(':') + 1)
  const base = { found: true, level: null, at: 0 }
  if (f === 'state') return { ...base, label: bot.label, state: bot.state }
  if (f === 'line') return { ...base, label: bot.line, state: bot.state }
  if (f === 'needs') return { ...base, label: String(bot.needs.length), state: bot.needs.length ? 'needs' : 'idle', level: Math.min(1, bot.needs.length / 3) }
  if (f.startsWith('thing:')) {
    const t = bot.things.find(x => x.slot === rest)
    return t ? { ...base, label: t.topic || t.line, state: t.state === 'idle' && !t.direct ? 'done' : t.state, at: t.ts } : NOT_BOUND
  }
  if (f.startsWith('need:')) {
    const n = bot.needs.find(x => x.id === rest)
    return n ? { ...base, label: n.topic || n.action, state: 'needs', at: n.ts } : NOT_BOUND
  }
  if (f.startsWith('goal:')) {
    const n = glance?.numbers[Number(rest)]
    if (!n) return NOT_BOUND
    const value = plainNumber(n.value)
    const target = plainNumber(n.target)
    return { ...base, label: n.label, state: 'idle', level: value !== null && target !== null && target !== 0 ? Math.max(0, Math.min(1.2, value / target)) : null, at: n.at }
  }
  if (f.startsWith('job:')) {
    const j = bot.jobs.find(x => x.id === rest)
    return j ? { ...base, label: j.name, state: j.on ? 'idle' : 'failed', at: j.next } : NOT_BOUND
  }
  if (f.startsWith('report:')) {
    const r = glance?.reports.find(x => reportKey(x) === f)
    return r ? { ...base, label: r.title, state: 'done', at: r.at } : NOT_BOUND
  }
  return NOT_BOUND
}

const zonesOf = (bots: readonly XrBot[], plots: PlotMap): SceneZone[] =>
  bots.filter(b => Object.hasOwn(plots, b.slug)).map(b => ({ id: b.slug, owner: b.slug, cell: plots[b.slug] }))

export type SceneMaker = (bots: readonly XrBot[], glances: ReadonlyMap<string, XrGlance>, plots: PlotMap) => SceneDoc

/** The objects of one Bot's plot, each bound to the field it stands for. `keep` says which kinds a scene wants. */
const plotObjects = (bot: XrBot, glance: XrGlance | null, keep: (kind: string) => boolean): SceneObject[] =>
  plotPlan(bot, glance).filter(it => keep(it.kind)).map((it): SceneObject => ({
    id: `${bot.slug}/${it.id}`, kind: it.kind, anchor: 'land', zone: bot.slug, pos: [it.x, 0, it.z], yaw: 0, scale: 1, label: '',
    bind: { bot: bot.slug, field: it.field }, page: it.page, more: it.more,
  }))

/**
 * The calm scene, and the one that opens: the land with only the work on it. A plot holds one thing for each piece of
 * work that runs or waits, and nothing else. What waits on the person is not a thing of the land: it is a marble in
 * the tray, drawn by the interaction layer.
 */
export const calmScene: SceneMaker = (bots, glances, plots) => ({
  id: 'calm',
  env: { ground: 'grid', horizon: true },
  zones: zonesOf(bots, plots),
  objects: bots.flatMap(bot => plotObjects(bot, glances.get(bot.slug) ?? null, kind => kind === 'work')),
  pages: [],
})

/** Open land settled by Bots: each Bot's plot holds an object for each of its real things, finished work and all. */
export const landScene: SceneMaker = (bots, glances, plots) => ({
  id: 'land',
  env: { ground: 'grid', horizon: true },
  zones: zonesOf(bots, plots),
  // The last one is a stand-in for the person's real screen. A page is pulled out of it into the air.
  objects: [...bots.flatMap(bot => plotObjects(bot, glances.get(bot.slug) ?? null, () => true)), SCREEN],
  pages: [],
})

/** The blank space: every Bot has its ground and nothing stands on it yet. */
export const blankScene: SceneMaker = (bots, _glances, plots) => ({
  id: 'blank', env: { ground: 'none', horizon: false }, zones: zonesOf(bots, plots), objects: [], pages: [],
})

export const SCENES: Readonly<Record<string, SceneMaker>> = { calm: calmScene, land: landScene, blank: blankScene }
export const DEFAULT_SCENE = 'calm'
export const sceneFor = (id: string | null | undefined): SceneMaker => SCENES[id ?? ''] ?? SCENES[DEFAULT_SCENE]
