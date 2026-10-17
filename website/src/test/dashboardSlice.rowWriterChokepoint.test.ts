import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

/**
 * The row-write chokepoint stays a chokepoint.
 *
 * Issue #11149 is a per-slot recency problem: a `/api/chat/slots` reply applied
 * by `applySlots` overwrites any single-slot write that landed while it
 * travelled. `patchSlotRow` fixes that by stamping every such write, and the fix
 * is only as good as the guarantee that writers GO THROUGH it. Ten writers do
 * today; the eleventh is the one that matters, and it will be written by someone
 * who has never read this file.
 *
 * So the rule is enforced on the source rather than on a reviewer's attention: a
 * reducer may not take a mutable row reference out of `state.slots` on its own.
 * A lookup that genuinely only READS says so on the line, with a
 * `// row-read: <why>` marker — which makes the exemption a visible choice in
 * the diff instead of an omission nobody sees.
 *
 * This is a text rule, and it is worth being plain about the limit: it catches
 * the shapes a writer in this file has actually used, not every conceivable way
 * to reach a row. All three are policed, because the pre-fix code used two of
 * them: `patchSlotSourceLinks` reached its rows by ITERATING `state.slots`, so a
 * guard that watched only `.find(` would have missed the very writer whose
 * replacement motivated it, and index access escapes the same way.
 */

const SLICE = join(process.cwd(), 'src', 'store', 'dashboardSlice.ts')

/** The ways a reducer in this file has reached, or could reach, one row. Named,
 *  so a failure says which shape was used rather than only which line. */
const ROW_ACCESS: { shape: string; re: RegExp }[] = [
  { shape: 'lookup by key', re: /state\.slots\b[^\n]*?\.find\(/ },
  { shape: 'iteration', re: /for\s*\([^)]*\bof\s+[^)]*state\.slots\b/ },
  { shape: 'index', re: /(state\.slots|state\.slots\s*\?\?\s*\[\s*\]\s*\))\s*\[/ },
]

/** Says on the line that this access is read-only, and why. */
const READ_ONLY_MARKER = /\/\/\s*row-read:\s*\S/
/** Marks the chokepoint's own access — the only place allowed to yield a row to mutate. */
const CHOKEPOINT_MARKER = /\/\/\s*row-write:\s*via patchSlotRow/

describe('dashboardSlice row writes go through patchSlotRow', () => {
  const lines = readFileSync(SLICE, 'utf8').split('\n')
  const accesses = lines
    .flatMap((text, i) => ROW_ACCESS
      .filter(a => a.re.test(text))
      .map(a => ({ text, line: i + 1, shape: a.shape })))

  it('finds the accesses it is meant to police', () => {
    // A regex that silently stopped matching would make the cases below pass
    // vacuously, so assert each shape is still detectable — the two the
    // chokepoint itself uses by their own marker, and `index` against a sample,
    // since the file deliberately contains no index access to find.
    const marked = accesses.filter(a => CHOKEPOINT_MARKER.test(a.text))
    expect(marked.map(a => a.shape).sort()).toEqual(['iteration', 'lookup by key'])
    const index = ROW_ACCESS.find(a => a.shape === 'index')!.re
    expect(index.test('const row = state.slots[0]')).toBe(true)
    expect(index.test('const rows = (state.slots ?? [])[0]')).toBe(true)
    // and does not fire on the whole-list reads that are not row access
    expect(index.test('const prev = state.slots ?? []')).toBe(false)
    expect(index.test('state.slots = merged')).toBe(false)
    expect(index.test('new Map((state.slots ?? []).map(s => [s.key, s]))')).toBe(false)
  })

  it('has no unannotated row access outside the chokepoint', () => {
    const offenders = accesses
      .filter(a => !CHOKEPOINT_MARKER.test(a.text) && !READ_ONLY_MARKER.test(a.text))
      .map(a => `dashboardSlice.ts:${a.line} [${a.shape}]: ${a.text.trim()}`)
    expect(offenders).toEqual([])
  })

  it('keeps the stamp inside the chokepoint, where a writer cannot forget it', () => {
    const source = readFileSync(SLICE, 'utf8')
    // Both entry points must stamp; a helper that mutates without stamping would
    // reintroduce the defect while looking like the fix.
    for (const helper of ['const patchSlotRow =', 'const patchSlotRowsWhere =']) {
      const start = source.indexOf(helper)
      expect(start, `${helper} not found`).toBeGreaterThan(-1)
      const body = source.slice(start, source.indexOf('\n}', start))
      expect(body).toContain('stampSlotWrite(state')
    }
  })

  it('stamps through a prefix, so no key is exempt from the guard', () => {
    // The record must carry no bare slot key: an `isUnsafeKey` bail-out would
    // leave a `__proto__`-keyed slot unstamped and silently unprotected.
    const source = readFileSync(SLICE, 'utf8')
    const start = source.indexOf('const stampSlotWrite =')
    const body = source.slice(start, source.indexOf('\n}', start))
    expect(body).toContain('state.slotWrittenAt[stampKey(key)]')
    expect(body).not.toContain('isUnsafeKey')
  })
})
