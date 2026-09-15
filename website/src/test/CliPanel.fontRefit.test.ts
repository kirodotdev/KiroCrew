import { describe, it, expect, vi } from 'vitest'
import { remeasureAndFit } from '../components/CliPanel'
import { version as xtermVersion } from '@xterm/xterm/package.json'

import type { Terminal } from '@xterm/xterm'
import type { FitAddon } from '@xterm/addon-fit'

/**
 * Regression tests for the terminal web-font refit.
 *
 * `remeasureAndFit` works around the absence of a public xterm "re-measure now"
 * API by toggling `term.options.fontFamily` to trigger xterm's internal
 * CharSizeService re-measure, then refitting. These tests pin both halves of
 * that contract: the toggle/guard logic here, and the xterm version below.
 */
function makeTerm(element: { offsetParent: unknown } | null, fontFamily = "'JetBrains Mono', monospace") {
  const sets: string[] = []
  let ff = fontFamily
  return {
    element,
    options: {
      get fontFamily() { return ff },
      set fontFamily(v: string) { ff = v; sets.push(v) },
    },
    _sets: sets,
  }
}

describe('remeasureAndFit', () => {
  it('toggles fontFamily monospace->original to force a re-measure, then fits, on a laid-out pane', () => {
    const term = makeTerm({ offsetParent: {} }) // non-null offsetParent => visible / laid out
    const fit = { fit: vi.fn() }
    remeasureAndFit(term as unknown as Terminal, fit as unknown as FitAddon)
    expect(term._sets).toEqual(['monospace', "'JetBrains Mono', monospace"]) // transient then restored
    expect(term.options.fontFamily).toBe("'JetBrains Mono', monospace")
    expect(fit.fit).toHaveBeenCalledTimes(1)
  })

  it('skips a display:none / detached pane (offsetParent === null) — matches the sibling refit guards', () => {
    const term = makeTerm({ offsetParent: null })
    const fit = { fit: vi.fn() }
    remeasureAndFit(term as unknown as Terminal, fit as unknown as FitAddon)
    expect(term._sets).toEqual([])         // no font toggle / re-measure on a hidden pane
    expect(fit.fit).not.toHaveBeenCalled() // and never fits a zero-size pane
  })

  it('no-ops when the terminal is not yet attached (element null)', () => {
    const term = makeTerm(null)
    const fit = { fit: vi.fn() }
    remeasureAndFit(term as unknown as Terminal, fit as unknown as FitAddon)
    expect(term._sets).toEqual([])
    expect(fit.fit).not.toHaveBeenCalled()
  })

  it('is pinned to xterm 5.5.x — the version whose CharSizeService re-measures on a fontFamily change', () => {
    // If this fails, xterm was bumped past 5.5. Before updating the pin,
    // re-verify remeasureAndFit's fontFamily toggle still forces a
    // CharSizeService re-measure (otherwise the clipping regresses
    // silently — there is no public re-measure API to depend on instead).
    expect(xtermVersion).toMatch(/^5\.5\./)
  })
})
