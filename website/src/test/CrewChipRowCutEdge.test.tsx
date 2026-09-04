/**
 * The pinned-crew chip row's cut edge must be marked ON the boundary, never
 * ACROSS the chips.
 *
 * The row is one nowrap line with `overflow:hidden`, so the chip at the boundary
 * is cut rather than dropped (see InstanceTabBar.CrewChipRow). An alpha mask over
 * the row's trailing pixels marks that cut by ERASING it: a chip's unread badge
 * is its trailing element, so any cut reaches the badge first, and a ramp wider
 * than the 16px badge dissolves the count the chip exists to show. At the rungs
 * where the crew name has already collapsed, the badge is nearly all the chip has.
 *
 * These pin the shape of the replacement, because both halves are invisible to
 * every other test: jsdom performs no layout, so no rendered-component test can
 * observe a cut, and the cue itself lives in a stylesheet.
 */
import { describe, it, expect } from 'vitest'
import { readFile } from 'node:fs/promises'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = dirname(fileURLToPath(import.meta.url))
const css = () => readFile(join(__dirname, '..', 'index.css'), 'utf8')
const tsx = () => readFile(join(__dirname, '..', 'components', 'InstanceTabBar.tsx'), 'utf8')

describe('crew chip row cut edge', () => {
  it('paints no alpha mask over the chip row', async () => {
    const s = await css()
    // Every rule that names the row, with its declarations.
    const rules = s.match(/\.crew-chip-row[^{]*\{[^}]*\}/g) || []
    expect(rules.length, 'expected at least one rule for the chip row').toBeGreaterThan(0)
    for (const rule of rules) {
      expect(rule, 'a mask over the row erases the unread badge at the cut').not.toMatch(
        /mask-image/,
      )
    }
  })

  it('marks the cut with a 1px rule that takes no layout', async () => {
    const s = await css()
    const rule = /\.crew-chip-row\[data-cut='true'\]::after\s*\{([^}]*)\}/.exec(s)
    expect(rule, 'expected the cut-edge rule').not.toBeNull()
    const body = rule![1]
    // Absolute so the cue cannot change the row's width — which is the very
    // measurement that decides what got cut.
    expect(body).toMatch(/position:\s*absolute/)
    expect(body).toMatch(/width:\s*1px/)
    expect(body).toMatch(/right:\s*0/)
    expect(body).toMatch(/pointer-events:\s*none/)
  })

  it('drives the cue off the measured clip, not off a constant', async () => {
    const t = await tsx()
    expect(t, 'the row carries the cue hook').toMatch(/className="crew-chip-row /)
    expect(t, "data-cut reflects the measurement").toMatch(
      /data-cut=\{clipped\.size > 0 \? 'true' : 'false'\}/,
    )
  })
})

describe('pinned chips adapt to their own track', () => {
  /**
   * The identity group's width is a function of the WINDOW, not of its content
   * (both side tracks are `minmax(0,1fr)` and the group's inline-size containment
   * keeps its content out of the track calculation). Widening it would have to be
   * paid for by the centred search, so the group has to absorb a shortage inside
   * its own track: the chips give up name width rather than one chip being sliced.
   */
  it('lets a pinned chip give up name width, down to a legible floor', async () => {
    const t = await tsx()
    expect(t, 'the pinned row asks for shrinkable chips').toMatch(
      /shrinkable=\{!entry\.state \|\| entry\.state === 'connected'\}/,
    )
    // The floor has to sit on the CHIP: a flex item's `min-width:auto` resolves to
    // its content-based minimum, which for nowrap text is the full name, so leaving
    // it auto means the chip never shrinks. `min-w-0` is the opposite failure — the
    // row squeezes the chip past its own content and the name paints outside the
    // border. Both spellings are literal so Tailwind's content scan sees them.
    expect(t, 'a shrinkable chip declares a 5ch-based floor').toMatch(
      /min-w-\[calc\(5ch\+30px\)\]/,
    )
    expect(t, 'the floor accounts for the unread badge').toMatch(
      /min-w-\[calc\(5ch\+54px\)\]/,
    )
    expect(t, 'the chip must not be shrinkable below its own content').not.toMatch(
      /shrinkable\s*\?\s*'min-w-0/,
    )
    // One source of truth for the floor: the name carries no minimum of its own.
    expect(t, 'the name span carries no competing floor').toMatch(
      /className="tb-drop-crew-name truncate max-w-\[140px\]"/,
    )
  })

  it('keeps the crew on screen at full width', async () => {
    const t = await tsx()
    // The active chip is rendered by `Switcher`, outside the row, and must NOT be
    // shrinkable: it is the one label that says where you are.
    const active = /className="tb-crew-active-chip"[\s\S]{0,200}?\/>/.exec(t)
      || /active\s*\n\s*onSelect=\{\(\) => onSelect\(active\.id\)\}[\s\S]{0,200}?\/>/.exec(t)
    expect(active, 'expected the active-chip render site').not.toBeNull()
    expect(active![0], 'the active chip must not be shrinkable').not.toMatch(/shrinkable/)
  })
})
