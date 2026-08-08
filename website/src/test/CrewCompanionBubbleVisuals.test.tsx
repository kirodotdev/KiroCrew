/**
 * The bubble's three deliberate visual decisions.
 *
 * These were chosen while watching the live companion on a desktop, and every one of
 * them is the kind of thing a later "tidy-up" undoes without noticing: the arrow looks
 * like an accidental deletion, an always-visible ✕ looks more discoverable, and moving
 * the countdown back out of the card looks like cleaner separation. Each was wrong for
 * a concrete reason recorded below, so each gets a guard.
 *
 * Two of the three are CSS facts, so they are asserted against the stylesheet text.
 * That is deliberate: jsdom does not apply the real cascade, and a rendering assertion
 * that silently passes because no CSS loaded would be worse than none.
 */
import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import { readFileSync } from 'node:fs'

import { Bubble } from '../apps/crew-companion/Bubble'

// Resolved from the vitest root (website/), not import.meta.url -- the test
// environment does not serve this module over file://.
const CSS = readFileSync('src/apps/crew-companion/bubble.css', 'utf8')

describe('bubble: no arrow', () => {
  it('renders no arrow element for any kind', () => {
    // Removed on the user's explicit instruction: the bubble already sits directly
    // above the companion, so the pointer added a fussier second shape for nothing.
    for (const kind of ['break', 'reminder', 'session-done', 'approval'] as const) {
      const { container, unmount } = render(
        <Bubble text="hello" kind={kind} onDismiss={() => {}} />,
      )
      expect(container.querySelector('.cc-bubble-arrow')).toBeNull()
      unmount()
    }
  })

  it('has no arrow rule left in the stylesheet', () => {
    // The class was declared TWICE once, and CSS merged the two into a stray white
    // rectangle. Leaving a dead rule behind invites that hybrid to come back.
    expect(CSS).not.toMatch(/^\.cc-bubble-arrow\s*\{/m)
  })
})

describe('bubble: the ✕ is hover-revealed', () => {
  it('is transparent at rest', () => {
    const rule = CSS.match(/\.cc-bubble-x\s*\{[^}]*\}/)?.[0] ?? ''
    expect(rule, '.cc-bubble-x rule not found').not.toBe('')
    // `\b` after 0 would also match the `0` in `0.55`, which is the very value
    // this guard exists to reject -- so the terminator must be explicit.
    expect(rule).toMatch(/opacity:\s*0\s*;/)
  })

  it('comes back on bubble hover', () => {
    expect(CSS).toMatch(/\.cc-bubble-wrap:hover\s+\.cc-bubble-x\s*\{[^}]*opacity:/)
  })

  it('keeps a :focus-visible rule for surfaces that can take focus', () => {
    /*
     * NOT a claim that the overlay is keyboard-reachable -- it is not. The pet overlay
     * window is created with `setFocusable(false)`, so nothing in it ever takes focus
     * and no CSS can change that. This pins the rule's existence for the day the
     * component renders somewhere focusable, and documents that the hover reveal did
     * not remove a keyboard path, because there was never one to remove.
     */
    const rule = CSS.match(/\.cc-bubble-x:focus-visible\s*\{[^}]*\}/)?.[0] ?? ''
    expect(rule, ':focus-visible rule not found').not.toBe('')
    expect(rule).toMatch(/opacity:\s*1\b/)
  })
})

describe('bubble: the countdown lives inside the card', () => {
  it('renders the countdown as a descendant of the card, not a sibling', () => {
    /*
     * Below the card it was invisible: its colours are mixed from `--card-fg`, the
     * bubble's dark ink, and below the card there is no card -- only the transparent
     * overlay over the user's desktop. Dark ink on a dark desktop reads as nothing.
     */
    const { container } = render(
      <Bubble text="Your glass could use a refill." kind="break" onDismiss={() => {}} />,
    )
    const card = container.querySelector('.cc-bubble')
    const bar = container.querySelector('.cc-bubble-countdown')
    expect(card, 'card not found').not.toBeNull()
    expect(bar, 'countdown not rendered for a break nudge').not.toBeNull()
    expect(card!.contains(bar!)).toBe(true)
  })

  it('is inset from the corners so the radius cannot clip it', () => {
    // Being clipped by the 14px radius is why it was moved out of the card originally.
    const rule = CSS.match(/\.cc-bubble-countdown\s*\{[^}]*\}/)?.[0] ?? ''
    expect(rule).toMatch(/position:\s*absolute/)
    expect(rule).toMatch(/left:\s*\d+px/)
    expect(rule).toMatch(/right:\s*\d+px/)
  })

  it('draws no countdown for a kind that never expires', () => {
    // A reminder waits for the user; a depleting bar would contradict that.
    const { container } = render(
      <Bubble text="stand up" kind="reminder" onDismiss={() => {}} />,
    )
    expect(container.querySelector('.cc-bubble-countdown')).toBeNull()
  })
})
