import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import PinnedPrompt from '../pages/chat/PinnedPrompt'

// A short prompt whose preview already fits (a one-line paste token, say) shows a
// short label and carries no images, so neither the clamp gate nor the image gate
// fires. Without an explicit flag the expand affordance never mounts and the
// fuller body it was handed is unreachable. (Nudge rows are no longer pinned; the
// flag is generic to any prompt whose body exceeds its flattened preview.)
function renderBanner(over: Partial<Parameters<typeof PinnedPrompt>[0]> = {}) {
  return render(
    <PinnedPrompt
      text="first second third"
      fullText={'first\nsecond\nthird — and the line the preview flattened away'}
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
    expect(screen.getByText(/the line the preview flattened away/)).toBeTruthy()
  })

  it('calls onToggleExpanded when the chevron is pressed', () => {
    const onToggleExpanded = vi.fn()
    renderBanner({ bodyBeyondPreview: true, onToggleExpanded })
    screen.getByLabelText(/expand/i).click()
    expect(onToggleExpanded).toHaveBeenCalledTimes(1)
  })
})
