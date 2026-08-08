/**
 * Can a bubble actually be closed? Asked at the DOM level, on purpose.
 *
 * "I still cannot close it" has two completely different causes and reading source
 * cannot tell them apart: either React never dismisses, or the click never reaches
 * the bubble because the overlay is click-through everywhere except its reported
 * hitboxes. This file settles the first half deterministically — it dispatches real
 * DOM events, which bypass the OS-level hit test entirely. If these pass and the
 * bubble still will not close on screen, the fault is in the hitbox, not here.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup } from '@testing-library/react'

import { Bubble } from '../apps/crew-companion/Bubble'

beforeEach(() => {
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
  cleanup()
})

/** The exit animation runs before onDismiss fires, so tests must step past it. */
function flushExit() {
  vi.advanceTimersByTime(400)
}

describe('closing a bubble', () => {
  it('a fired reminder offers a ✕ — the case that shipped broken', () => {
    const { container } = render(<Bubble text="起床" kind="reminder" onDismiss={() => {}} />)
    // Present in the DOM; CSS reveals it on :hover. It used to be absent entirely,
    // because the ✕ was gated on "does it auto-dismiss" rather than on stickiness.
    // Queried by class, not accessible name: the label comes from i18nT, whose init
    // is async and would make this assert the translation rather than the button.
    expect(container.querySelector('.cc-bubble-x')).not.toBeNull()
  })

  it('clicking the ✕ dismisses it', () => {
    const onDismiss = vi.fn()
    const { container } = render(<Bubble text="起床" kind="reminder" onDismiss={onDismiss} />)
    fireEvent.click(container.querySelector('.cc-bubble-x')!)
    flushExit()
    expect(onDismiss).toHaveBeenCalledTimes(1)
  })

  it('clicking the body dismisses it too — the whole bubble is a target', () => {
    const onDismiss = vi.fn()
    render(<Bubble text="起床" kind="reminder" onDismiss={onDismiss} />)
    fireEvent.click(screen.getByRole('status'))
    flushExit()
    expect(onDismiss).toHaveBeenCalledTimes(1)
  })

  it('a short first line becomes a kicker above the body', () => {
    // "context\nmessage" is set as a small upper-case label over the body, which is
    // what makes a completion read as two things rather than one run-on blob.
    const { container } = render(
      <Bubble text={'MY PROJECT · build\nFix the parser'} kind="session-done" onDismiss={() => {}} />,
    )
    const kicker = container.querySelector('.cc-bubble-kicker')
    expect(kicker?.textContent).toBe('MY PROJECT · build')
    expect(container.querySelector('.cc-bubble-body')?.textContent).toBe('Fix the parser')
  })

  it('a long first line is left alone rather than mangled into a heading', () => {
    // The 40-character bound is what protects a genuine two-line sentence.
    const long = 'x'.repeat(41)
    const { container } = render(
      <Bubble text={`${long}\nsecond line`} kind="session-done" onDismiss={() => {}} />,
    )
    expect(container.querySelector('.cc-bubble-kicker')).toBeNull()
  })

  it('one-line text renders with no kicker at all', () => {
    const { container } = render(<Bubble text="起床" kind="reminder" onDismiss={() => {}} />)
    expect(container.querySelector('.cc-bubble-kicker')).toBeNull()
    expect(container.querySelector('.cc-bubble-text')?.textContent).toBe('起床')
  })

  it('the ✕ is a sibling of the text, not stacked on top of it', () => {
    // Absolutely positioned over the corner, it sat ON the words of a bubble as
    // narrow as a two-character reminder. As a flex sibling it reserves its own space.
    const { container } = render(<Bubble text="起床" kind="reminder" onDismiss={() => {}} />)
    const x = container.querySelector('.cc-bubble-x')!
    const textCol = container.querySelector('.cc-bubble-text')!
    expect(x.parentElement).toBe(textCol.parentElement)
    expect(getComputedStyle(x as Element).position).not.toBe('absolute')
  })

  it('the CTA sits below the box, outside the text row', () => {
    // Inside the row it would line up beside the words; the box also clips overflow.
    const { container } = render(
      <Bubble text="needs your OK" kind="approval" onDismiss={() => {}} onAction={() => {}} />,
    )
    const row = container.querySelector('.cc-bubble-cta-row')
    expect(row).not.toBeNull()
    expect(row!.parentElement?.className).toContain('cc-bubble-wrap')
  })

  it('unresolved work has NO ✕ and is cleared through its CTA instead', () => {
    const onDismiss = vi.fn()
    const onAction = vi.fn()
    const { container } = render(
      <Bubble text="needs your OK" kind="approval" onDismiss={onDismiss} onAction={onAction} />,
    )
    expect(container.querySelector('.cc-bubble-x')).toBeNull()
    // The CTA both acts and clears, and must not be swallowed by the body handler.
    const cta = screen.getAllByRole('button').find((b) => b.className.includes('cta'))
    expect(cta).toBeDefined()
    fireEvent.click(cta!)
    flushExit()
    expect(onAction).toHaveBeenCalledWith('open-session')
    expect(onDismiss).toHaveBeenCalledTimes(1)
  })

  it('a timed bubble still closes by hand before its timeout', () => {
    const onDismiss = vi.fn()
    render(<Bubble text="stretch" kind="break" onDismiss={onDismiss} />)
    fireEvent.click(screen.getByRole('status'))
    flushExit()
    expect(onDismiss).toHaveBeenCalledTimes(1)
  })
})

