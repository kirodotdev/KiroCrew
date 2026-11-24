import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import ts from 'typescript'
import { describe, expect, it } from 'vitest'
import { CREW, crewPlaces } from './crewRow'
import { PAGES } from './layout'
import { DASH, FORM, HUE_APART_DEG, PART_WIDE, TAKEN_DISTANCE_M, TAKEN_LOOK_DEG, TAKEN_MARGIN_DEG, boardsCameUp, fitAgain, fitTaken, drawnUnder, raysOnDrawn, type RayPiece, REFIT, gapTakes, glideGoes, glideShare, hiddenBy, hiddenStep, HIDDEN_SLACK, sameLook, takenComes, takenTrayTopY, restGaps, dataHues, picturesOverlap, hueOf, isPart, partText, partValue, readLayout, restBox, restRow, shortNumber, takenRow, trayBlock, trayPress, trayWideM, type DashKind, type DashPart } from './dash3d'

const ring = { kind: 'ring', name: 'Checks', field: 'checks_done', max: 20 }
const layout = (...parts: unknown[]) => ({ v: 1, parts })
const part = (raw: unknown): DashPart => {
  const p = readLayout(layout(raw)).parts[0]
  if (!isPart(p)) throw new Error(`not a part: ${p.broken}`)
  return p
}
const why = (raw: unknown) => { const p = readLayout(layout(raw)).parts[0]; return isPart(p) ? '' : p.broken }
const fmt = (n: number) => String(n)

describe('a layout is read as data', () => {
  it('takes the four kinds of the kit, in the order they are written', () => {
    const read = readLayout(layout(
      ring,
      { kind: 'bar', name: 'Spend', field: 'spend_today', max: 'spend_budget', unit: '$' },
      { kind: 'lamps', name: 'Workers', field: 'workers', key: 'status' },
      { kind: 'number', name: 'Open PRs', field: 'open_prs' },
    ))
    expect(read.why).toBe('')
    expect(read.parts.map(p => (isPart(p) ? p.kind : p.broken))).toEqual(['ring', 'bar', 'lamps', 'number'])
    expect(read.parts[0]).toEqual({ kind: 'ring', name: 'Checks', field: 'checks_done', path: [], max: 20, key: 'state', unit: '' })
    expect(read.parts[1]).toMatchObject({ max: 'spend_budget', unit: '$' })
    expect(read.parts[2]).toMatchObject({ key: 'status', max: null })
  })

  it('says why a document is no layout, and gives no parts', () => {
    for (const [raw, said] of [
      [null, 'not_layout'], ['a layout', 'not_layout'], [[ring], 'not_layout'], [{ v: 1 }, 'not_layout'], [{ v: 1, parts: {} }, 'not_layout'],
      [{ v: 2, parts: [ring] }, 'version'], [{ parts: [ring] }, 'version'], [{ v: '1', parts: [ring] }, 'version'],
      [{ v: 1, parts: [] }, 'no_parts'],
      [layout(...Array.from({ length: DASH.mostParts + 1 }, () => ring)), 'too_many'],
    ] as const) {
      expect(readLayout(raw)).toEqual({ parts: [], why: said })
    }
    expect(readLayout(layout(...Array.from({ length: DASH.mostParts }, () => ring))).parts).toHaveLength(DASH.mostParts)
  })

  it('makes a wrong part an empty socket that says why, and the others stand', () => {
    const read = readLayout(layout(ring, { kind: 'gauge', name: 'Load', field: 'load' }, { kind: 'number', name: 'Open PRs', field: 'open_prs' }))
    expect(read.why).toBe('')
    expect(read.parts.map(p => (isPart(p) ? p.kind : p.broken))).toEqual(['ring', 'kind', 'number'])
  })

  it('holds each part to its limits', () => {
    expect(why('ring')).toBe('part')
    expect(why([ring])).toBe('part')
    expect(why({ ...ring, kind: 'shader' })).toBe('kind')
    // A kind is one of the list's own words: a name that every object has finds nothing.
    expect(why({ ...ring, kind: 'constructor' })).toBe('kind')
    expect(why({ ...ring, name: 'x'.repeat(40) })).toBe('name')
    expect(why({ ...ring, name: 'x'.repeat(DASH.nameMax + 1) })).toBe('name')
    expect(why({ ...ring, name: '' })).toBe('name')
    expect(why({ ...ring, name: 7 })).toBe('name')
    expect(why({ ...ring, name: '\u202E\u0007' })).toBe('name')
    expect(why({ ...ring, field: 'Checks' })).toBe('field')
    expect(why({ ...ring, field: '__proto__' })).toBe('field')
    expect(why({ ...ring, field: 'a.b' })).toBe('field')
    expect(why({ ...ring, field: 'x'.repeat(65) })).toBe('field')
    expect(why({ ...ring, max: Infinity })).toBe('max')
    expect(why({ ...ring, max: Number.NaN })).toBe('max')
    expect(why({ ...ring, max: 0 })).toBe('max')
    expect(why({ ...ring, max: -3 })).toBe('max')
    expect(why({ ...ring, max: 'Not A Field' })).toBe('max')
    expect(why({ kind: 'bar', name: 'Spend', field: 'spend' })).toBe('max')
    expect(why({ ...ring, path: 'a.b.c.d.e' })).toBe('path')
    expect(why({ ...ring, path: 'a..b' })).toBe('path')
    expect(why({ ...ring, path: ['a'] })).toBe('path')
    expect(why({ kind: 'lamps', name: 'Workers', field: 'workers', key: 'a b' })).toBe('key')
  })

  it('keeps a name as its own characters: no control or direction character, no state glyph, and markup stays text', () => {
    expect(part({ ...ring, name: ' Che\u0007cks\u202E \u2713' }).name).toBe('Checks')
    // Format characters go too (a joiner, a direction mark, a soft hyphen), not only the controls.
    expect(part({ ...ring, name: 'Ch\u200Dec\u00ADks\u2067\u061C' }).name).toBe('Checks')
    // A letter keeps two of the marks drawn over it and no more; a mark with no letter under it goes.
    expect(part({ ...ring, name: '\u0301a' + '\u0301'.repeat(40) + 'b' }).name).toBe('a\u0301\u0301b'.normalize('NFC'))
    expect(why({ ...ring, name: '\u0301'.repeat(16) })).toBe('name')
    expect(why({ ...ring, name: ('a' + '\u0301\u0302').repeat(DASH.nameMax) })).toBe('')
    expect(part({ ...ring, name: '<b>hi</b>' }).name).toBe('<b>hi</b>')
    // Sixteen characters, counted as the eye counts them.
    expect(part({ ...ring, name: '\u{1F600}'.repeat(DASH.nameMax) }).name).toBe('\u{1F600}'.repeat(DASH.nameMax))
  })

  it('takes nothing but the keys it knows, and a number needs no limit', () => {
    const p = part({ kind: 'number', name: 'Open PRs', field: 'open_prs', max: 'ignored here', src: 'https://example.invalid/x', html: '<script>' })
    expect(p).toEqual({ kind: 'number', name: 'Open PRs', field: 'open_prs', path: [], max: null, key: 'state', unit: '' })
  })

  it('takes a unit from its own short list, and drops any other', () => {
    for (const unit of ['%', '$', 'ms', '/s']) expect(part({ kind: 'number', name: 'N', field: 'n', unit }).unit).toBe(unit)
    for (const unit of ['OK \u2713', 'dollars', 'yes', '', 7, 'constructor']) expect(part({ kind: 'number', name: 'N', field: 'n', unit }).unit).toBe('')
  })

  it('reads only what the document itself holds', () => {
    const made = Object.create({ v: 1, parts: [ring] }) as object
    expect(readLayout(made).why).toBe('version')
  })
})

