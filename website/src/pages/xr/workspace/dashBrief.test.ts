import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import en from '../../../i18n/locales/en.json'
import { DASH, DASH_KINDS, DASH_SAYS, DASH_UNITS, NO_VALUE, isPart, partText, partValue, readLayout, shortNumber, type DashPart, type DashWhy } from './dash3d'
import { BRIEF_EXAMPLE, DASH_DASHES, DASH_WHYS, briefReplies, dashBrief } from './dashBrief'

const SKILL = resolve(process.cwd(), '../src/kiro_crew/builtin_skills/dashboard/SKILL.md')
const HEADING = '## 8. In XR: a 3D dashboard'

/** The skill's section 8: from its heading to the next section, or to the end. */
function section(text: string): string {
  const start = text.indexOf(HEADING)
  if (start < 0) return ''
  const next = text.indexOf('\n## ', start + HEADING.length)
  return next < 0 ? text.slice(start) : text.slice(start, next + 1)
}

const one = (part: Record<string, unknown>) => readLayout({ v: DASH.version, parts: [part] }).parts[0]
const why = (part: Record<string, unknown>) => { const p = one(part); return isPart(p) ? '' : p.broken }
const fmt = (n: number) => new Intl.NumberFormat('en', { notation: 'compact', maximumFractionDigits: 1 }).format(n)

