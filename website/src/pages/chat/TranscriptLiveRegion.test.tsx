import { describe, expect, it } from 'vitest'
import { render } from '@testing-library/react'

import { TranscriptLiveRegion } from './TranscriptLiveRegion'

const region = (container: HTMLElement) => container.querySelector('[aria-live="polite"]') as HTMLElement

describe('TranscriptLiveRegion', () => {
  it('renders one offscreen polite atomic region', () => {
    const { container } = render(<TranscriptLiveRegion announcement="Kiro Crew: Done." />)
    const live = region(container)
    expect(live).toHaveClass('sr-only')
    expect(live).toHaveAttribute('aria-atomic', 'true')
    expect(live).toHaveTextContent('Kiro Crew: Done.')
    expect(container.querySelectorAll('[aria-live]')).toHaveLength(1)
  })

  it('keeps the same region mounted while its announcement changes', () => {
    const view = render(<TranscriptLiveRegion announcement="" />)
    const live = region(view.container)
    view.rerender(<TranscriptLiveRegion announcement="You: Sent." />)
    expect(region(view.container)).toBe(live)
    expect(live).toHaveTextContent('You: Sent.')
  })
})