describe('what a part shows', () => {
  it('is not found when the field is missing, and never reads a name that every object has', () => {
    expect(partValue(part(ring), { other: 3 }).found).toBe(false)
    expect(partValue(part(ring), null).found).toBe(false)
    expect(partValue(part({ kind: 'number', name: 'N', field: 'constructor' }), {}).found).toBe(false)
    expect(partValue(part({ kind: 'number', name: 'N', field: 'record', path: 'constructor.name' }), { record: {} }).found).toBe(false)
    expect(partValue(part({ kind: 'number', name: 'N', field: 'record', path: 'a.b' }), { record: { a: [1] } }).found).toBe(false)
  })

  it('reads a number, also one that is written as text, and walks a path into a record', () => {
    const n = part({ kind: 'number', name: 'Open PRs', field: 'open_prs' })
    expect(partValue(n, { open_prs: 7 })).toMatchObject({ found: true, number: 7, share: null })
    expect(partValue(n, { open_prs: '1,204' })).toMatchObject({ found: true, number: 1204 })
    expect(partValue(n, { open_prs: 'many' }).found).toBe(false)
    expect(partValue(n, { open_prs: Number.NaN }).found).toBe(false)
    expect(partValue(n, { open_prs: true }).found).toBe(false)
    expect(partValue(part({ kind: 'number', name: 'Cost', field: 'usage', path: 'today.cost_usd' }), { usage: { today: { cost_usd: 3.5 } } }).number).toBe(3.5)
  })

  it('reads a share against a limit that is a number or a field, up to 1.2 and never under 0', () => {
    expect(partValue(part(ring), { checks_done: 7 })).toMatchObject({ found: true, number: 7, max: 20, share: 0.35 })
    expect(partValue(part(ring), { checks_done: 60 }).share).toBe(DASH.overShare)
    expect(partValue(part(ring), { checks_done: -4 }).share).toBe(0)
    const bar = part({ kind: 'bar', name: 'Spend', field: 'spend', max: 'budget' })
    expect(partValue(bar, { spend: 10, budget: 40 })).toMatchObject({ found: true, max: 40, share: 0.25 })
    expect(partValue(bar, { spend: 10 }).found).toBe(false)
    expect(partValue(bar, { spend: 10, budget: 0 }).found).toBe(false)
    expect(partValue(bar, { spend: 10, budget: 'soon' }).found).toBe(false)
    // A limit that names a field is read from the values themselves, like the field.
    expect(partValue(part({ kind: 'bar', name: 'Spend', field: 'spend', max: 'constructor' }), { spend: 10 }).found).toBe(false)
  })

  it('reads lamps from rows or from words, draws at most twelve and counts them all, and a word outside the list is grey', () => {
    const lamps = part({ kind: 'lamps', name: 'Workers', field: 'workers' })
    expect(partValue(lamps, { workers: [{ state: 'working' }, { state: 'failed' }, 'done', { state: 'exploded' }, { other: 1 }, 4] }))
      .toMatchObject({ found: true, number: 6, lamps: ['working', 'failed', 'done', 'other', 'other', 'other'] })
    // "Needs you" is the room's to say at a Bot's feet, never a lamp's: it is grey like any other word.
    expect(partValue(lamps, { workers: [{ state: 'needs' }, 'needs', { state: 'idle' }] }).lamps).toEqual(['other', 'other', 'idle'])
    // A row's state is read from the row itself: a key that every object has finds nothing.
    expect(partValue(part({ kind: 'lamps', name: 'Jobs', field: 'jobs', key: 'constructor' }), { jobs: [{}, { constructor: 'done' }] }).lamps).toEqual(['other', 'done'])
    const many = partValue(lamps, { workers: Array.from({ length: 30 }, () => ({ state: 'idle' })) })
    expect(many.number).toBe(30)
    expect(many.lamps).toHaveLength(DASH.mostLamps)
    expect(partValue(lamps, { workers: 3 }).found).toBe(false)
    expect(partValue(part({ kind: 'lamps', name: 'Jobs', field: 'jobs', key: 'status' }), { jobs: [{ status: 'failed', state: 'done' }] }).lamps).toEqual(['failed'])
  })

  it('writes the number over a part: the reading, a ring with its limit, a unit after it, a dash when there is none', () => {
    expect(partText(part(ring), partValue(part(ring), { checks_done: 7 }), fmt)).toBe('7/20')
    const spend = part({ kind: 'bar', name: 'Spend', field: 'spend', max: 50, unit: '$' })
    expect(partText(spend, partValue(spend, { spend: 12 }), fmt)).toBe('12 $')
    const n = part({ kind: 'number', name: 'Open PRs', field: 'open_prs' })
    expect(partText(n, partValue(n, { open_prs: 3 }), fmt)).toBe('3')
    expect(partText(n, partValue(n, {}), fmt)).toBe('\u2014')
  })

  it('never writes a number longer than six characters', () => {
    const long = () => '1,000,000,000,000,000,000,000'
    expect(shortNumber(1e21, long)).toBe('1e21')
    expect(shortNumber(1e21, fmt)).toBe('1e+21')
    expect(shortNumber(-123456789, fmt)).toBe('-1e8')
    expect(shortNumber(1234.5678, () => '1.2K')).toBe('1.2K')
    expect(shortNumber(0.000001234, fmt)).toBe('1e-6')
    const huge = part({ kind: 'number', name: 'N', field: 'n', unit: 'ms' })
    expect(partText(huge, partValue(huge, { n: 1e21 }), long)).toBe('1e21 ms')
  })
})

