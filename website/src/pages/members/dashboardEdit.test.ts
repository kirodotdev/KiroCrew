import { describe, expect, it } from 'vitest'
import { changeRequestOutcome, declaredRoot, holdDuration, isBlockId, isFieldPath, manifestFields, parseCorrection, parseFieldPath, pickedMessage, placePopover, popoverMaxHeight, popoverWidth, PICKED_MESSAGE_TYPE, rootField, scriptLabel } from './dashboardEdit'
import { changeRequestMessage } from './dashboardChangeRequest.prompt'
// One table for both parsers; test/test_dashboard_edit_mode.py runs the same rows in Python.
import shared from '../../../../test/fixtures/dashboard_edit_paths.json'

describe('the shared path and component-id table', () => {
  it.each(shared.paths)('parses $path the same as the gateway', ({ path, root }) => {
    expect(parseFieldPath(path)?.root ?? null).toBe(root)
    expect(isFieldPath(path)).toBe(root !== null)
    if (root !== null) expect(rootField(path)).toBe(root)
  })

  it.each(shared.block_ids)('checks component id $id the same as the gateway', ({ id, ok }) => {
    expect(isBlockId(id)).toBe(ok)
  })
})

const TEMPLATE = {
  id: 't', version: 1, title: '', description: '', source: 'builtin' as const,
  fields: {
    accepted: { type: 'number' as const, source: { fold: 'workstreams', path: 'accepted' } },
    verdict: { type: 'string' as const, source: { agentic: true as const } },
    p99: { type: 'number' as const, source: { fold: 'custom', path: 'latest.p99.data.ms' } },
  },
}

