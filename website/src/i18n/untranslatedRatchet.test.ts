/**
 * The untranslated-string gate's two comparisons, tested directly.
 *
 * `check-i18n-strings.mjs` runs eslint over ~540 source files before it compares
 * anything, so its logic has no cheap coverage from the outside — which is exactly why
 * the direction of a comparison, or a wrong path prefix, can change without a test
 * noticing. Two contracts are asserted here:
 *
 *   - `diffAgainstBaseline` — the ASYMMETRY. Growth fails; improvement does not force an
 *     edit. That is a deliberate loosening, and a loosening with no test is
 *     indistinguishable from a bug.
 *   - `parseAddedLines` + `findingsOnAddedLines` — the STRICT half that replaced the
 *     removed downward guard. A literal on a line the branch wrote fails with no
 *     baseline to raise. Its failure mode is silent: a wrong prefix or a missed hunk
 *     shape reports zero findings forever and reads as "clean". Both were real bugs
 *     during development, caught by injecting a string into a real file rather than by
 *     the suite, so the cases they broke on are pinned here.
 *
 * All three functions are pure, so these cases cost nothing.
 */

import { describe, it, expect } from 'vitest'

import {
  ALL_LINES,
  classify,
  diffAgainstBaseline,
  findingsOnAddedLines,
  grewVersusBase,
  parseAddedLines,
} from '../../scripts/check-i18n-strings.mjs'

const f = (total: number) => ({ total })

describe('parseAddedLines', () => {
  it('reads a single-line hunk with the count omitted', () => {
    // `+26` with no count means exactly one line, and getting this wrong silently
    // exempts every one-line addition.
    const added = parseAddedLines(
      '--- a/website/src/a.ts\n+++ b/website/src/a.ts\n@@ -25,0 +26 @@ export function x() {\n+const y = 1\n',
    )
    expect([...added['website/src/a.ts']]).toEqual([26])
  })

  it('expands a multi-line hunk to every line it covers', () => {
    const added = parseAddedLines('+++ b/f.ts\n@@ -1,0 +4,3 @@\n')
    expect([...added['f.ts']]).toEqual([4, 5, 6])
  })

  it('records a file with no added lines as present but empty', () => {
    // A pure deletion: `+0,0`. The file must still appear, so callers can tell "touched
    // but added nothing" from "not touched".
    const added = parseAddedLines('+++ b/f.ts\n@@ -3,2 +2,0 @@\n')
    expect(added['f.ts']).toBeInstanceOf(Set)
    expect(added['f.ts'].size).toBe(0)
  })

  it('keeps hunks attributed to the right file across a multi-file diff', () => {
    const added = parseAddedLines(
      '+++ b/a.ts\n@@ -1 +1 @@\n+++ b/b.ts\n@@ -9,0 +10,2 @@\n',
    )
    expect([...added['a.ts']]).toEqual([1])
    expect([...added['b.ts']]).toEqual([10, 11])
  })

  it('ignores a deleted file’s /dev/null target', () => {
    const added = parseAddedLines('--- a/gone.ts\n+++ /dev/null\n@@ -1,5 +0,0 @@\n')
    expect(Object.keys(added)).toEqual([])
  })

  it('is not fooled by a diff body line that looks like a hunk header', () => {
    // Added content can itself contain `@@ -1 +1 @@` — a test fixture, or this very
    // file. Body lines start with `+`, headers do not.
    const added = parseAddedLines('+++ b/f.ts\n@@ -1,0 +5 @@\n+@@ -900 +900 @@\n')
    expect([...added['f.ts']]).toEqual([5])
  })
})