describe('the colours of the parts are never the room\'s colours for a Bot\'s state', () => {
  // The room's four: a Bot that rests (grey), works (the theme's accent), needs the person (amber), failed (red).
  const themes: [string, number[]][] = [
    ['blue accent', [0xa9b1c0, 0x4f8cff, 0xf5a524, 0xe5484d]],
    ['violet accent', [0xa9b1c0, 0x8b5cf6, 0xf59e0b, 0xef4444]],
    ['green accent', [0xa9b1c0, 0x22c55e, 0xeab308, 0xdc2626]],
    ['teal accent', [0xa9b1c0, 0x14b8a6, 0xf97316, 0xf43f5e]],
    ['orange accent', [0xa9b1c0, 0xf97316, 0xfacc15, 0xb91c1c]],
    ['pink accent', [0xa9b1c0, 0xec4899, 0xf59e0b, 0xef4444]],
    ['cyan accent', [0xa9b1c0, 0x06b6d4, 0xf59e0b, 0xef4444]],
  ]
  const apart = (a: number, b: number) => { const d = Math.abs(a - b) % 360; return Math.min(d, 360 - d) }

  it.each(themes)('%s: every part colour stands apart from every state colour, and from the other part colours', (_name, state) => {
    const hues = dataHues(state)
    const mine = [hues.fill, hues.done, hues.failed]
    for (const c of mine) {
      expect(state).not.toContain(c)
      expect(hueOf(c).sat).toBeGreaterThan(0.3)
      // Two degrees are left for a colour's rounding to whole steps of red, green and blue.
      for (const s of state.filter(x => hueOf(x).sat > 0.15)) expect(apart(hueOf(c).hue, hueOf(s).hue)).toBeGreaterThanOrEqual(HUE_APART_DEG - 2)
    }
    expect(new Set(mine).size).toBe(3)
    for (const a of mine) for (const b of mine) if (a !== b) expect(apart(hueOf(a).hue, hueOf(b).hue)).toBeGreaterThanOrEqual(HUE_APART_DEG - 2)
  })

  it('reads a colour\'s hue', () => {
    expect(hueOf(0xff0000).hue).toBe(0)
    expect(hueOf(0x00ff00).hue).toBe(120)
    expect(hueOf(0x0000ff).hue).toBe(240)
    expect(hueOf(0x808080).sat).toBe(0)
  })
})

