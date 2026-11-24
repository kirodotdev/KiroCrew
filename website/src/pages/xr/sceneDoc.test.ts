import { describe, expect, it } from 'vitest'
import { fixtureBriefing, fixtureGoals, fixturePage, xrFixture } from './fixture'
import { buildGlance, type XrGlance } from './glance'
import { PLOT_M, assignPlots } from './land'
import { buildXrCrew, type XrBot } from './model'
import { PAGE_MAX, PAGE_W_DEG, freePlace, pagePos, parsePages, placeOf, samePage, type PageRec } from './pages'
import { BOT_SPOT, idleLife, plotPlan } from './plots'
import { OBJECT_KINDS, SCENES, SCENE_MAX_OBJECTS, applyPatch, applyPatches, blankScene, boundValue, calmScene, landScene, sceneFor, type SceneDoc, type ScenePatch } from './sceneDoc'
import { AURA_VIEW, FULL, TEXT_FLOOR_DEG, FULL_FONT, degOf } from './metrics'

const crew = buildXrCrew(xrFixture(false))
const bot = (slug: string): XrBot => crew.bots.find(b => b.slug === slug)!
const glanceOf = (b: XrBot): XrGlance => buildGlance(b, fixtureGoals(b.slug), fixtureBriefing(b.slug), fixturePage(b.slug))
const glances = new Map(crew.bots.map(b => [b.slug, glanceOf(b)]))
const plots = assignPlots({}, crew.bots.map(b => b.slug))
const land = landScene(crew.bots, glances, plots)

describe('a plot, from the Bot\'s real fields', () => {
  it('gives every Bot a home, so no plot is ever empty', () => {
    for (const b of crew.bots) expect(plotPlan(b, null).filter(i => i.kind === 'home')).toHaveLength(1)
  })

  it('stands one thing for each real thing: work, asks, key results, scheduled jobs, finished work', () => {
    const atlas = plotPlan(bot('atlas'), glances.get('atlas')!)
    const count = (kind: string) => atlas.filter(i => i.kind === kind).length
    expect(count('ask')).toBe(bot('atlas').needs.length)
    expect(count('work')).toBe(bot('atlas').things.filter(t => t.state !== 'idle').length)
    expect(count('goal')).toBe(3)
    expect(count('clock')).toBe(bot('atlas').jobs.length)
    expect(count('clock')).toBeGreaterThan(0)
    expect(atlas.filter(i => i.kind === 'built').map(i => i.id)).toEqual([`report:${glances.get('atlas')!.reports[0].at}:Release week 46 report`])
  })

  it('keeps finished work as built things on a Bot that has nothing to do today', () => {
    const mira = plotPlan(bot('mira'), glances.get('mira')!)
    expect(bot('mira').state).toBe('idle')
    expect(mira.filter(i => i.kind === 'built').length).toBeGreaterThanOrEqual(2)
  })

  it('never moves a thing when another is added: places go by a key that does not change', () => {
    const before = plotPlan(bot('atlas'), glances.get('atlas')!)
    const more: XrBot = { ...bot('atlas'), things: [...bot('atlas').things, { slot: 'a-new-one', direct: false, topic: 'New', line: '', state: 'idle', ts: 9e9 }, { slot: 'zz-work', direct: false, topic: 'More work', line: '', state: 'working', ts: 9e9 }] }
    const after = plotPlan(more, glances.get('atlas')!)
    for (const it of before) expect(after.find(a => a.id === it.id)).toMatchObject({ x: it.x, z: it.z })
    // The finished one is built at the end of its row. The fifth piece of work has no place left, and is counted.
    expect(after.filter(i => i.kind === 'built').at(-1)!.id).toBe('thing:a-new-one')
    expect(after.filter(i => i.kind === 'work').map(i => i.more)).toEqual([0, 0, 0, 1])
  })

  it('counts what has no place of its own on the last one drawn', () => {
    const many: XrBot = { ...bot('sage'), things: Array.from({ length: 9 }, (_, i) => ({ slot: `w${i}`, direct: false, topic: `Job ${i}`, line: '', state: 'working' as const, ts: i })) }
    const work = plotPlan(many, null).filter(i => i.kind === 'work')
    expect(work).toHaveLength(4)
    expect(work.map(i => i.more)).toEqual([0, 0, 0, 5])
  })

  it('keeps everything inside the plot, and clear of where the Bot stands', () => {
    for (const b of crew.bots) {
      for (const it of plotPlan(b, glances.get(b.slug)!)) {
        expect(Math.max(Math.abs(it.x), Math.abs(it.z))).toBeLessThan(PLOT_M / 2)
        expect(Math.hypot(it.x - BOT_SPOT[0], it.z - BOT_SPOT[1])).toBeGreaterThan(0.6)
      }
    }
  })
})

