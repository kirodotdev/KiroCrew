import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

/** The two bounce instruments are only worth their overlay line if they are
 *  actually FED. Their unit tests call the module's functions directly, so they
 *  stay green when the call site in the virtualizer is deleted -- verified by
 *  mutation, and it is exactly how an instrument goes silently blind: the
 *  overlay simply never prints the line, and the absence reads as "no defect
 *  here" rather than as "nothing is measuring".
 *
 *  The device is the only place the effect can be observed, so there is no
 *  honest unit test for it. A source pin is the next best thing, and it is
 *  written to fail for the way this actually breaks: the call being removed or
 *  commented out during debugging and not put back. */

const virtualizer = () =>
  readFileSync(
    resolve(__dirname, '../hooks/virtualizer/useVirtualChat.ts'),
    'utf8',
  )

/** Comment lines are stripped BEFORE searching. A comment that merely NAMES the
 *  call must not satisfy the pin -- a lesson learned the hard way in this file's
 *  neighbours, where prose describing a mechanism was mistaken for the
 *  mechanism itself. */
const code = (src: string): string =>
  src
    .split('\n')
    .filter((l) => {
      const t = l.trim()
      return t !== '' && !t.startsWith('//') && !t.startsWith('*') && !t.startsWith('/*')
    })
    .join('\n')

describe('scroll inspector wiring: the bounce instruments are fed', () => {
  it('reports the top spacer, which is what separates a reprice from a scroll', () => {
    const src = code(virtualizer())
    expect(src).toContain('devSpacer(offsetBefore, windowRange.start)')
  })

  it('imports the reporter, so the call cannot be a stale reference', () => {
    const src = code(virtualizer())
    expect(src).toMatch(/import\s*{[^}]*\bdevSpacer\b[^}]*}\s*from\s*'\.\.\/\.\.\/dev\/scrollInspector'/)
  })

  it('logs every programmatic scroll write through the one chokepoint', () => {
    // `moved` is derived from this log line, so losing it loses the reading
    // that named the culprit on the device.
    const src = code(virtualizer())
    expect(src).toContain("devLog('WRITE'")
    expect(src).toContain("->${Math.round(top)}")
  })

  it('feeds the spacer from the OFFSET TREE, not from a DOM read', () => {
    // `offsetBefore` is the tree's own answer for everything above the window.
    // Reading the spacer element instead would measure the fiction AFTER the
    // browser resolved it, which is the very thing being investigated.
    const src = code(virtualizer())
    expect(src).toContain('const offsetBefore = offsetIndex.offsetOf(windowRange.start)')
  })
})

/** The two conditions that stop the corrector CREATING the displacement it claims
 *  to repair. Both are pinned at source because the harness genuinely cannot reach
 *  either state, and that is a measured limit rather than an excuse:
 *
 *  - `displaced == 0` while `delta != 0` requires scrollTop to drift BETWEEN an
 *    anchor's capture and its consume. `act(() => rerender(...))` flushes layout
 *    effects synchronously, so a test can never get between the two — recorded in
 *    useVirtualChat.prependAnchor.test.tsx as its own gap.
 *  - The giveup branch fires only after the restore deadline passes with the stored
 *    row never arriving, on a real session id, with the placement change it
 *    triggers actually moving scrollTop.
 *
 *  The device frame both were written from, entering a session whose stored row was
 *  gone (`ENTER RESTORE.giveup n=0`):
 *
 *    CORR d=-1016 owed=-1262 res=246 painted=1   WRITE resize 6404->5388
 *    CORR d=1448  owed=1484  res=-36 painted=1   WRITE resize 6590->8038
 *
 *  Both painted, ~2.4kpx of jolt on entry, and `res` small in each — the residual
 *  could never have found it, because both figures came from the same abandoned
 *  capture and so agreed with each other rather than with the glass. */
describe('anchor correction: refuses to move a reader who has not moved', () => {
  it('gates the write on the MEASURED displacement, not only on the owed delta', () => {
    const src = code(virtualizer())
    expect(src).toContain('const displaced = newTop - pending.top')
    expect(src).toMatch(/Math\.abs\(displaced\) > 0\.5\s*&&\s*Math\.abs\(delta\) > 0\.5/)
  })

  it('drops the correction that straddles a restore giving up', () => {
    const src = code(virtualizer())
    // Set where the giveup actually happens, next to the placement change that
    // moves scrollTop -- not at some later convenience point.
    expect(src).toMatch(/restoreGaveUpRef\.current = true[\s\S]{0,120}stickRef\.current = followOutput/)
    expect(src).toContain('const abandoned = restoreGaveUpRef.current')
    expect(src).toMatch(/if \(!abandoned && /)
  })

  it('CONSUMES the abandoned flag instead of latching it on', () => {
    // An authorization that can only ever turn on is not an authorization -- the
    // same defect this file already fixed once in the walk poll. Left latched, one
    // giveup would silence the corrector for the rest of the mount.
    const src = code(virtualizer())
    expect(src).toMatch(/if \(abandoned\) \{\s*restoreGaveUpRef\.current = false/)
  })

  it('accumulates our own writes at the ONE chokepoint every write passes', () => {
    // The correction can only tell the reader's finger from its own earlier hand if
    // every writer registers, and fourteen call sites write this scroller. Counted
    // inside writeScrollTop rather than at the call sites so a new writer cannot
    // forget -- the same reason the WRITE log lives there.
    const src = code(virtualizer())
    expect(src).toContain('writeSumRef.current += top - el.scrollTop')
    // Before the write, while el.scrollTop is still the old value. Registering
    // afterwards cannot recover the delta without forcing a layout.
    expect(src).toMatch(/writeSumRef\.current \+= top - el\.scrollTop[\s\S]{0,200}el\.scrollTo\(\{ top, behavior \}\)/)
  })

  it('carries the write total ON the anchor, so it is read as a difference', () => {
    // A cumulative counter used absolutely drifts further off the longer a session
    // stays open -- which no short test would ever show. Pairing it with the capture
    // is what makes it a window.
    const src = code(virtualizer())
    expect(src).toContain('capturedWriteSum: pending.writeSum')
    expect(src).toContain('currentWriteSum: writeSumRef.current')
    expect(src).toContain('offsetOfRef.current, writeSumRef.current)')
  })
})
