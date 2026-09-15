import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, act } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import FollowUpBar, { FOLLOWUP_CHIP_STAGGER_MS, FOLLOWUP_CHIP_STAGGER_MAX_STEPS, FOLLOWUP_CHIP_HOP_DURATION_MS } from '../components/FollowUpBar'

/**
 * The whole option set arrives in one render (options are parsed only once the
 * turn ends), so without a stagger the row paints in a single frame. These
 * tests lock in the ladder, its ceiling, which element carries the entrance,
 * and that the entrance does not replay after the row has settled.
 */

// jsdom polyfill: the scroll layout observes its strip to track scrollability.
if (typeof globalThis.ResizeObserver === 'undefined') {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver
}

const ENTRANCE_CLASS = 'animate-chip-hop'

/** The chip's flex item: the button when standalone, the wrapper when split. */
function chipItems(container: HTMLElement): HTMLElement[] {
  const row = container.querySelector('.flex') as HTMLElement
  return Array.from(row.children) as HTMLElement[]
}

function delayMs(el: HTMLElement): number {
  return el.style.animationDelay === '' ? 0 : parseInt(el.style.animationDelay, 10)
}

afterEach(() => { vi.useRealTimers() })

describe('follow-up chip entrance stagger', () => {
  it('gives every chip the hop entrance on a strictly increasing delay ladder', () => {
    const { container } = render(
      <FollowUpBar options={['Alpha', 'Beta', 'Gamma', 'Delta']} picked={new Set()} onSelect={() => {}} />,
    )
    const chips = chipItems(container)
    expect(chips).toHaveLength(4)

    for (const chip of chips) expect(chip.className).toContain(ENTRANCE_CLASS)
    chips.forEach((chip, i) => expect(delayMs(chip)).toBe(i * FOLLOWUP_CHIP_STAGGER_MS))
    for (let i = 1; i < chips.length; i++) {
      expect(delayMs(chips[i])).toBeGreaterThan(delayMs(chips[i - 1]))
    }
  })

  it('leaves the first chip with no inline delay so it enters immediately', () => {
    const { container } = render(<FollowUpBar options={['Only']} picked={new Set()} onSelect={() => {}} />)
    const [chip] = chipItems(container)
    expect(chip.style.animationDelay).toBe('')
    expect(chip.className).toContain(ENTRANCE_CLASS)
  })

  it('caps the ladder so a long option row does not trail off for most of a second', () => {
    const options = Array.from({ length: FOLLOWUP_CHIP_STAGGER_MAX_STEPS + 4 }, (_, i) => `Option ${i}`)
    const { container } = render(<FollowUpBar options={options} picked={new Set()} onSelect={() => {}} />)
    const chips = chipItems(container)
    const ceiling = FOLLOWUP_CHIP_STAGGER_MAX_STEPS * FOLLOWUP_CHIP_STAGGER_MS

    expect(delayMs(chips[FOLLOWUP_CHIP_STAGGER_MAX_STEPS])).toBe(ceiling)
    for (const chip of chips.slice(FOLLOWUP_CHIP_STAGGER_MAX_STEPS)) {
      expect(delayMs(chip)).toBe(ceiling)
    }
  })

  it('animates the split-button wrapper, not its inner label button', () => {
    // onSend present and quickSend off ⇒ every chip renders as a split button
    // (label + "send now" segment). Animating the inner button would slide the
    // label out from under its own send arrow.
    const { container } = render(
      <FollowUpBar options={['Alpha', 'Beta']} picked={new Set()} onSelect={() => {}} onSend={() => {}} />,
    )
    const chips = chipItems(container)
    for (const chip of chips) {
      expect(chip.tagName).toBe('SPAN')
      expect(chip.className).toContain(ENTRANCE_CLASS)
      const inner = Array.from(chip.querySelectorAll('button')) as HTMLElement[]
      expect(inner).toHaveLength(2)
      for (const button of inner) expect(button.className).not.toContain(ENTRANCE_CLASS)
    }
  })

  it('does not replay the entrance once the row has settled', () => {
    vi.useFakeTimers()
    // Quick Send on with nothing picked renders plain buttons; picking one
    // flips the others to the split-button shape, which remounts them. The
    // entrance must not fire again for that — the options never changed.
    const options = ['Alpha', 'Beta']
    const { container, rerender } = render(
      <FollowUpBar options={options} picked={new Set()} onSelect={() => {}} onSend={() => {}} quickSend />,
    )
    expect(chipItems(container)[0].className).toContain(ENTRANCE_CLASS)

    act(() => { vi.advanceTimersByTime(2000) })
    rerender(
      <FollowUpBar options={options} picked={new Set(['Alpha'])} onSelect={() => {}} onSend={() => {}} quickSend />,
    )

    const chips = chipItems(container)
    expect(chips[0].tagName).toBe('SPAN') // the shape really did swap
    for (const chip of chips) expect(chip.className).not.toContain(ENTRANCE_CLASS)
  })

  it('replays the entrance when a new option set arrives', () => {
    vi.useFakeTimers()
    const { container, rerender } = render(
      <FollowUpBar options={['Alpha', 'Beta']} picked={new Set()} onSelect={() => {}} />,
    )
    act(() => { vi.advanceTimersByTime(2000) })
    expect(chipItems(container)[0].className).not.toContain(ENTRANCE_CLASS)

    rerender(<FollowUpBar options={['Gamma', 'Delta']} picked={new Set()} onSelect={() => {}} />)
    const chips = chipItems(container)
    for (const chip of chips) expect(chip.className).toContain(ENTRANCE_CLASS)
    expect(delayMs(chips[1])).toBe(FOLLOWUP_CHIP_STAGGER_MS)
  })

  it('keeps the entrance out of the way of a same-content re-render', () => {
    vi.useFakeTimers()
    // The caller rebuilds the options array every render; a fresh array with
    // identical content must not restart the entrance.
    const { container, rerender } = render(
      <FollowUpBar options={['Alpha', 'Beta']} picked={new Set()} onSelect={() => {}} />,
    )
    act(() => { vi.advanceTimersByTime(2000) })
    rerender(<FollowUpBar options={['Alpha', 'Beta']} picked={new Set()} onSelect={() => {}} />)
    for (const chip of chipItems(container)) expect(chip.className).not.toContain(ENTRANCE_CLASS)
  })
})