describe('an idle Bot has a small life of its own', () => {
  const items = plotPlan(bot('mira'), glances.get('mira')!)

  it('does more than one thing within a minute, and never leaves its plot', () => {
    const steps = Array.from({ length: 60 }, (_, s) => idleLife('mira', items, s))
    expect(new Set(steps.map(s => s.act)).size).toBeGreaterThanOrEqual(2)
    expect(new Set(steps.map(s => `${s.x.toFixed(2)},${s.z.toFixed(2)}`)).size).toBeGreaterThanOrEqual(2)
    for (const s of steps) expect(Math.max(Math.abs(s.x), Math.abs(s.z))).toBeLessThanOrEqual(PLOT_M / 2 - 0.4)
  })

  it('is the same for the same Bot at the same second, and each Bot keeps its own beat', () => {
    expect(idleLife('mira', items, 137)).toEqual(idleLife('mira', items, 137))
    const beats = (slug: string) => Array.from({ length: 120 }, (_, s) => idleLife(slug, items, s).act).join()
    expect(beats('mira')).not.toBe(beats('sage'))
  })

  it('still has something to do on ground with nothing on it', () => {
    for (let s = 0; s < 60; s += 1) expect(['sit', 'look', 'nap']).toContain(idleLife('pico', [], s).act)
  })
})

