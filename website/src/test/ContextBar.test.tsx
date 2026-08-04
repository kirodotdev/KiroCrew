/**
 * ContextBar tooltip is percentage-only (absolute token counts live in the
 * click popover, not the hover tooltip). The fill color shifts to warn (>=75%)
 * / danger (>=90%).
 */
import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import ContextBar from '../components/ContextBar'

describe('ContextBar', () => {
  it('shows the rounded percentage in the tooltip', () => {
    const { container } = render(<ContextBar pct={44} />)
    expect(container.querySelector('span')?.getAttribute('title')).toBe('Context: 44%')
  })

  it('clamps the percentage at 100', () => {
    const { container } = render(<ContextBar pct={140} />)
    expect(container.querySelector('span')?.getAttribute('title')).toBe('Context: 100%')
  })

  it('uses accent fill below 75%', () => {
    const { container } = render(<ContextBar pct={50} />)
    const rects = container.querySelectorAll('rect')
    expect(rects[1].getAttribute('fill')).toBe('var(--accent)')
  })

  it('uses warn fill at 75–89%', () => {
    const { container } = render(<ContextBar pct={80} />)
    const rects = container.querySelectorAll('rect')
    expect(rects[1].getAttribute('fill')).toBe('var(--warn)')
  })

  it('uses danger fill at 90%+', () => {
    const { container } = render(<ContextBar pct={95} />)
    const rects = container.querySelectorAll('rect')
    expect(rects[1].getAttribute('fill')).toBe('var(--danger)')
  })
})
