/**
 * AppStatusIndicator (issue #520) — a colored sidebar dot. Collapsed rail = a
 * corner dot on the icon; expanded rail = a dot at the row's right edge. The
 * label is never drawn inline — it is the dot's tooltip/accessible name only.
 * Neutral/idle and unknown tones render nothing; "busy" pulses.
 */
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { AppStatusIndicator } from '../components/AppStatusIndicator'

describe('AppStatusIndicator', () => {
  it('renders nothing when there is no status', () => {
    const { container } = render(<AppStatusIndicator status={null} />)
    expect(container.firstChild).toBeNull()
  })

  it('renders nothing for the neutral (idle) tone', () => {
    const { container } = render(<AppStatusIndicator status={{ tone: 'neutral', label: '' }} />)
    expect(container.firstChild).toBeNull()
  })

  it('renders nothing for an unknown tone (coerced to neutral)', () => {
    const { container } = render(<AppStatusIndicator status={{ tone: 'bogus', label: 'x' }} />)
    expect(container.firstChild).toBeNull()
  })

  it('exposes the label only as tooltip/aria, never as inline text', () => {
    render(<AppStatusIndicator status={{ tone: 'positive', label: 'Valid 11h' }} collapsed={false} />)
    const dot = screen.getByLabelText('Valid 11h')
    expect(dot.getAttribute('title')).toBe('Valid 11h')
    // No visible label text in the rail.
    expect(screen.queryByText('Valid 11h')).toBeNull()
  })

  it('expanded rail: dot placed at the row edge', () => {
    render(<AppStatusIndicator status={{ tone: 'caution', label: 'Expiring 12m' }} collapsed={false} />)
    const dot = screen.getByLabelText('Expiring 12m')
    expect(dot.classList.contains('app-status-dot--edge')).toBe(true)
    expect(dot.classList.contains('app-status-dot--caution')).toBe(true)
  })

  it('collapsed rail: corner dot on the icon', () => {
    render(<AppStatusIndicator status={{ tone: 'positive', label: 'Valid 11h' }} collapsed={true} />)
    const dot = screen.getByLabelText('Valid 11h')
    expect(dot.classList.contains('app-status-dot--corner')).toBe(true)
  })

  it('falls back to the tone name as the label when none is given', () => {
    render(<AppStatusIndicator status={{ tone: 'critical', label: '' }} />)
    expect(screen.getByLabelText('critical').classList.contains('app-status-dot--critical')).toBe(true)
  })

  it('adds the pulse treatment for the busy tone', () => {
    render(<AppStatusIndicator status={{ tone: 'busy', label: 'running' }} />)
    expect(screen.getByLabelText('running').classList.contains('app-status-dot--pulse')).toBe(true)
  })
})
