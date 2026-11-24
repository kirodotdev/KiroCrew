import { describe, expect, it } from 'vitest'
import { fixtureBriefing, fixtureGoals, fixturePage, xrFixture } from './fixture'
import { buildGlance, type XrGlance } from './glance'
import { PARTS_MAX } from './handling'
import { buildXrCrew, type XrBot } from './model'
import { partsOf } from './parts'
import { reportKey } from './plots'

const crew = buildXrCrew(xrFixture(false))
const bot = (slug: string): XrBot => crew.bots.find(b => b.slug === slug)!
const glanceOf = (b: XrBot): XrGlance => buildGlance(b, fixtureGoals(b.slug), fixtureBriefing(b.slug), fixturePage(b.slug))
const atlas = bot('atlas')
const glance = glanceOf(atlas)
/** Every piece of text the Crew holds about a Bot: a part may show nothing else. */
const known = (b: XrBot, g: XrGlance): string[] => [
  b.label, b.line, ...b.things.flatMap(t => [t.topic, t.line]), ...b.needs.flatMap(n => [n.action, n.topic, n.command]),
  ...g.goals, ...g.numbers.flatMap(n => [n.label, n.value, `${n.value} / ${n.target}`]), ...g.reports.map(r => r.title), ...b.jobs.map(j => j.name),
]

