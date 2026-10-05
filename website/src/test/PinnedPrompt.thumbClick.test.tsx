import { describe, it, expect, vi, beforeAll } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import PinnedPrompt from '../pages/chat/PinnedPrompt'

// A collapsed thumbnail sits inside the body button whose click jumps to the
// turn. Clicking the thumbnail must expand the card instead.

function renderCard(over: Partial<Parameters<typeof PinnedPrompt>[0]> = {}) {
  const onToggleExpanded = vi.fn()
  const onJump = vi.fn()
  render(
    <PinnedPrompt
      text="look at this screenshot"
      fullText="look at this screenshot"
      images={['/tmp/a.png']}
      bodyBeyondPreview={false}
      pushUp={0}
      bannerH={40}
      expanded={false}
      onToggleExpanded={onToggleExpanded}
      onJump={onJump}
      onCollapsedHeight={() => {}}
      {...over}
    />,
  )
  const card = screen.getByTestId('pinned-prompt')
  const thumb = card.querySelector('img') as HTMLImageElement
  const p = card.querySelector('p') as HTMLElement
  return { onToggleExpanded, onJump, thumb, p }
}

describe('PinnedPrompt collapsed thumbnail click', () => {
  beforeAll(() => {
    if (!('ResizeObserver' in globalThis)) {
      (globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = class {
        observe() {}
        unobserve() {}
        disconnect() {}
      }
    }
  })

  it('expands the card and does not jump', () => {
    const { onToggleExpanded, onJump, thumb } = renderCard()
    expect(thumb).not.toBeNull()
    fireEvent.click(thumb)
    expect(onToggleExpanded).toHaveBeenCalledTimes(1)
    expect(onJump).not.toHaveBeenCalled()
  })

  it('still jumps when the text is clicked', () => {
    const { onToggleExpanded, onJump, p } = renderCard()
    fireEvent.click(p)
    expect(onJump).toHaveBeenCalledTimes(1)
    expect(onToggleExpanded).not.toHaveBeenCalled()
  })

  it('does nothing while the card is folding', () => {
    const { onToggleExpanded, onJump, thumb } = renderCard({ liveH: 120 })
    expect(thumb).not.toBeNull()
    fireEvent.click(thumb)
    expect(onToggleExpanded).not.toHaveBeenCalled()
    expect(onJump).not.toHaveBeenCalled()
  })
})