describe('the rest form is held by its Bot in front of its own body', () => {
  const rows: [string, (DashKind | 'socket')[]][] = [
    ['one ring', ['ring']],
    ['a bar', ['bar']],
    ['the four kinds', ['ring', 'bar', 'lamps', 'number']],
    ['six rings', ['ring', 'ring', 'ring', 'ring', 'ring', 'ring']],
    ['six bars', ['bar', 'bar', 'bar', 'bar', 'bar', 'bar']],
    ['a socket between two', ['number', 'socket', 'ring']],
    ['no part', []],
  ]
  // The front row as the person sees it from the room's middle: the eye, and a Bot's feet.
  const place = crewPlaces(1)[0]
  const eye = PAGES.eyeM
  /** How high the line from the eye to a point of the floor `aheadM` in front of the feet passes, `atM` in front of the feet. */
  const sightTo = (aheadM: number, atM: number) => eye - ((eye - place.feetM) / (place.radiusM - aheadM)) * (place.radiusM - atM)

  it.each(rows)('%s: no wider than its Bot, in front of no ring and no eyes', (_name, kinds) => {
    const row = restRow(kinds)
    const box = restBox(row.wideM)
    // As wide as the Bot's body at most, so it is in front of no neighbour.
    expect(box.wideM).toBeLessThanOrEqual(FORM.restWideM + 1e-9)
    expect(box.wideM).toBeLessThanOrEqual(CREW.heightM * CREW.widthShare + 1e-9)
    expect(box.scale).toBeLessThanOrEqual(FORM.restScale)
    // The ring at the feet: the line from the eye to the ring's near edge passes under the tray's lowest near edge.
    const front = box.centreM + box.deepM / 2
    const bottom = place.feetM + box.liftM - FORM.trayThickM * box.scale
    expect(bottom).toBeGreaterThan(sightTo(CREW.ringM[1], front))
    // The Bot's eyes stand at about six tenths of its height: the tallest part ends under them.
    expect(box.liftM + box.tallM).toBeLessThan(CREW.heightM * 0.55)
  })

  it('stands the parts close at rest, each as wide as its kind, and a pitch apart when taken', () => {
    const row = restRow(['ring', 'bar', 'lamps', 'number'])
    expect(row.xs).toHaveLength(4)
    for (let i = 1; i < 4; i += 1) expect(row.xs[i]).toBeGreaterThan(row.xs[i - 1])
    // No two overlap: from one middle to the next is half of each and the gap.
    expect(row.xs[1] - row.xs[0]).toBeCloseTo(PART_WIDE.ring / 2 + PART_WIDE.bar / 2 + FORM.restGapM, 9)
    expect(row.wideM).toBeLessThan(trayWideM(4))
    expect(row.xs[0] + row.xs[3] + row.xs[1] + row.xs[2]).not.toBeNaN()
    expect(takenRow(4)).toEqual([-1.5, -0.5, 0.5, 1.5].map(v => v * FORM.pitchM))
    expect(takenRow(0)).toEqual([0])
    // With the four kinds it is larger than a tray of four whole pitches would be.
    expect(restBox(row.wideM).scale).toBeGreaterThan(restBox(trayWideM(4)).scale)
  })
})

describe('a tray and what stands where it would be taken', () => {
  it('is refused only by a board in his hand or the card\'s keyboard, never by a board a Bot holds up', () => {
    expect(trayBlock(false, false)).toBeNull()
    expect(trayBlock(true, false)).toBe('board')
    expect(trayBlock(false, true)).toBe('keys')
    expect(trayPress(false, null)).toBe('take')
    expect(trayPress(false, 'board')).toBe('refused')
    expect(trayPress(false, 'keys')).toBe('refused')
    // A taken tray always goes back at a press.
    expect(trayPress(true, 'board')).toBe('back')
  })
  it('watches a board that comes up at a Bot, not one that stays up', () => {
    expect(boardsCameUp(new Set(), new Set(['a']))).toEqual(['a'])
    expect(boardsCameUp(new Set(['a']), new Set(['a']))).toEqual([])
    expect(boardsCameUp(new Set(['a', 'b']), new Set(['a']))).toEqual([])
    expect(boardsCameUp(new Set(['a']), new Set(['a', 'c']))).toEqual(['c'])
  })
  it('goes back only for a board whose picture overlaps the taken tray', () => {
    const tray = [-0.3, -0.6, 0.3, -0.2] as const
    // A placard at a Bot straight ahead, under which the tray stands.
    expect(picturesOverlap([-0.2, -0.3, 0.2, 0.1], tray)).toBe(true)
    // A placard at a Bot far to the side, and one high over the tray.
    expect(picturesOverlap([0.5, -0.5, 0.9, -0.1], tray)).toBe(false)
    expect(picturesOverlap([-0.2, 0.1, 0.2, 0.5], tray)).toBe(false)
    // Edges that only touch do not overlap.
    expect(picturesOverlap([0.3, -0.5, 0.6, -0.3], tray)).toBe(false)
    // He looked away: the two meet only outside the view, where the eye sees neither.
    expect(picturesOverlap([-1.9, 0.0, -1.2, 0.6], [-1.3, -0.9, 0.05, 0.7])).toBe(false)
  })
})