describe('the parts a thing opens into', () => {
  it('opens a Bot into how it stands, its oldest ask, what it works on and its goal', () => {
    const parts = partsOf({ bot: 'atlas', field: 'state' }, atlas, glance)
    expect(parts[0]).toMatchObject({ head: 'what', state: atlas.state })
    const oldest = [...atlas.needs].sort((a, b) => a.ts - b.ts)[0]
    expect(parts.find(p => p.head === 'ask')).toMatchObject({ text: oldest.action || oldest.topic, command: oldest.command, state: 'needs' })
    expect(parts.some(p => p.head === 'goal')).toBe(glance.goals.length > 0)
  })

  it('opens a piece of work into what it is, its latest line, and each ask that waits on it', () => {
    const need = atlas.needs.find(n => atlas.things.some(t => t.slot === n.slot))!
    const thing = atlas.things.find(t => t.slot === need.slot)!
    const parts = partsOf({ bot: 'atlas', field: `thing:${thing.slot}` }, atlas, glance)
    expect(parts[0]).toMatchObject({ head: 'what', text: thing.topic || thing.line })
    const asks = parts.filter(p => p.head === 'ask')
    expect(asks).toHaveLength(Math.min(atlas.needs.filter(n => n.slot === thing.slot).length, PARTS_MAX - parts.findIndex(p => p.head === 'ask')))
    expect(asks[0].command).toBe(atlas.needs.find(n => n.slot === thing.slot)!.command)
  })

  it('opens an ask into the ask with its exact command, and the work it belongs to', () => {
    const need = atlas.needs[0]
    const parts = partsOf({ bot: 'atlas', field: `need:${need.id}` }, atlas, glance)
    expect(parts[0]).toMatchObject({ head: 'ask', text: need.action || need.topic, command: need.command })
    if (need.topic) expect(parts[1]).toMatchObject({ head: 'work', text: need.topic })
  })

  it('opens a key result into what is measured and its reading', () => {
    const n = glance.numbers[0]
    const parts = partsOf({ bot: 'atlas', field: 'goal:0' }, atlas, glance)
    expect(parts.map(p => p.head)).toEqual(['goal', 'what'])
    expect(parts[0].text).toBe(n.label)
    expect(parts[1].text).toBe(n.target ? `${n.value} / ${n.target}` : n.value)
  })

  it('opens a job and a report into one part each: the data holds no more about them', () => {
    const job = atlas.jobs[0]
    expect(partsOf({ bot: 'atlas', field: `job:${job.id}` }, atlas, glance)).toEqual([expect.objectContaining({ head: 'what', text: job.name, at: job.next })])
    const report = glance.reports[0]
    expect(partsOf({ bot: 'atlas', field: reportKey(report) }, atlas, glance)).toEqual([expect.objectContaining({ head: 'what', text: report.title, state: 'done' })])
  })

  it('makes nothing up: every part is text the Crew holds, for every thing of every Bot', () => {
    for (const b of crew.bots) {
      const g = glanceOf(b)
      const fields = ['state', ...b.things.map(t => `thing:${t.slot}`), ...b.needs.map(n => `need:${n.id}`), ...g.numbers.map((_, i) => `goal:${i}`), ...b.jobs.map(j => `job:${j.id}`), ...g.reports.map(reportKey)]
      const all = known(b, g)
      for (const field of fields) {
        const parts = partsOf({ bot: b.slug, field }, b, g)
        expect(parts.length, `${b.slug} ${field}`).toBeGreaterThan(0)
        expect(parts.length).toBeLessThanOrEqual(PARTS_MAX)
        for (const p of parts) {
          expect(all, `${b.slug} ${field}`).toContain(p.text)
          if (p.command) expect(all).toContain(p.command)
        }
      }
    }
  })

  it('has no part for a change, its checks or its review: the Crew has no field for them', () => {
    const heads = new Set(crew.bots.flatMap(b => ['state', ...b.things.map(t => `thing:${t.slot}`)].flatMap(f => partsOf({ bot: b.slug, field: f }, b, glanceOf(b)).map(p => p.head))))
    for (const h of heads) expect(['what', 'latest', 'ask', 'goal', 'work']).toContain(h)
  })

  it('lights the part in trouble, and only that: a part that waits on the person, or that failed', () => {
    for (const b of crew.bots) {
      const g = glanceOf(b)
      for (const field of ['state', ...b.things.map(t => `thing:${t.slot}`), ...b.needs.map(n => `need:${n.id}`)]) {
        for (const p of partsOf({ bot: b.slug, field }, b, g)) expect(p.lit, `${b.slug} ${field} ${p.head}`).toBe(p.state === 'needs' || p.state === 'failed')
      }
    }
    const need = atlas.needs.find(n => atlas.things.some(t => t.slot === n.slot))!
    const parts = partsOf({ bot: 'atlas', field: `thing:${need.slot}` }, atlas, glance)
    expect(parts.filter(p => p.lit).length).toBeGreaterThan(0)
    expect(parts.some(p => !p.lit)).toBe(true)
    // A thing with nothing wrong has no lit part.
    const calm = crew.bots.find(b => b.state === 'idle' && b.things.length)!
    expect(partsOf({ bot: calm.slug, field: `thing:${calm.things[0].slot}` }, calm, glanceOf(calm)).some(p => p.lit)).toBe(false)
  })

  it('opens nothing for a thing that is gone, a Bot that is gone, or a thing bound to no field', () => {
    expect(partsOf({ bot: 'atlas', field: 'thing:not-there' }, atlas, glance)).toEqual([])
    expect(partsOf({ bot: 'atlas', field: 'need:not-there' }, atlas, glance)).toEqual([])
    expect(partsOf({ bot: 'nobody', field: 'state' }, undefined, null)).toEqual([])
    expect(partsOf(null, atlas, glance)).toEqual([])
  })

  it('counts the parts that have no place on the last one shown', () => {
    const need = atlas.needs[0]
    const many: XrBot = { ...atlas, needs: Array.from({ length: 6 }, (_, i) => ({ ...need, id: `n${i}`, slot: 'busy', action: `Ask ${i}` })), things: [{ slot: 'busy', direct: false, topic: 'Busy work', line: 'Latest line', state: 'needs', ts: 1 }] }
    const parts = partsOf({ bot: 'atlas', field: 'thing:busy' }, many, glance)
    expect(parts).toHaveLength(PARTS_MAX)
    expect(parts.at(-1)!.more).toBe(8 - PARTS_MAX)
    expect(parts.slice(0, -1).every(p => p.more === 0)).toBe(true)
  })
})