describe('keyboard dismissal (a11y)', () => {
  // The ✕ was hover-revealed (display:none at idle), which made a persistent
  // reminder undismissable for keyboard users — the wrapper's click handler is
  // pointer-only. The button must be rendered visible and focusable at idle.
  it('the ✕ is rendered visible at idle, not hover-revealed', () => {
    const { container } = render(<Bubble text="起床" kind="reminder" onDismiss={() => {}} />)
    const x = container.querySelector('.cc-bubble-x') as HTMLElement
    expect(x).not.toBeNull()
    expect(getComputedStyle(x).display).not.toBe('none')
  })

  it('the ✕ is a real focusable button that dismisses on activation', () => {
    const onDismiss = vi.fn()
    const { container } = render(<Bubble text="起床" kind="reminder" onDismiss={onDismiss} />)
    const x = container.querySelector('.cc-bubble-x') as HTMLButtonElement
    expect(x.tagName).toBe('BUTTON')
    expect(x.tabIndex).toBeGreaterThanOrEqual(0)
    x.focus()
    expect(document.activeElement).toBe(x)
    // A keyboard Enter on a native button fires click activation.
    fireEvent.click(x)
    flushExit()
    expect(onDismiss).toHaveBeenCalledTimes(1)
  })
})

describe('sticky bubbles ignore body clicks', () => {
  // A sticky bubble holds UNRESOLVED work (approval / needs-input / error) and
  // is cleared only through its CTA. The wrapper's click-to-dismiss ungated
  // let a stray body click silently swallow the very notification promising
  // "anything waiting on you always notifies".
  it('clicking an approval bubble body does NOT dismiss it', () => {
    const onDismiss = vi.fn()
    const { container } = render(
      <Bubble text="needs your OK" kind="approval" onDismiss={onDismiss} onAction={() => {}} />,
    )
    fireEvent.click(container.querySelector('.cc-bubble-wrap')!)
    flushExit()
    expect(onDismiss).not.toHaveBeenCalled()
  })

  it('clicking a reminder bubble body still dismisses it', () => {
    const onDismiss = vi.fn()
    const { container } = render(<Bubble text="起床" kind="reminder" onDismiss={onDismiss} />)
    fireEvent.click(container.querySelector('.cc-bubble-wrap')!)
    flushExit()
    expect(onDismiss).toHaveBeenCalledTimes(1)
  })
})