/**
 * The class above only animates because the utility and its reduced-motion
 * companion exist. Both live outside the component, so a green component suite
 * would still ship a chip with a class that resolves to nothing.
 */
describe('follow-up chip entrance styling contract', () => {
  it('declares the hop keyframe and utility with a backwards fill', () => {
    const config = readFileSync(resolve(process.cwd(), 'tailwind.config.js'), 'utf-8')
    // The overshoot midpoint IS the effect — a two-endpoint keyframe is a fade.
    expect(config).toMatch(/'chip-hop':\s*\{[\s\S]{0,200}?'55%':\s*\{[^}]*translateY\(-4px\)/)
    expect(config).toMatch(/'chip-hop':\s*'chip-hop [^']*backwards'/)
  })

  it('keeps the utility duration equal to the settle window CHIP_HOP_DURATION_MS is built from', () => {
    // The settle window is the ladder ceiling plus one animation. If the CSS
    // duration grows past the constant, `animating` flips false while the
    // deepest chip is still mid-hop, its class is pulled, and the chip snaps
    // from an in-flight translateY to rest — a visible pop on the one chip the
    // stagger was for. The two live in different files, so pin them together.
    const config = readFileSync(resolve(process.cwd(), 'tailwind.config.js'), 'utf-8')
    const declared = config.match(/'chip-hop':\s*'chip-hop \.?(\d+)s/)
    expect(declared).not.toBeNull()
    // `.42s` → 420ms.
    expect(Math.round(parseFloat(`0.${declared![1]}`) * 1000)).toBe(FOLLOWUP_CHIP_HOP_DURATION_MS)
  })

  it('zeroes the stagger delay under prefers-reduced-motion', () => {
    // The global reduced-motion rule zeroes duration only; with `backwards`
    // fill a delayed chip would otherwise stay invisible for its whole delay.
    const css = readFileSync(resolve(process.cwd(), 'src/index.css'), 'utf-8')
    expect(css).toMatch(/prefers-reduced-motion:reduce\)\{[^}]*\.animate-chip-hop[^}]*animation-delay:0s !important/)
  })
})