describe('the dashboard takes a press only on what is drawn (D55 5)', () => {
  it('takes a hit on a shown piece, and none on a hidden piece, under a hidden group, or with a hidden material', () => {
    const root = { visible: false, parent: null }
    const body = { visible: true, parent: root }
    const piece = { visible: true, parent: body, material: { visible: true } }
    // The dashboard's own group is hidden (its Bot sits at a desk, or before its first frame): it takes no press (D56 3,
    // small 7). Shown, the pieces under it are judged.
    expect(drawnUnder(piece, root)).toBe(false)
    root.visible = true
    expect(drawnUnder(piece, root)).toBe(true)
    expect(drawnUnder({ visible: false, parent: body }, root)).toBe(false)
    const lip = { visible: false, parent: body }
    expect(drawnUnder({ visible: true, parent: lip, material: {} }, root)).toBe(false)
    // An invisible box round it (the old padded press) is no drawn piece.
    expect(drawnUnder({ visible: true, parent: root, material: { visible: false } }, root)).toBe(false)
  })

  it('is called by each form, in its own frame, on its own group (M139)', () => {
    // Read from the component's source as the compiler parses it, not as text: a comment naming the call is no call.
    const file = resolve(process.cwd(), 'src/pages/xr/workspace/Dash3d.tsx')
    const src = ts.createSourceFile(file, readFileSync(file, 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
    let oneDash: ts.FunctionDeclaration | undefined
    src.forEachChild(n => { if (ts.isFunctionDeclaration(n) && n.name?.text === 'OneDash') oneDash = n })
    expect(oneDash).toBeDefined()
    const calls: string[] = []
    const inFrame = (n: ts.Node, frame: boolean): void => {
      if (ts.isCallExpression(n) && ts.isIdentifier(n.expression)) {
        if (n.expression.text === 'useFrame') { n.arguments.forEach(a => inFrame(a, true)); return }
        if (frame && n.expression.text === 'raysOnDrawn') calls.push(n.arguments[0]?.getText(src) ?? '')
      }
      n.forEachChild(c => inFrame(c, frame))
    }
    if (oneDash) inFrame(oneDash, false)
    expect(calls).toEqual(['g as unknown as RayPiece'])
  })

  it('lets a ray hit only its drawn pieces, keeps the gaps and the outline, and gives a piece its ray back once drawn (D140 D)', () => {
    const own = () => 'hit', none = () => 'none'
    const piece = (over: Partial<RayPiece> = {}): RayPiece => ({ visible: true, parent: null, isMesh: true, userData: {}, raycast: own, children: [], material: { visible: true }, ...over })
    const root: RayPiece = { visible: true, parent: null, userData: {}, raycast: null, children: [] }
    const tray: RayPiece = { visible: false, parent: root, userData: {}, raycast: null, children: [] }
    const hidden = piece({ parent: tray }), shown = piece({ parent: root }), gap = piece({ parent: root, userData: { dashGap: true }, material: { visible: false } })
    // The taken form's outline sets its own ray each frame (on while taken, `NO_RAY` at rest); this walk leaves it.
    const sheetRay = () => 'sheet'
    const outline = piece({ parent: root, userData: { dashOutline: true }, material: { visible: false }, raycast: sheetRay })
    tray.children.push(hidden)
    root.children.push(tray, shown, gap, outline)
    raysOnDrawn(root, none)
    expect(hidden.raycast).toBe(none)
    expect(shown.raycast).toBe(own)
    expect(gap.raycast).toBe(own)
    expect(outline.raycast).toBe(sheetRay)
    // The taken tray's group shown again (the form taken): its piece is hit again.
    tray.visible = true
    raysOnDrawn(root, none)
    expect(hidden.raycast).toBe(own)
    // A gap switched off (the form taken, or the gap wide) is not hit; on again, it is.
    gap.visible = false
    raysOnDrawn(root, none)
    expect(gap.raycast).toBe(none)
    gap.visible = true
    raysOnDrawn(root, none)
    expect(gap.raycast).toBe(own)
  })

  it('gives a mesh its own ray back when its form renders while it is off (D155, M142)', () => {
    // The renderer sets a `userData` prop anew at each render of the form, so the walk keeps no ray there.
    const own = () => 'hit', none = () => 'none'
    const root: RayPiece = { visible: true, parent: null, userData: {}, raycast: null, children: [] }
    const tray: RayPiece = { visible: false, parent: root, userData: {}, raycast: null, children: [] }
    const piece: RayPiece = { visible: true, parent: tray, isMesh: true, userData: { text: 'a' }, raycast: own, children: [], material: { visible: true } }
    const gap: RayPiece = { visible: false, parent: root, isMesh: true, userData: { dashGap: true }, raycast: own, children: [], material: { visible: false } }
    tray.children.push(piece)
    root.children.push(tray, gap)
    raysOnDrawn(root, none)
    expect([piece.raycast, gap.raycast]).toEqual([none, none])
    // A render while both are off: new `userData` objects, as from the JSX.
    piece.userData = { text: 'a' }
    gap.userData = { dashGap: true }
    raysOnDrawn(root, none)
    tray.visible = true
    gap.visible = true
    raysOnDrawn(root, none)
    expect([piece.raycast, gap.raycast]).toEqual([own, own])
  })
})

describe('a gap between a dashboard\'s parts at rest (D56 3)', () => {
  const tops = { number: 0.062, bar: 0.146, ring: 0.146, lamps: 0.088 }
  it('lies between each two neighbouring parts, as high as the lower of them', () => {
    const gaps = restGaps(['number', 'bar', 'ring'], tops)
    expect(gaps).toHaveLength(2)
    for (const g of gaps) expect(g.x1 - g.x0).toBeCloseTo(FORM.restGapM, 6)
    expect(gaps[0].top).toBe(0.062)
    expect(gaps[1].top).toBe(0.146)
    expect(restGaps(['bar'], tops)).toEqual([])
  })
  it('is the dashboard\'s while narrower than half a degree at his eye, and the Bot\'s when wider', () => {
    // At rest a 12 mm gap is drawn at 0.62 of it: 7.4 mm, 0.17 degrees at 2.5 m, 0.28 at 1.5 m.
    expect(gapTakes(FORM.restGapM * 0.62, 2.5)).toBe(true)
    expect(gapTakes(FORM.restGapM * 0.62, 1.5)).toBe(true)
    // Up close, or a wide gap: the Bot's.
    expect(gapTakes(FORM.restGapM * 0.62, 0.8)).toBe(false)
    expect(gapTakes(0.03, 2)).toBe(false)
    expect(gapTakes(0.01, 0)).toBe(false)
  })
  it('changes hands only past its slack: on under 0.45 degrees, off over 0.55 (REVIEW-19 S4)', () => {
    const gap = FORM.restGapM * 0.62
    const at = (deg: number) => gap / Math.tan((deg * Math.PI) / 180)
    // At 0.5 degrees (0.853 m) it keeps what it was.
    expect(gapTakes(gap, at(0.5), true)).toBe(true)
    expect(gapTakes(gap, at(0.5), false)).toBe(false)
    expect(gapTakes(gap, at(0.54), true)).toBe(true)
    expect(gapTakes(gap, at(0.56), true)).toBe(false)
    expect(gapTakes(gap, at(0.46), false)).toBe(false)
    expect(gapTakes(gap, at(0.44), false)).toBe(true)
    // A lean of 4 cm to and fro about the edge hands it over once, not each frame.
    let on = gapTakes(gap, at(0.47))
    let flips = 0
    for (let i = 0; i < 20; i += 1) { const now = gapTakes(gap, at(0.5) + (i % 2 ? 0.04 : -0.04), on); if (now !== on) flips += 1; on = now }
    expect(flips).toBeLessThanOrEqual(1)
  })
})

describe('the taken form stands whole in the lens (D99 2)', () => {
  const eyeY = 1.6
  const lean = (FORM.lipLeanDeg * Math.PI) / 180
  const top = 0.146
  /** Its lowest point and its top, as angles at the eye, in degrees. */
  const angles = (pitch: number, half: number) => {
    const d = TAKEN_DISTANCE_M
    const y = takenTrayTopY(eyeY, d, pitch, half, top)
    const lowY = y - FORM.trayThickM - FORM.lipM * Math.cos(lean)
    const lowAt = d - FORM.trayDeepM / 2 - FORM.lipM * Math.sin(lean)
    return { low: (Math.atan2(lowY - eyeY, lowAt) * 180) / Math.PI, high: (Math.atan2(y + top - eyeY, d) * 180) / Math.PI }
  }
  it('placed for a level head, it is whole in the Aura lens (40.7 degrees tall) at a level head and 9 degrees up and down', () => {
    const half = 40.7 / 2
    // As `Dash3d.tsx` places it: a level head, a lens `TAKEN_LOOK_DEG` shorter at each end.
    const a = angles(0, half - TAKEN_LOOK_DEG)
    expect(a.low).toBeCloseTo(-(half - TAKEN_LOOK_DEG) + TAKEN_MARGIN_DEG, 5)
    for (const head of [0, TAKEN_LOOK_DEG, -TAKEN_LOOK_DEG]) {
      expect(a.low).toBeGreaterThan(head - half)
      expect(a.high).toBeLessThan(head + half)
    }
  })
  it('a head turned far up keeps its top in the lens', () => {
    const a = angles(60, 20)
    expect(a.high).toBeLessThanOrEqual(60 + 20 - TAKEN_MARGIN_DEG + 1e-9)
  })
  it('taken looking 19 degrees down at a tray, it is whole in the lens at the take (D103 2)', () => {
    const half = 40.7 / 2
    const a = angles(-19, half - TAKEN_LOOK_DEG)
    expect(a.low).toBeGreaterThan(-19 - half)
    expect(a.high).toBeLessThan(-19 + half)
    // Placed for a level head instead, its top would stand over the lens's top at the take: the old fault.
    expect(angles(0, half - TAKEN_LOOK_DEG).high).toBeGreaterThan(-19 + half)
  })
})

describe('a taken form that is cut comes whole again, by the held board\'s rule (D103 2)', () => {
  const free = { pinching: false, pointed: false, moving: false }
  const rest = 500
  it('cut, with his look at rest for the board\'s 500 ms after the take, it is placed again', () => {
    expect(takenComes(false, free, 2000, 1400, 1000, rest)).toBe(true)
    expect(takenComes(false, free, 2000, 1600, 1000, rest)).toBe(false)
  })
  it('the rest counts only from the take', () => {
    expect(takenComes(false, free, 2000, 0, 1700, rest)).toBe(false)
  })
  it('whole, it never moves', () => {
    expect(takenComes(true, free, 9000, 0, 0, rest)).toBe(false)
  })
  it('it never starts to move while a pinch is down, a pointer is on it, or his head moves', () => {
    for (const hold of [{ ...free, pinching: true }, { ...free, pointed: true }, { ...free, moving: true }]) {
      expect(takenComes(false, hold, 9000, 0, 0, rest)).toBe(false)
    }
  })
  it('a glide under way runs to its end under his dot and his turning head; only a pinch stops it (D108 3)', () => {
    expect(glideGoes(free)).toBe(true)
    expect(glideGoes({ ...free, pointed: true })).toBe(true)
    expect(glideGoes({ ...free, moving: true })).toBe(true)
    expect(glideGoes({ ...free, pinching: true })).toBe(false)
  })
  it('the glide is eased at both ends, as the board\'s, and ends where it goes', () => {
    expect(glideShare(0)).toBe(0)
    expect(glideShare(0.5)).toBeCloseTo(0.5, 9)
    expect(glideShare(1)).toBe(1)
    expect(glideShare(2)).toBe(1)
    expect(glideShare(0.1)).toBeLessThan(0.1)
  })
})

describe('the taken form fits itself into the lens by its drawn box (D99 2)', () => {
  const eye = [0, 1.6, 0] as const
  type V = [number, number, number]
  /** A form's own box (half width `w`, from `y0` to `y1`, depth from `z0` to `z1` ahead), turned `yawDeg` about the eye. */
  const own = (y0: number, y1: number, { w = 0.4, z0 = -0.85, z1 = -0.7, yawDeg = 0 } = {}): V[] => {
    const t = (yawDeg * Math.PI) / 180
    const out: V[] = []
    for (const x of [-w, w]) for (const y of [y0, y1]) for (const z of [z0, z1]) out.push([x * Math.cos(t) - z * Math.sin(t), y, x * Math.sin(t) + z * Math.cos(t)])
    return out
  }
  /** A box laid along the room's axes round those corners, as the build read it before M71. */
  const roomBox = (c: V[]): V[] => { const lo = [0, 1, 2].map(i => Math.min(...c.map(p => p[i]))), hi = [0, 1, 2].map(i => Math.max(...c.map(p => p[i]))); const out: V[] = []; for (const x of [lo[0], hi[0]]) for (const y of [lo[1], hi[1]]) for (const z of [lo[2], hi[2]]) out.push([x, y, z]); return out }
  const ang = (y: number, h: number) => (Math.atan2(y - 1.6, h) * 180) / Math.PI
  it('stands farther when it is taller than the lens leaves', () => {
    const step = fitTaken(own(1.2, 2.0), eye, 0, 11.35)
    expect(step.k).toBeGreaterThan(1)
    expect(step.dy).toBe(0)
  })
  it('moves so its lowest corner is the margin over the lens\'s foot', () => {
    const step = fitTaken(own(1.35, 1.6), eye, 0, 11.35)
    expect(step.k).toBe(1)
    // The lowest corner is the nearest bottom one.
    expect(ang(1.35 + step.dy, Math.hypot(0.4, 0.7))).toBeCloseTo(-11.35 + TAKEN_MARGIN_DEG, 6)
  })
  it('a form turned 45 degrees reads as the same form facing 0 (M71); a box along the room\'s axes did not', () => {
    // A tray of six parts: about 0.8 m wide.
    for (const yawDeg of [15, 30, 45]) {
      const turned = fitTaken(own(1.35, 1.55, { yawDeg }), eye, 0, 11.35)
      const straight = fitTaken(own(1.35, 1.55), eye, 0, 11.35)
      expect(turned.k).toBeCloseTo(straight.k, 9)
      expect(turned.dy).toBeCloseTo(straight.dy, 9)
    }
    const axes = fitTaken(roomBox(own(1.35, 1.55, { yawDeg: 45 })), eye, 0, 11.35)
    expect(axes.k !== 1 || Math.abs(axes.dy - fitTaken(own(1.35, 1.55), eye, 0, 11.35).dy) > 0.01).toBe(true)
  })
  it('a form wider than the lens stands farther, turned 0 or 45 degrees alike (w1112)', () => {
    // Six parts: about 0.9 m wide at 0.8 m, wider than the Aura's 61 degrees less the margins.
    for (const yawDeg of [0, 45]) {
      const t = (yawDeg * Math.PI) / 180
      const step = fitTaken(own(1.45, 1.55, { w: 0.45, yawDeg }), eye, 0, 11.35, { yawRad: -t, halfWideDeg: 30.7 })
      expect(step.k).toBeGreaterThan(1)
    }
    // One part, narrow: the width asks nothing.
    expect(fitTaken(own(1.45, 1.55, { w: 0.08 }), eye, 0, 11.35, { yawRad: 0, halfWideDeg: 30.7 }).k).toBe(1)
  })
  it('four looks bring a form that is too tall whole, and stop (Q6)', () => {
    let k = 1, dy = 0
    const at = (): V[] => own(1.2 + dy, 2.0 + dy, { z0: -0.85 * k, z1: -0.7 * k }).map(([x, y, z]) => [x, y, z])
    for (let n = 0; n < 4; n += 1) {
      const step = fitTaken(at(), eye, 0, 11.35)
      if (step.k !== 1) k *= step.k
      else if (Math.abs(step.dy) > 0.001) dy += step.dy
      else break
    }
    const c = at()
    const angles = c.map(([x, y, z]) => ang(y, Math.hypot(x, z)))
    expect(Math.max(...angles) - Math.min(...angles)).toBeLessThanOrEqual(2 * (11.35 - TAKEN_MARGIN_DEG) + 1e-6)
    expect(k).toBeLessThanOrEqual(4)
  })
})

describe('a tray that a nearer Bot hides (D99 2)', () => {
  const eye = [0, 1.6, 0] as const
  const front = { feet: [0, 0, -1.5] as const, top: [0, 1.0, -1.5] as const, radiusM: 0.11 }
  it('is hidden when a body stands on the line from the eye, nearer than it', () => {
    expect(hiddenBy(eye, [0, 0.2, -2.6], [front])).toBe(true)
  })
  it('is not when the body stands aside, behind it, or under the line', () => {
    expect(hiddenBy(eye, [0.6, 0.2, -2.6], [front])).toBe(false)
    expect(hiddenBy(eye, [0, 0.2, -1.2], [front])).toBe(false)
    expect(hiddenBy(eye, [0, 1.5, -2.6], [front])).toBe(false)
    expect(hiddenBy(eye, [0, 0.2, -2.6], [])).toBe(false)
  })
  it('a body just in front of the tray counts too (Q7)', () => {
    const close = { feet: [0, 0, -2.55] as const, top: [0, 1.0, -2.55] as const, radiusM: 0.11 }
    expect(hiddenBy(eye, [0, 0.2, -2.6], [close])).toBe(true)
  })
  it('with slack, a body just past its radius still hides', () => {
    const edge = { ...front, feet: [0.115, 0, -1.5] as const, top: [0.115, 1.0, -1.5] as const }
    expect(hiddenBy(eye, [0, 0.2, -2.6], [edge])).toBe(false)
    expect(hiddenBy(eye, [0, 0.2, -2.6], [edge], HIDDEN_SLACK.slackM)).toBe(true)
  })
})

describe('a look is the same only from the same place (R9)', () => {
  const at = { eye: [0, 1.6, 0] as const, yawRad: 0, pitchDeg: 0 }
  it('the same way from the same eye is the same look; a turn of 2 degrees is not', () => {
    expect(sameLook(at, at, 2)).toBe(true)
    expect(sameLook({ ...at, yawRad: (2.5 * Math.PI) / 180 }, at, 2)).toBe(false)
    expect(sameLook({ ...at, pitchDeg: 2.5 }, at, 2)).toBe(false)
  })
  it('a step or a lean of 10 cm is a new look, facing the same way', () => {
    expect(sameLook({ ...at, eye: [0.1, 1.6, 0] }, at, 2)).toBe(false)
    expect(sameLook({ ...at, eye: [0, 1.6, -0.1] }, at, 2)).toBe(false)
    expect(sameLook({ ...at, eye: [0.05, 1.6, 0] }, at, 2)).toBe(true)
  })
})

describe('the fit runs again once, when the lip\'s words are laid out (M82)', () => {
  const done = { n: 4, box: 'a', at: 1000, again: 0 }
  it('a new box soon after the placement fits it again', () => {
    expect(fitAgain(done, 'b', 1100, false)).toBe(true)
  })
  it('never a second time, never while a pinch is down, never late, never for the same box, never mid-fit', () => {
    expect(fitAgain({ ...done, again: 1 }, 'c', 1100, false)).toBe(false)
    expect(fitAgain(done, 'b', 1100, true)).toBe(false)
    expect(fitAgain(done, 'b', 1000 + REFIT.ms, false)).toBe(false)
    expect(fitAgain(done, 'a', 1100, false)).toBe(false)
    expect(fitAgain({ ...done, n: 2 }, 'b', 1100, false)).toBe(false)
  })
})

describe('a hidden tray does not bob (M72)', () => {
  it('a Bot whose edge sways on the line changes it at most once', () => {
    // The hider's edge goes in and out of the plain radius every frame, but stays inside the slack.
    let s = { hidden: false, since: null as number | null }
    let changes = 0
    for (let f = 0; f < 120; f += 1) {
      const t = f * 16
      const inside = f % 2 === 0
      const n = hiddenStep(s.hidden, inside, true, s.since, t, false)
      if (n.hidden !== s.hidden) changes += 1
      s = n
    }
    expect(changes).toBeLessThanOrEqual(1)
  })
  it('a new answer counts only once it has held 300 ms', () => {
    let s = hiddenStep(false, true, true, null, 0, false)
    expect(s.hidden).toBe(false)
    s = hiddenStep(s.hidden, true, true, s.since, 299, false)
    expect(s.hidden).toBe(false)
    s = hiddenStep(s.hidden, true, true, s.since, 300, false)
    expect(s.hidden).toBe(true)
    // Clear only past the slack.
    expect(hiddenStep(true, false, true, null, 1000, false)).toEqual({ hidden: true, since: null })
  })
  it('nothing changes while a pointer is down on it or lies on it', () => {
    expect(hiddenStep(false, true, true, 0, 5000, true)).toEqual({ hidden: false, since: null })
    expect(hiddenStep(true, false, false, 0, 5000, true)).toEqual({ hidden: true, since: null })
  })
})

describe('the layouts\' cases, one file for the tab and the gateway (P20b)', () => {
  type Want = { broken: string } | { kind: string; name: string; unit: string }
  const file = JSON.parse(readFileSync(resolve(process.cwd(), '../test/fixtures/xr_layout_cases.json'), 'utf-8')) as { cases: { name: string; layout: unknown; why: string; parts: Want[] }[] }
  it('has cases', () => { expect(file.cases.length).toBeGreaterThan(20) })
  for (const c of file.cases) {
    it(c.name, () => {
      const l = readLayout(c.layout)
      expect(l.why).toBe(c.why)
      expect(l.parts.map(p => (isPart(p) ? { kind: p.kind, name: p.name, unit: p.unit, path: [...p.path], max: p.max, key: p.key } : { broken: p.broken }))).toEqual(c.parts)
    })
  }
})
