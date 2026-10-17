/**
 * Glass thickness ladder (components/Glass.tsx `thickness`, issue #16299).
 *
 * One scale, five steps: blur radius (Glass.tsx) and tint (index.css, one token
 * per step per polarity) move together, so a call site picks a step and never a
 * blur or an alpha of its own. The two halves live in the file that owns each
 * value; this test pins them against the table the maintainer fixed, in both
 * places, so neither can drift alone:
 *
 *   step        blur   dark tint                 light tint
 *   ultrathin   2px    rgba(34, 33, 37, 0.30)    rgba(236, 236, 236, 0.35)
 *   thin        4px    rgba(30, 30, 34, 0.40)    rgba(240, 240, 240, 0.45)   (default)
 *   regular     8px    rgba(28, 28, 32, 0.50)    rgba(243, 243, 243, 0.55)
 *   thick       14px   rgba(26, 27, 31, 0.60)    rgba(245, 245, 245, 0.65)
 *   ultrathick  28px   rgba(24, 25, 30, 0.78)    rgba(247, 247, 247, 0.82)
 *
 * The tint colours keep a pane over the plain page at ONE overall colour across
 * the ladder (light: #f8f8f8 over white): c = page + (c_thin - page) * (a_thin / a),
 * anchored on white in light and on `--bg` (#12141a) in dark. The arithmetic
 * is re-derived here from the thin step so a future retune has to change the
 * anchor on purpose, not one number by accident.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { DEFAULT_GLASS_THICKNESS, GLASS_THICKNESS, GLASS_THICKNESSES, Glass, type GlassThickness } from '../components/Glass'
import { resetDisplacementMapCache } from '../components/ui/liquid-glass'

const INDEX_CSS = readFileSync(resolve(__dirname, '../index.css'), 'utf8')
const GLASS_SRC = readFileSync(resolve(__dirname, '../components/Glass.tsx'), 'utf8')

type Rgba = [number, number, number, number]
const LADDER: Record<GlassThickness, { blur: number; dark: Rgba; light: Rgba }> = {
  ultrathin: { blur: 2, dark: [34, 33, 37, 0.30], light: [236, 236, 236, 0.35] },
  thin: { blur: 4, dark: [30, 30, 34, 0.40], light: [240, 240, 240, 0.45] },
  regular: { blur: 8, dark: [28, 28, 32, 0.50], light: [243, 243, 243, 0.55] },
  thick: { blur: 14, dark: [26, 27, 31, 0.60], light: [245, 245, 245, 0.65] },
  ultrathick: { blur: 28, dark: [24, 25, 30, 0.78], light: [247, 247, 247, 0.82] },
}

const rgba = ([r, g, b, a]: Rgba) => `rgba(${r}, ${g}, ${b}, ${a.toFixed(2)})`

/** The `:root { … }` and `[data-mode="light"] { … }` blocks that carry the glass tokens. */
function tokenBlock(selector: string): string {
  const re = new RegExp(`${selector.replace(/[[\]"=]/g, m => `\\${m}`)} \\{([^}]*--glass-tint-thin[^}]*)\\}`)
  const m = re.exec(INDEX_CSS)
  expect(m, `glass token block for ${selector}`).not.toBeNull()
  return m![1]
}

class FakeResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