describe('findingsOnAddedLines', () => {
  const report = [{ rel: 'a/b.tsx', messages: [{ line: 10, message: 'old' }, { line: 20, message: 'new' }] }]

  it('reports only findings on lines the branch wrote', () => {
    const found = findingsOnAddedLines(report, { 'website/src/a/b.tsx': new Set([20]) })
    expect(found).toEqual([{ file: 'a/b.tsx', line: 20, message: 'new' }])
  })

  it('joins the eslint path to the git path with the src prefix', () => {
    // The bug this guards: eslint reports relative to `website/src`, git relative to the
    // repo root. A wrong prefix makes the gate report zero findings forever while
    // looking green, which is indistinguishable from "no violations".
    expect(findingsOnAddedLines(report, { 'website/a/b.tsx': new Set([20]) })).toEqual([])
    expect(findingsOnAddedLines(report, { 'a/b.tsx': new Set([20]) })).toEqual([])
  })

  it('ignores a file the branch did not touch, however many findings it has', () => {
    expect(findingsOnAddedLines(report, {})).toEqual([])
  })

  it('ignores a touched file whose added lines carry no finding', () => {
    expect(findingsOnAddedLines(report, { 'website/src/a/b.tsx': new Set([1, 2, 3]) })).toEqual([])
  })

  it('attributes every line of an untracked file to the branch', () => {
    const found = findingsOnAddedLines(report, { 'website/src/a/b.tsx': ALL_LINES })
    expect(found.map((x) => x.line)).toEqual([10, 20])
  })

  describe('multi-line findings', () => {
    // eslint anchors a finding at its OPENING line, so a five-line template literal is
    // reported at `line`. Editing its third line touches no line the finding is anchored
    // to — and a long interpolated template is exactly the shape most likely to be
    // hiding a rewritten user-visible string.
    const spanning = [{ rel: 'a/b.tsx', messages: [{ line: 10, endLine: 14, message: 'tpl' }] }]

    it('matches an edit to an interior line of the span', () => {
      const found = findingsOnAddedLines(spanning, { 'website/src/a/b.tsx': new Set([12]) })
      expect(found).toEqual([{ file: 'a/b.tsx', line: 10, message: 'tpl' }])
    })

    it('matches an edit to the closing line of the span', () => {
      expect(findingsOnAddedLines(spanning, { 'website/src/a/b.tsx': new Set([14]) })).toHaveLength(1)
    })

    it('does not match a line just outside the span', () => {
      expect(findingsOnAddedLines(spanning, { 'website/src/a/b.tsx': new Set([9]) })).toEqual([])
      expect(findingsOnAddedLines(spanning, { 'website/src/a/b.tsx': new Set([15]) })).toEqual([])
    })

    it('falls back to the single line when endLine is absent or malformed', () => {
      const noEnd = [{ rel: 'a/b.tsx', messages: [{ line: 7, message: 'x' }] }]
      expect(findingsOnAddedLines(noEnd, { 'website/src/a/b.tsx': new Set([7]) })).toHaveLength(1)
      // `endLine` before `line` would otherwise produce an empty range and match nothing.
      const backwards = [{ rel: 'a/b.tsx', messages: [{ line: 7, endLine: 3, message: 'x' }] }]
      expect(findingsOnAddedLines(backwards, { 'website/src/a/b.tsx': new Set([7]) })).toHaveLength(1)
    })

    it('reports a spanning finding only once, however many of its lines were edited', () => {
      const found = findingsOnAddedLines(spanning, { 'website/src/a/b.tsx': new Set([10, 11, 12, 13, 14]) })
      expect(found).toHaveLength(1)
    })
  })
})

describe('diffAgainstBaseline', () => {
  it('reports a file that gained strings as grown', () => {
    const { grew, shrank } = diffAgainstBaseline({ 'a.ts': f(5) }, { 'a.ts': f(3) })
    expect(grew).toEqual(['  a.ts: 3 → 5'])
    expect(shrank).toEqual([])
  })

  it('reports a file that lost strings as shrunk, NOT as a failure signal', () => {
    // The whole point of the change: `grew` is what the gate exits non-zero on, so an
    // improvement has to land in `shrank` and nowhere else.
    const { grew, shrank } = diffAgainstBaseline({ 'a.ts': f(1) }, { 'a.ts': f(4) })
    expect(grew).toEqual([])
    expect(shrank).toEqual(['  a.ts: 4 → 1'])
  })

  it('treats a file missing from the baseline as a ceiling of zero', () => {
    // A newly added file carrying untranslated copy must still fail; the relaxation
    // is about not being forced to re-snapshot, not about admitting new debt.
    const { grew } = diffAgainstBaseline({ 'new.ts': f(2) }, {})
    expect(grew).toEqual(['  new.ts: 0 → 2'])
  })

  it('counts a file fully cleaned up, and so absent from the live report, as shrunk', () => {
    const { grew, shrank } = diffAgainstBaseline({}, { 'gone.ts': f(7) })
    expect(grew).toEqual([])
    expect(shrank).toEqual(['  gone.ts: 7 → 0'])
  })

  it('does not report a zero-count baseline entry as shrunk', () => {
    // `0 → 0` is not an improvement, and listing it would make the informational
    // output grow without limit as files are driven to zero.
    const { shrank } = diffAgainstBaseline({}, { 'clean.ts': f(0) })
    expect(shrank).toEqual([])
  })

  it('is stable when nothing moved', () => {
    const { grew, shrank } = diffAgainstBaseline({ 'a.ts': f(3) }, { 'a.ts': f(3) })
    expect(grew).toEqual([])
    expect(shrank).toEqual([])
  })

  it('separates growth from improvement in a mixed change', () => {
    const { grew, shrank } = diffAgainstBaseline(
      { 'up.ts': f(9), 'down.ts': f(1), 'same.ts': f(2) },
      { 'up.ts': f(4), 'down.ts': f(6), 'same.ts': f(2) },
    )
    expect(grew).toEqual(['  up.ts: 4 → 9'])
    expect(shrank).toEqual(['  down.ts: 6 → 1'])
  })

  it('sorts the improvement list so the informational output is deterministic', () => {
    const { shrank } = diffAgainstBaseline(
      { 'b.ts': f(1), 'a.ts': f(1) },
      { 'b.ts': f(2), 'a.ts': f(2), 'c.ts': f(2) },
    )
    expect(shrank).toEqual(['  a.ts: 2 → 1', '  b.ts: 2 → 1', '  c.ts: 2 → 0'])
  })
})

