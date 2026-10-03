import { describe, it, expect, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import PinnedPrompt from '../pages/chat/PinnedPrompt'

// A prompt whose content is an image pins as thumbnails rather than a blank card
// (`promptPreview` strips the image markdown from `text`). A thumbnail whose file
// is gone 404s; the card drops that one src and, once every image has failed and
// there is no text either, stands a neutral glyph in for them so the card never
// goes blank. Both the collapsed and the expanded card carry that fallback.
function renderBanner(over: Partial<Parameters<typeof PinnedPrompt>[0]> = {}) {
  return render(
    <PinnedPrompt
      text=""
      fullText=""
      images={['/tmp/a.png', '/tmp/b.png']}
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

const thumbs = (root: HTMLElement) => Array.from(root.querySelectorAll('img'))
const glyph = (root: HTMLElement) => root.querySelector('svg.lucide-image-off')

describe('PinnedPrompt image thumbnails', () => {
  it('renders one thumbnail per image on the collapsed card', () => {
    const { container } = renderBanner()
    expect(thumbs(container)).toHaveLength(2)
    expect(glyph(container)).toBeNull()
  })

  it('drops only the thumbnail whose load failed', () => {
    const { container } = renderBanner()
    fireEvent.error(thumbs(container)[0])
    expect(thumbs(container)).toHaveLength(1)
    expect(glyph(container)).toBeNull()
  })

  it('stands a glyph in once every image has failed and there is no text', () => {
    const { container } = renderBanner()
    for (const img of thumbs(container)) fireEvent.error(img)
    expect(thumbs(container)).toHaveLength(0)
    expect(glyph(container)).not.toBeNull()
  })

  it('keeps the glyph fallback on the expanded card too', () => {
    const { container } = renderBanner({ expanded: true })
    expect(thumbs(container)).toHaveLength(2)
    for (const img of thumbs(container)) fireEvent.error(img)
    expect(thumbs(container)).toHaveLength(0)
    expect(glyph(container)).not.toBeNull()
  })

  it('jumps back to the prompt when the body is clicked', () => {
    const onJump = vi.fn()
    renderBanner({ text: 'a short prompt', fullText: 'a short prompt', images: [], onJump })
    fireEvent.click(screen.getByText('a short prompt'))
    expect(onJump).toHaveBeenCalledTimes(1)
  })
})
