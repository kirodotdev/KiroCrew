import { describe, it, expect, beforeAll } from 'vitest'
import { render, screen } from '@testing-library/react'
import PinnedPrompt from '../pages/chat/PinnedPrompt'
import { scopeOverridesCss } from '../hooks/themeCss'

// #23789: the pinned card stands in for the user's bubble, so a theme pack that
// tints the bubble must reach the card too. A pack's overrides.css only keeps
// rules that name an allowlisted class (website/docs/theming-contract.md), and
// `message-bubble` is the bubble's one allowlisted hook. Run the issue's own pack
// rule through the real scoper and check the card's box matches what survives.

beforeAll(() => {
  if (!('ResizeObserver' in globalThis)) {
    (globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    }
  }
})

function cardBox(): HTMLElement {
  render(
    <PinnedPrompt
      text="a short prompt"
      fullText="a short prompt"
      images={[]}
      bodyBeyondPreview={false}
      pushUp={0}
      bannerH={40}
      expanded={false}
      onToggleExpanded={() => {}}
      onJump={() => {}}
      onCollapsedHeight={() => {}}
    />,
  )
  return screen.getByTestId('pinned-prompt').firstElementChild as HTMLElement
}

describe('PinnedPrompt theme hook (#23789)', () => {
  it('a theme pack rule that tints the user bubble also matches the pinned card', () => {
    const scoped = scopeOverridesCss('.message-bubble.user-bubble { background: var(--accent); color: var(--accent-fg) }')
    expect(scoped.kept).toBe(1)
    const selector = scoped.css.slice(0, scoped.css.indexOf('{')).trim()
    expect(selector).toContain('.message-bubble.user-bubble')
    expect(cardBox().matches(selector)).toBe(true)
  })

  it('carries both bubble hooks, the same pair the plain user bubble carries', () => {
    const box = cardBox()
    expect(box.classList.contains('message-bubble')).toBe(true)
    expect(box.classList.contains('user-bubble')).toBe(true)
  })
})