describe('the scene is a document', () => {
  it('builds the land from the real Bots: a zone each, and an object for each thing, bound to its field', () => {
    expect(land.id).toBe('land')
    expect(land.zones.map(z => [z.owner, z.cell])).toEqual(crew.bots.map(b => [b.slug, plots[b.slug]]))
    // Everything on the land is a real thing of a real Bot. The one thing by the seat is the stand-in for the person's own screen.
    expect(land.objects.filter(o => o.anchor === 'seat')).toEqual([expect.objectContaining({ id: 'screen', kind: 'screen', bind: null, page: { kind: 'hub', slot: '' } })])
    for (const o of land.objects.filter(x => x.anchor === 'land')) {
      expect(OBJECT_KINDS).toContain(o.kind)
      expect(o.bind?.bot).toBe(o.zone)
      expect(boundValue(o.bind, bot(o.zone), glances.get(o.zone)).found).toBe(true)
    }
    expect(new Set(land.objects.map(o => o.id)).size).toBe(land.objects.length)
  })

  it('has an empty scene too: the same zones, and nothing on them', () => {
    const blank = blankScene(crew.bots, glances, plots)
    expect(blank.zones).toEqual(land.zones)
    expect(blank.objects).toEqual([])
    expect(sceneFor('blank')).toBe(SCENES.blank)
    expect(sceneFor('nowhere')).toBe(SCENES.calm)
    expect(sceneFor(null)).toBe(SCENES.calm)
  })

  it('opens on the calm scene: every Bot on its plot, a thing for each piece of work that runs or waits, and nothing else', () => {
    const calm = calmScene(crew.bots, glances, plots)
    expect(calm.id).toBe('calm')
    expect(calm.zones).toEqual(land.zones)
    expect(calm.objects.every(o => o.kind === 'work' && o.anchor === 'land' && o.bind?.field.startsWith('thing:'))).toBe(true)
    expect(calm.objects.length).toBe(crew.bots.reduce((n, b) => n + Math.min(4, b.things.filter(t => t.state !== 'idle').length), 0))
    // It is the same work the full land shows, in the same places: a scene only picks what stands.
    for (const o of calm.objects) expect(land.objects.find(x => x.id === o.id)).toEqual(o)
    // A Bot with nothing to do has an empty plot, and still a small life of its own.
    expect(calm.objects.some(o => o.zone === 'mira')).toBe(false)
    expect(['sit', 'look', 'nap']).toContain(idleLife('mira', [], 12).act)
  })

  it('reads what a bound object shows from the Bot\'s real field', () => {
    const atlas = bot('atlas')
    const g = glances.get('atlas')
    expect(boundValue({ bot: 'atlas', field: 'state' }, atlas, g)).toMatchObject({ found: true, label: 'Atlas', state: 'needs' })
    expect(boundValue({ bot: 'atlas', field: 'needs' }, atlas, g)).toMatchObject({ label: '2', state: 'needs' })
    expect(boundValue({ bot: 'atlas', field: 'goal:0' }, atlas, g)).toMatchObject({ label: 'Release checklist items done', level: 0.75 })
    expect(boundValue({ bot: 'atlas', field: 'thing:w-atlas-notes' }, atlas, g)).toMatchObject({ label: 'Release notes for 0.15', state: 'working' })
    expect(boundValue({ bot: 'atlas', field: 'thing:w-atlas-cache' }, atlas, g).state).toBe('needs')
    expect(boundValue({ bot: 'mira', field: 'thing:w-mira-report' }, bot('mira'), glances.get('mira')).state).toBe('done')
    expect(boundValue({ bot: 'atlas', field: `job:${atlas.jobs[0].id}` }, atlas, g)).toMatchObject({ label: atlas.jobs[0].name, at: atlas.jobs[0].next })
    // A field that is not there, a Bot that is not there, no binding: not bound, never a guess.
    for (const v of [boundValue({ bot: 'atlas', field: 'goal:9' }, atlas, g), boundValue({ bot: 'atlas', field: 'nonsense' }, atlas, g), boundValue({ bot: 'ghost', field: 'state' }, undefined, undefined), boundValue(null, atlas, g)]) {
      expect(v.found).toBe(false)
    }
  })

  it('follows a patch: add an object, move it, bind it to a field, remove it', () => {
    const blank = blankScene(crew.bots, glances, plots)
    const added = applyPatch(blank, { op: 'add', object: { id: 'lamp', kind: 'marker', zone: 'juno', pos: [1, 0, 1], label: 'A lamp' } })
    expect(added.ok).toBe(true)
    expect(added.doc.objects).toEqual([{ id: 'lamp', kind: 'marker', anchor: 'land', zone: 'juno', pos: [1, 0, 1], yaw: 0, scale: 1, label: 'A lamp', bind: null, page: null, more: 0 }])
    expect(blank.objects).toEqual([])
    const moved = applyPatch(added.doc, { op: 'set', id: 'lamp', pos: [-2, 0, 0.5], scale: 2, yaw: 90 })
    expect(moved.doc.objects[0]).toMatchObject({ pos: [-2, 0, 0.5], scale: 2, yaw: 90, label: 'A lamp' })
    const bound = applyPatch(moved.doc, { op: 'bind', id: 'lamp', bind: { bot: 'juno', field: 'needs' } })
    expect(boundValue(bound.doc.objects[0].bind, bot('juno'), glances.get('juno'))).toMatchObject({ found: true, label: '1', state: 'needs' })
    const unbound = applyPatch(bound.doc, { op: 'bind', id: 'lamp', bind: null })
    expect(unbound.doc.objects[0].bind).toBeNull()
    const gone = applyPatch(unbound.doc, { op: 'remove', id: 'lamp' })
    expect(gone.doc.objects).toEqual([])
    expect(applyPatch(blank, { op: 'env', env: { ground: 'grid' } }).doc.env).toEqual({ ground: 'grid', horizon: false })
  })

  it('refuses a patch that is not well formed, and changes nothing', () => {
    const bad: [ScenePatch, string][] = [
      [{ op: 'add', object: { id: 'x', kind: 'script' } }, 'unknown kind'],
      [{ op: 'add', object: { id: '', kind: 'block' } }, 'bad id'],
      [{ op: 'add', object: { id: land.objects[0].id, kind: 'block' } }, 'id taken'],
      [{ op: 'add', object: { id: 'x', kind: 'block', pos: [0, Number.NaN, 0] } }, 'bad pos'],
      [{ op: 'add', object: { id: 'x', kind: 'block', bind: { bot: '', field: 'state' } } }, 'bad bind'],
      [{ op: 'set', id: 'nobody', pos: [0, 0, 0] }, 'no such object'],
      [{ op: 'set', id: land.objects[0].id, kind: 'eval' }, 'unknown kind'],
      [{ op: 'set', id: land.objects[0].id, scale: -1 }, 'bad scale'],
      [{ op: 'bind', id: 'nobody', bind: null }, 'no such object'],
      [{ op: 'remove', id: 'nobody' }, 'no such object'],
      [{ op: 'env', env: { ground: 'lava' as 'grid' } }, 'bad ground'],
      [{ op: 'explode' } as unknown as ScenePatch, 'unknown op'],
    ]
    for (const [patch, why] of bad) {
      const r = applyPatch(land, patch)
      expect([r.ok, r.why]).toEqual([false, why])
      expect(r.doc).toBe(land)
    }
  })

  it('holds a document to its limits: positions are kept in reach and objects are counted', () => {
    const far = applyPatch(land, { op: 'add', object: { id: 'far', kind: 'block', pos: [1e9, -1e9, 5] } })
    expect(far.doc.objects.at(-1)!.pos).toEqual([60, -60, 5])
    let doc: SceneDoc = blankScene(crew.bots, glances, plots)
    for (let i = 0; i < SCENE_MAX_OBJECTS; i += 1) doc = applyPatch(doc, { op: 'add', object: { id: `b${i}`, kind: 'block' } }).doc
    expect(applyPatch(doc, { op: 'add', object: { id: 'one-more', kind: 'block' } })).toMatchObject({ ok: false, why: 'too many objects' })
  })

  it('keeps a person\'s patches when the land is made again from new data', () => {
    const patches: ScenePatch[] = [{ op: 'set', id: 'atlas/home', pos: [1, 0, -2] }, { op: 'add', object: { id: 'mine', kind: 'block', zone: 'atlas' } }, { op: 'remove', id: 'no-longer-there' }]
    const again = applyPatches(landScene(crew.bots, glances, plots), patches)
    expect(again.objects.find(o => o.id === 'atlas/home')!.pos).toEqual([1, 0, -2])
    expect(again.objects.some(o => o.id === 'mine')).toBe(true)
    expect(again.objects.length).toBe(land.objects.length + 1)
  })
})