describe('grewVersusBase', () => {
  /**
   * The check that does not rely on line attribution at all. eslint anchors a finding to
   * the LITERAL, so an edit to the surrounding context — `console.log(` becoming
   * `setStatus(` on its own line, with the string on the next — turns an exempt site into
   * a real one without touching the line the finding sits on. Counting per file against
   * the base sees it; `findingsOnAddedLines` cannot, by construction.
   *
   * The base count is computed LIVE from the base content, never read from the committed
   * ledger, so a stale ceiling cannot launder a regression through it.
   */
  it('fails a touched file that gained findings', () => {
    expect(grewVersusBase({ 'a.ts': 4 }, { 'a.ts': 3 })).toEqual(['  a.ts: 3 → 4'])
  })

  it('passes a touched file that lost findings', () => {
    expect(grewVersusBase({ 'a.ts': 1 }, { 'a.ts': 3 })).toEqual([])
  })

  it('passes a touched file that is unchanged in count', () => {
    // A same-count swap inside one file is NOT caught here — that is what
    // `findingsOnAddedLines` is for. The two are complementary, and neither alone is
    // sufficient.
    expect(grewVersusBase({ 'a.ts': 3 }, { 'a.ts': 3 })).toEqual([])
  })

  it('treats a file absent from the head report as zero, not as missing', () => {
    // Fully cleaned up: no eslint entry at all. Must not read as a gain.
    expect(grewVersusBase({}, { 'a.ts': 3 })).toEqual([])
  })

  it('fails a file that is new on the branch, whose base count is zero', () => {
    expect(grewVersusBase({ 'new.ts': 2 }, { 'new.ts': 0 })).toEqual(['  new.ts: 0 → 2'])
  })

  it('ignores files the branch did not touch, however many findings they hold', () => {
    // Only touched files are linted at the base, so untouched files never appear in the
    // base map. Iterating the base map rather than the head report is what keeps the
    // whole-tree debt out of this check.
    expect(grewVersusBase({ 'untouched.ts': 900 }, {})).toEqual([])
  })

  it('reports deterministically when several files grew', () => {
    expect(grewVersusBase({ 'b.ts': 2, 'a.ts': 2 }, { 'b.ts': 1, 'a.ts': 1 }))
      .toEqual(['  a.ts: 1 → 2', '  b.ts: 1 → 2'])
  })
})

describe('classify', () => {
  /**
   * The categories are what makes the baseline a worklist rather than a number, and
   * `check-source-strings.mjs` / the translation driver both split work by them. They
   * are order-sensitive: a template literal that also looks like an object property
   * must land in `template`, because its shape has to change before it can be keyed.
   */
  it('routes each shape to the category that describes how it gets fixed', () => {
    expect(classify('disallow literal string: `${n} items`')).toBe('template')
    expect(classify("['a', 'b']")).toBe('array')
    expect(classify("title: 'Run'")).toBe('object-prop')
    expect(classify("setError('failed')")).toBe('status-call')
    expect(classify('aria-label="Close"')).toBe('attribute')
    expect(classify("ok ? 'Yes' : 'No'")).toBe('expression')
    expect(classify('Ready to deploy')).toBe('prose')
  })

  it('strips the eslint message prefix before classifying', () => {
    expect(classify('disallow literal string: Ready')).toBe('prose')
  })

  it('prefers template over object-prop when a value is both', () => {
    expect(classify('title: `${n} items`')).toBe('template')
  })
})