describe('dashboard edit mode rules', () => {
  it('says a hold as a duration, dropping the zero parts', () => {
    expect(holdDuration(7200)).not.toMatch(/7200|second|sec|\bs\b/)
    expect(holdDuration(7200)).toMatch(/^2\s?h/)
    expect(holdDuration(86400)).toMatch(/^1\s?d/)
    expect(holdDuration(5400)).toMatch(/^1\s?h.*30\s?m/)
  })

  it("names a job's script without the host's folders", () => {
    expect(scriptLabel('/srv/jobs/digest.py:run')).toBe('digest.py:run')
    expect(scriptLabel('C:\\srv\\jobs\\digest.py:run')).toBe('digest.py:run')
    expect(scriptLabel('\\srv\\jobs\\digest.py:run')).toBe('digest.py:run')
    expect(scriptLabel('~/jobs/digest.py:run')).toBe('~/jobs/digest.py:run')
    expect(scriptLabel('jobs/digest.py')).toBe('jobs/digest.py')
    expect(scriptLabel('')).toBe('')
    // Each root form alone, and near misses that are not a host path.
    expect(scriptLabel('/x')).toBe('x')
    expect(scriptLabel('\\x')).toBe('x')
    expect(scriptLabel('C:/a/x')).toBe('x')
    expect(scriptLabel('c:\\a\\x')).toBe('x')
    expect(scriptLabel('1:/a/x')).toBe('1:/a/x')
    expect(scriptLabel('C:a/x')).toBe('C:a/x')
    expect(scriptLabel('C:')).toBe('C:')
    expect(scriptLabel('\u00e9:/a/x')).toBe('\u00e9:/a/x')
  })

  it('reads both manifest shapes', () => {
    const fromTemplate = manifestFields(TEMPLATE)
    expect(fromTemplate.map(f => [f.name, f.kind])).toEqual([['accepted', 'fold'], ['p99', 'custom'], ['verdict', 'agentic']])
    const fromPackage = manifestFields({ types: { x: { type: 'text', label: 'X', source: { agentic: true } } } })
    expect(fromPackage).toEqual([{ name: 'x', type: 'text', kind: 'agentic', fold: '', path: '', label: 'X' }])
  })

  it('takes only a well-formed picked message, and keeps only names from it', () => {
    expect(pickedMessage({ type: 'other', seq: 1 })).toBeNull()
    const ok = pickedMessage({ type: PICKED_MESSAGE_TYPE, seq: 3, path: 'items[0].n', rect: { x: 1, y: 2, w: 3, h: 4 }, block: 'tasks', blockRect: { x: 0, y: 0, w: 9, h: 9 }, blockFields: ['items', '<b>', 7] })
    expect(ok).toEqual({ seq: 3, path: 'items[0].n', rect: { x: 1, y: 2, w: 3, h: 4 }, block: 'tasks', blockRect: { x: 0, y: 0, w: 9, h: 9 }, blockFields: ['items'] })
    const bad = pickedMessage({ type: PICKED_MESSAGE_TYPE, seq: 3, path: 'Evil text', rect: null, block: 'Bad Id' })
    expect(bad?.path).toBeNull()
    expect(bad?.block).toBe('')
  })

  it('refuses a path whose root field the page does not declare', () => {
    const fields = manifestFields(TEMPLATE)
    expect(declaredRoot('accepted', fields)?.name).toBe('accepted')
    expect(declaredRoot('secret.x', fields)).toBeNull()
  })

  it('never covers the value: right, then left, then below, then above', () => {
    const size = { w: 100, h: 50 }
    const bounds = { w: 400, h: 400 }
    expect(placePopover({ x: 10, y: 100, w: 20, h: 10 }, size, bounds).side).toBe('right')
    expect(placePopover({ x: 350, y: 100, w: 40, h: 10 }, size, bounds).side).toBe('left')
    expect(placePopover({ x: 50, y: 10, w: 340, h: 10 }, size, bounds).side).toBe('below')
    expect(placePopover({ x: 50, y: 380, w: 340, h: 10 }, size, bounds).side).toBe('above')
  })

  it('keeps an unconfirmed send apart from a sent or refused one', () => {
    expect(changeRequestOutcome('dispatched')).toBe('sent')
    expect(changeRequestOutcome('queued')).toBe('sent')
    expect(changeRequestOutcome('refused')).toBe('refused')
    for (const s of ['unknown', 'response-late', 'transport-error']) expect(changeRequestOutcome(s)).toBe('unconfirmed')
  })

  it('fits the lineage popover inside a 320px pane', () => {
    expect(popoverWidth(1200)).toBe(360)
    const w = popoverWidth(320)
    expect(w).toBe(312)
    const place = placePopover({ x: 150, y: 40, w: 30, h: 20 }, { w, h: 200 }, { w: 320, h: 600 })
    expect(place.x).toBeGreaterThanOrEqual(4)
    expect(place.x + w).toBeLessThanOrEqual(316)
  })

  it('keeps a popover that fits no side inside a 320 x 400 pane', () => {
    const w = popoverWidth(320)
    const place = placePopover({ x: 20, y: 190, w: 30, h: 20 }, { w, h: 280 }, { w: 320, h: 400 })
    expect(place.x).toBeGreaterThanOrEqual(4)
    expect(place.x + w).toBeLessThanOrEqual(316)
    expect(place.y).toBeGreaterThanOrEqual(4)
    expect(popoverMaxHeight(400)).toBe(392)
  })

  it('parses a correction by the field type', () => {
    expect(parseCorrection('4.5', 'number')).toEqual({ value: 4.5 })
    expect(parseCorrection('four', 'number')).toBeNull()
    expect(parseCorrection('yes', 'boolean')).toEqual({ value: true })
    expect(parseCorrection('[1,2]', 'array')).toEqual({ value: [1, 2] })
    expect(parseCorrection('{}', 'array')).toBeNull()
    expect(parseCorrection('  green ', 'string')).toEqual({ value: 'green' })
    // A string can be cleared; a number or boolean has no empty value.
    expect(parseCorrection('  ', 'string')).toEqual({ value: '' })
    expect(parseCorrection('', 'number')).toBeNull()
    expect(parseCorrection('', 'boolean')).toBeNull()
    // Boolean words in any case, with spaces around them; anything else is refused.
    expect(parseCorrection('TRUE', 'boolean')).toEqual({ value: true })
    expect(parseCorrection(' yes ', 'bool')).toEqual({ value: true })
    expect(parseCorrection('1', 'boolean')).toEqual({ value: true })
    expect(parseCorrection('No', 'boolean')).toEqual({ value: false })
    expect(parseCorrection('0', 'boolean')).toEqual({ value: false })
    expect(parseCorrection('false', 'boolean')).toEqual({ value: false })
    for (const bad of ['yess', 'y', '2', 'true false', 'nope']) expect(parseCorrection(bad, 'boolean')).toBeNull()
  })

  it('carries the board, component, field and page version to the Bot', () => {
    const text = changeRequestMessage(' make it red ', { board: 'project-report', block: 'tasks', path: 'accepted', version: 14 })
    expect(text).toBe('make it red\n\n[Dashboard change request · board project-report · component tasks · field accepted · page v14]')
  })
})