describe('many pages around the desk', () => {
  const page = (o: Partial<PageRec>): PageRec => ({ kind: 'chat', slug: 'atlas', slot: 'member-atlas', topic: '', yaw: 0, pitch: 0, stored: false, ...o })

  it('opens the first two side by side inside what the glasses show, then further round, then a row above', () => {
    const pages: PageRec[] = []
    const places = []
    for (let i = 0; i < 8; i += 1) { const p = freePlace(pages, 0)!; places.push(p); pages.push(page({ slug: `b${i}`, slot: `s${i}`, ...p })) }
    expect(places[0].yaw).toBeCloseTo(-places[1].yaw)
    for (const p of places.slice(0, 2)) expect(Math.abs(p.yaw) + PAGE_W_DEG / 2).toBeLessThanOrEqual(AURA_VIEW.h / 2)
    expect(Math.abs(places[2].yaw)).toBeGreaterThan(AURA_VIEW.h / 2)
    // No two overlap, in any pair.
    for (let i = 0; i < pages.length; i += 1) for (let j = 0; j < i; j += 1) {
      expect(Math.abs(pages[i].yaw - pages[j].yaw) >= PAGE_W_DEG || Math.abs(pages[i].pitch - pages[j].pitch) >= 20).toBe(true)
    }
  })

  it('opens a new page where the person looks, and reuses the place of a page that was put away', () => {
    expect(Math.abs(freePlace([], 70)!.yaw - 70)).toBeLessThan(PAGE_W_DEG)
    const first = freePlace([], 0)!
    expect(freePlace([page({ ...first, stored: true })], 0)).toEqual(first)
    expect(freePlace([page(first)], 0)).not.toEqual(first)
  })

  it('stands every page at reading distance, so its text keeps its angle wherever it is put', () => {
    for (const [yaw, pitch] of [[0, 0], [95, 10], [-140, 24]]) {
      const [x, y, z] = pagePos(yaw, pitch)
      expect(Math.hypot(x, y, z)).toBeCloseTo(FULL.distance)
      const back = placeOf(x, y, z)
      expect(back.yaw).toBeCloseTo(yaw)
      expect(back.pitch).toBeCloseTo(pitch)
    }
    expect(degOf(FULL, FULL_FONT.small)).toBeGreaterThanOrEqual(TEXT_FLOOR_DEG)
  })

  it('knows two pages are the same thing: one page per Bot thread, and one for the crew', () => {
    expect(samePage(page({ kind: 'chat' }), page({ kind: 'dash' }))).toBe(true)
    expect(samePage(page({}), page({ slot: 'w-atlas-notes' }))).toBe(false)
    expect(samePage(page({ kind: 'hub', slug: '', slot: '' }), page({ kind: 'hub', slug: 'x', slot: 'y' }))).toBe(true)
    expect(samePage(page({ kind: 'hub', slug: '', slot: '' }), page({ slug: '', slot: '' }))).toBe(false)
  })

  it('reads stored pages, drops what is not a page, and stops at the limit', () => {
    const good = [page({}), page({ kind: 'hub', slug: '', slot: '', yaw: 40, stored: true })]
    expect(parsePages(JSON.stringify(good))).toEqual(good)
    const junk = [{ kind: 'script', slug: 'a', slot: 'b', yaw: 0, pitch: 0 }, { kind: 'chat', slug: 1, slot: 'b', yaw: 0, pitch: 0 }, { kind: 'chat', slug: 'a', slot: 'b', yaw: 'x', pitch: 0 }, { kind: 'chat', slug: 'a', slot: 'b', yaw: 0, pitch: 500 }, null, 7]
    expect(parsePages(JSON.stringify([...junk, good[0], good[0]]))).toEqual([good[0]])
    for (const bad of ['', '{', '{"a":1}', 'null']) expect(parsePages(bad)).toEqual([])
    const many = Array.from({ length: PAGE_MAX + 5 }, (_, i) => page({ slug: `b${i}`, slot: `s${i}` }))
    expect(parsePages(JSON.stringify(many))).toHaveLength(PAGE_MAX)
  })
})
