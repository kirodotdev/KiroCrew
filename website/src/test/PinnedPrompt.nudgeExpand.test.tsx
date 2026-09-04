import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import PinnedPrompt from '../pages/chat/PinnedPrompt'

// A pinned nudge shows a short "cycle N" label and carries no images, so neither
// the clamp gate nor the image gate fires. Without an explicit flag the expand
// affordance never mounts and the instruction body it was handed is unreachable.
function renderBanner(over: Partial<Parameters<typeof PinnedPrompt>[0]> = {}) {
  return render(
    <PinnedPrompt
      text="Auto-nudge · cycle 17"
      fullText="Babysit PR #4449. Check CI and review comments…"
      images={[]}
      pushUp={0}
      bannerH={40}
      expanded={false}
      onToggleExpanded={() => {}}
      onJump={() => {}}
      onCollapsedHeight={() => {}}
      {...over}
    />,
  )
}

describe('PinnedPrompt expand affordance', () => {
  it('mounts the chevron for a body the preview cannot show', () => {
    renderBanner({ bodyBeyondPreview: true })
    expect(screen.getByLabelText(/expand/i)).toBeTruthy()
  })

  it('omits the chevron when the preview already shows everything', () => {
    renderBanner({ bodyBeyondPreview: false })
    expect(screen.queryByLabelText(/expand/i)).toBeNull()
  })

  it('reveals the body once expanded', () => {
    renderBanner({ bodyBeyondPreview: true, expanded: true })
    expect(screen.getByText(/Babysit PR #4449/)).toBeTruthy()
  })

  it('calls onToggleExpanded when the chevron is pressed', () => {
    const onToggleExpanded = vi.fn()
    renderBanner({ bodyBeyondPreview: true, onToggleExpanded })
    screen.getByLabelText(/expand/i).click()
    expect(onToggleExpanded).toHaveBeenCalledTimes(1)
  })
})