describe('the Bot\'s brief for its 3D dashboard', () => {
  it('is section 8 of the dashboard skill, word for word', () => {
    // On a difference, the skill's section is written again from `dashBrief()`: the code is the source.
    expect(section(readFileSync(SKILL, 'utf-8'))).toBe(dashBrief())
  })

  it('quotes reply lines that the reader itself makes, and names every reason for a dash', () => {
    const text = dashBrief()
    const { layout, number } = briefReplies()
    expect(layout).toBe('In the XR room this layout reads: part 1: ring 12/20; part 2: bar, a dash: not a number; part 3: layout: bad name.')
    expect(number).toBe('In the XR room: part 1: ring 13/20.')
    expect(text).toContain(`"${layout}"`)
    expect(text).toContain(`"${number}"`)
    expect(DASH_DASHES).toEqual(Object.values(DASH_SAYS).filter(s => s.startsWith('a dash: ')))
    for (const d of DASH_DASHES) expect(text).toContain(`"${d}"`)
    // The older sentence, which said the reply names nothing of the room, is gone.
    expect(text).not.toContain('only says the field was written')
  })

  it('shows a whole layout: every part of its example is drawn', () => {
    const l = readLayout(BRIEF_EXAMPLE)
    expect(l.why).toBe('')
    expect(l.parts.every(isPart)).toBe(true)
    expect(new Set(BRIEF_EXAMPLE.parts.map(p => p.kind))).toEqual(new Set(DASH_KINDS))
  })

  it('names every reason the room gives, and each has its line', () => {
    const every: Record<DashWhy, true> = { not_layout: true, version: true, no_parts: true, too_many: true, part: true, kind: true, name: true, field: true, path: true, max: true, key: true }
    expect([...DASH_WHYS].sort()).toEqual(Object.keys(every).sort())
    for (const w of DASH_WHYS) expect((en.xr as Record<string, string>)[`dash3d_why_${w}`]).toMatch(/^layout: /)
  })

  it('holds a layout where the brief says: 1 to the most parts, its one version', () => {
    const part = { kind: 'number', name: 'N', field: 'n' }
    expect(readLayout({ v: DASH.version, parts: Array(DASH.mostParts).fill(part) }).why).toBe('')
    expect(readLayout({ v: DASH.version, parts: Array(DASH.mostParts + 1).fill(part) }).why).toBe('too_many')
    expect(readLayout({ v: DASH.version, parts: [] }).why).toBe('no_parts')
    expect(readLayout({ v: DASH.version + 1, parts: [part] }).why).toBe('version')
    expect(readLayout('ring').why).toBe('not_layout')
  })

  it('holds each key of a part where the brief says', () => {
    const n = { kind: 'number', name: 'N', field: 'n' }
    for (const kind of DASH_KINDS) expect(why({ ...n, kind, max: 1 })).toBe('')
    expect(why({ ...n, kind: 'gauge' })).toBe('kind')
    expect(why({ ...n, name: 'x'.repeat(DASH.nameMax) })).toBe('')
    expect(why({ ...n, name: 'x'.repeat(DASH.nameMax + 1) })).toBe('name')
    expect(why({ ...n, name: '' })).toBe('name')
    expect((one({ ...n, name: `e${'\u0301'.repeat(DASH.marksMost + 2)}` }) as DashPart).name.normalize('NFD')).toHaveLength(1 + DASH.marksMost)
    expect((one({ ...n, name: 'A\u0007\u200eB' }) as DashPart).name).toBe('AB')
    expect(why({ ...n, field: `a${'b'.repeat(63)}` })).toBe('')
    for (const field of [`a${'b'.repeat(64)}`, 'Open', '1a', 'a-b']) expect(why({ ...n, field })).toBe('field')
    expect((one({ ...n, path: Array(DASH.pathKeys).fill('k').join('.') }) as DashPart).path).toHaveLength(DASH.pathKeys)
    for (const path of [Array(DASH.pathKeys + 1).fill('k').join('.'), ['k'], 'a-b']) expect(why({ ...n, path })).toBe('path')
    for (const kind of ['bar', 'ring']) {
      expect(why({ ...n, kind })).toBe('max')
      expect(why({ ...n, kind, max: 0 })).toBe('max')
      expect(why({ ...n, kind, max: 'spend_budget' })).toBe('')
    }
    expect((one({ ...n, kind: 'lamps' }) as DashPart).key).toBe('state')
    expect(why({ ...n, kind: 'lamps', key: 'phase' })).toBe('')
    expect(why({ ...n, kind: 'lamps', key: 'a b' })).toBe('key')
    for (const unit of DASH_UNITS) expect((one({ ...n, unit }) as DashPart).unit).toBe(unit)
    expect((one({ ...n, unit: 'kg' }) as DashPart).unit).toBe('')
    // A path's key and a lamp's key: `keyMax` characters, not one more.
    const long = 'k'.repeat(DASH.keyMax)
    expect(why({ ...n, path: `${long}.b` })).toBe('')
    expect(why({ ...n, path: `${long}k.b` })).toBe('path')
    expect(why({ ...n, kind: 'lamps', key: long })).toBe('')
    expect(why({ ...n, kind: 'lamps', key: `${long}k` })).toBe('key')
    // `key` is the lamps' only: on another kind it breaks nothing.
    for (const kind of ['number', 'bar', 'ring']) expect(why({ ...n, kind, max: 1, key: 'a b' })).toBe('')
  })

  it('keeps the parts in the order written', () => {
    const parts = DASH_KINDS.map((kind, i) => ({ kind, name: `P${i}`, field: `f${i}`, max: 1 }))
    expect(readLayout({ v: DASH.version, parts: [...parts].reverse() }).parts.map(p => (p as DashPart).name)).toEqual(['P3', 'P2', 'P1', 'P0'])
  })

  it('draws what each kind shows as the brief says', () => {
    const part = (p: Record<string, unknown>) => one({ name: 'N', field: 'n', ...p }) as DashPart
    const num = part({ kind: 'number' })
    expect(partValue(num, { n: '7' }).number).toBe(7)
    expect([...shortNumber(123456789, fmt)].length).toBeLessThanOrEqual(DASH.numberMax)
    expect(partValue(part({ kind: 'bar', max: 10 }), { n: 50 }).share).toBe(DASH.overShare)
    const ring = part({ kind: 'ring', max: 20, unit: '%' })
    expect(partText(ring, partValue(ring, { n: 7 }), fmt)).toBe('7/20')
    // Text that is a plain number is read for a bar and a ring too; a bar writes its unit, a ring none.
    const bar = part({ kind: 'bar', max: 10, unit: '$' })
    expect(partValue(bar, { n: '5' }).share).toBe(0.5)
    expect(partText(bar, partValue(bar, { n: '5' }), fmt)).toBe('5 $')
    expect(partText(ring, partValue(ring, { n: '7' }), fmt)).toBe('7/20')
    // A value that is missing or that the kind does not take: the dash, and the part is no socket.
    for (const [p, fields] of [[num, {}], [num, { n: 'many' }], [bar, { n: [1] }], [part({ kind: 'lamps' }), { n: 3 }]] as const) {
      expect(isPart(p)).toBe(true)
      expect(partText(p, partValue(p, fields), fmt)).toBe(NO_VALUE)
    }
    const lamps = part({ kind: 'lamps' })
    const many = partValue(lamps, { n: Array(DASH.mostLamps + 8).fill({ state: 'working' }) })
    expect([many.lamps.length, many.number]).toEqual([DASH.mostLamps, DASH.mostLamps + 8])
    expect(partValue(lamps, { n: ['done', { state: 'needs' }] }).lamps).toEqual(['done', 'other'])
  })
})
