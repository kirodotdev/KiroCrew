/**
 * The pinned banner's minimize control and its minimized form.
 *
 * The banner replaces a transcript row that ChatPage HIDES while it is pinned, so
 * the two states this covers are not cosmetic variants: the card is the only
 * visible copy of that message, and the chip is the only way back to it.
 */

import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import PinnedPrompt from '../pages/chat/PinnedPrompt'
import PinnedPromptPill from '../pages/chat/PinnedPromptPill'
import { pinHidesRow } from '../utils/pinnedPrompt'

function renderBanner(over: Partial<Parameters<typeof PinnedPrompt>[0]> = {}) {
  return render(
    <PinnedPrompt
      text="Clean up the leftover worktrees"
      fullText="Clean up the leftover worktrees"
      images={[]}
      pushUp={0}
      bannerH={40}
      expanded={false}
      onToggleExpanded={() => {}}
      onJump={() => {}}
      onMinimize={() => {}}
      cardRef={null}
      onCollapsedHeight={() => {}}
      {...over}
    />,
  )
}

describe('PinnedPrompt minimize control', () => {
  it('renders the minimize control', () => {
    renderBanner()
    expect(screen.getByLabelText(/minimize pinned turn/i)).toBeTruthy()
  })

  // The row cap is why this sits in the band instead of the card's control row: the
  // jump region and the chevron already fill that row.
  it('renders minimize outside the card, not in its control row', () => {
    renderBanner({ bodyBeyondPreview: true })
    const card = screen.getByTestId('pinned-prompt')
    const minimize = screen.getByTestId('pinned-prompt-minimize')
    expect(card.contains(minimize)).toBe(false)
    expect(card.querySelectorAll('button').length).toBe(2)
  })

  it('calls onMinimize when pressed', () => {
    const onMinimize = vi.fn()
    renderBanner({ onMinimize })
    screen.getByLabelText(/minimize pinned turn/i).click()
    expect(onMinimize).toHaveBeenCalledTimes(1)
  })

  // A one-line prompt is the commonest shape and earns no chevron, so the card is
  // pure overhead there and still needs a way out.
  it('offers minimize on a short prompt that earns no chevron', () => {
    renderBanner()
    expect(screen.queryByLabelText(/expand/i)).toBeNull()
    expect(screen.getByLabelText(/minimize pinned turn/i)).toBeTruthy()
  })

  it('still offers minimize alongside the chevron when the body is clamped', () => {
    renderBanner({ bodyBeyondPreview: true })
    expect(screen.getByLabelText(/expand/i)).toBeTruthy()
    expect(screen.getByLabelText(/minimize pinned turn/i)).toBeTruthy()
  })
})

describe('PinnedPromptPill', () => {
  it('is a labelled control, not a hover-only target', () => {
    render(<PinnedPromptPill onRestore={() => {}} />)
    expect(screen.getByLabelText(/restore pinned turn/i)).toBeTruthy()
  })

  // Minimized persists across sessions, so this chip is the feature's only trace
  // for a returning user — and `title` never fires on touch.
  it('carries a VISIBLE label, not just a tooltip', () => {
    render(<PinnedPromptPill onRestore={() => {}} />)
    const btn = screen.getByLabelText(/restore pinned turn/i)
    expect(btn.textContent).toMatch(/pinned turn/i)
  })

  it('calls onRestore when pressed', () => {
    const onRestore = vi.fn()
    render(<PinnedPromptPill onRestore={onRestore} />)
    screen.getByLabelText(/restore pinned turn/i).click()
    expect(onRestore).toHaveBeenCalledTimes(1)
  })
})

describe('pinHidesRow', () => {
  const pin = { ts: '2026-08-29T12:00:00Z', idx: 4 }

  it('hides the row the banner is standing in for', () => {
    expect(pinHidesRow(pin, false, { ts: pin.ts, idx: 4 })).toBe(true)
  })

  // The regression this exists to prevent: no card covers the row, so hiding it
  // would leave the message invisible in both places.
  it('never hides a row while minimized', () => {
    expect(pinHidesRow(pin, true, { ts: pin.ts, idx: 4 })).toBe(false)
  })

  it('hides nothing when no prompt is pinned', () => {
    expect(pinHidesRow(null, false, { ts: pin.ts, idx: 4 })).toBe(false)
  })

  it('leaves other rows visible', () => {
    expect(pinHidesRow(pin, false, { ts: 'other', idx: 9 })).toBe(false)
  })

  // The index is computed in a scroll rAF, so a streaming append can shift the list
  // before the row renders; matching on index alone hid the wrong row.
  it('prefers ts over a shifted index', () => {
    expect(pinHidesRow(pin, false, { ts: pin.ts, idx: 5 })).toBe(true)
    expect(pinHidesRow(pin, false, { ts: 'other', idx: 4 })).toBe(false)
  })

  it('falls back to the index for a message with no ts', () => {
    expect(pinHidesRow({ idx: 4 }, false, { idx: 4 })).toBe(true)
    expect(pinHidesRow({ idx: 4 }, false, { idx: 5 })).toBe(false)
  })

  // A grouped row carries no single message ts, so it must never satisfy a
  // ts-keyed pin.
  it('never matches a grouped row against a ts-keyed pin', () => {
    expect(pinHidesRow(pin, false, { ts: undefined, idx: 4 })).toBe(false)
  })
})