beforeEach(() => {
  resetDisplacementMapCache()
  vi.stubGlobal('ResizeObserver', FakeResizeObserver)
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('Glass thickness ladder', () => {
  it('names five steps, thinnest first, with thin as the default', () => {
    expect([...GLASS_THICKNESSES]).toEqual(['ultrathin', 'thin', 'regular', 'thick', 'ultrathick'])
    expect(DEFAULT_GLASS_THICKNESS).toBe('thin')
    // Blur grows monotonically along the ladder.
    const blurs = GLASS_THICKNESSES.map(s => GLASS_THICKNESS[s].frost)
    expect(blurs).toEqual([...blurs].sort((a, b) => a - b))
  })

  it('pins the blur of every step in Glass.tsx', () => {
    for (const step of GLASS_THICKNESSES) expect(GLASS_THICKNESS[step].frost, step).toBe(LADDER[step].blur)
    // Blur is the only optical number this component owns; the old per-variant
    // frost is gone, so a variant cannot quietly re-introduce a second ladder.
    expect(GLASS_SRC).not.toMatch(/panel: \{ frost:/)
    expect(GLASS_SRC).not.toMatch(/chip: \{ frost:/)
  })

  it('pins the tint of every step in index.css, dark and light', () => {
    const dark = tokenBlock(':root')
    const light = tokenBlock('[data-mode="light"]')
    for (const step of GLASS_THICKNESSES) {
      expect(dark, `dark ${step}`).toContain(`--glass-tint-${step}: ${rgba(LADDER[step].dark)};`)
      expect(light, `light ${step}`).toContain(`--glass-tint-${step}: ${rgba(LADDER[step].light)};`)
    }
    // The default a pane wears with no step class is thin, on both tokens.
    expect(dark).toMatch(/--glass-tint-step: var\(--glass-tint-thin\);\s*--glass-tint: var\(--glass-tint-thin\);/)
    // Light inherits those two from :root (same element), so it must NOT restate
    // them with a different step.
    expect(light).not.toMatch(/--glass-tint(-step)?: var\(--glass-tint-(?!thin\))/)
  })

  it('keeps a pane over the plain page at one overall colour across the ladder', () => {
    // c_step = page + (c_thin - page) * (a_thin / a_step), per channel, rounded.
    const check = (page: Rgba, pick: (s: GlassThickness) => Rgba) => {
      const thin = pick('thin')
      for (const step of GLASS_THICKNESSES) {
        const got = pick(step)
        for (let ch = 0; ch < 3; ch++) {
          const want = page[ch] + (thin[ch] - page[ch]) * (thin[3] / got[3])
          expect(Math.abs(got[ch] - want), `${step} channel ${ch}: ${got[ch]} vs ${want.toFixed(2)}`).toBeLessThanOrEqual(0.5)
        }
      }
    }
    check([255, 255, 255, 1], s => LADDER[s].light)
    // Dark anchors on the page colour `--bg` of the default dark theme.
    expect(INDEX_CSS).toMatch(/--bg:\s*#12141a/i)
    check([0x12, 0x14, 0x1a, 1], s => LADDER[s].dark)
  })

  it('selects a step with a host class that sets both the step and the live tint', () => {
    for (const step of GLASS_THICKNESSES) {
      expect(INDEX_CSS).toContain(`.glass-${step} { --glass-tint-step: var(--glass-tint-${step}); --glass-tint: var(--glass-tint-${step}); }`)
    }
    // The modifiers mix into the host's step and are declared AFTER the step
    // classes, so on a host carrying both the modifier's --glass-tint wins.
    const stepIdx = INDEX_CSS.indexOf('.glass-ultrathick {')
    const modIdx = INDEX_CSS.indexOf('.glass-accent {')
    expect(stepIdx).toBeGreaterThan(-1)
    expect(modIdx).toBeGreaterThan(stepIdx)
  })

  it('renders the step as the host class and the blur, thin by default', () => {
    const { container, rerender } = render(<Glass radius={12}>x</Glass>)
    let host = container.firstElementChild as HTMLElement
    expect(host.classList.contains('glass-thin')).toBe(true)
    const frostBox = () => host.querySelector<HTMLElement>(':scope > span[aria-hidden="true"] > span[style*="backdrop-filter"]')
    expect(frostBox()?.style.backdropFilter).toBe('blur(4px) saturate(1.55)')

    for (const step of GLASS_THICKNESSES) {
      rerender(<Glass radius={12} thickness={step} className="glass-accent extra">x</Glass>)
      host = container.firstElementChild as HTMLElement
      // Step class first, then the caller's classes, on the same host.
      expect(host.className.split(/\s+/)).toEqual(expect.arrayContaining(['liquid-glass', `glass-${step}`, 'glass-accent', 'extra']))
      expect(host.className.indexOf(`glass-${step}`)).toBeLessThan(host.className.indexOf('glass-accent'))
      // Exactly one step class, so two thicknesses can never compete.
      expect(host.className.split(/\s+/).filter(c => GLASS_THICKNESSES.some(s => c === `glass-${s}`))).toHaveLength(1)
      expect(frostBox()?.style.backdropFilter).toBe(`blur(${LADDER[step].blur}px) saturate(1.55)`)
    }
  })

  it('is worn thick by the progress panes above the composer, and by nothing else yet', () => {
    // Maintainer direction on #16299: the composer keeps the default; the
    // panes that show progress above it (sub-agent tray, task bar, workflow
    // bar) go `thick` so their dense rows stay readable
    // over the transcript passing under them.
    const read = (rel: string) => readFileSync(resolve(__dirname, rel), 'utf8')
    for (const rel of ['../pages/chat/SubagentProgressBar.tsx', '../pages/chat/WorkflowProgressBar.tsx', '../pages/chat/TaskProgressBar.tsx']) {
      expect(read(rel), rel).toMatch(/<Glass\s+variant="chip"\s+thickness="thick"/)
    }
    // The composer itself keeps the default step.
    expect(read('../components/ChatInput.tsx')).not.toMatch(/thickness=/)
  })
})
