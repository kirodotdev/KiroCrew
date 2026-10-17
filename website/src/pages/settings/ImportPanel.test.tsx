import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, onTestFinished, vi } from 'vitest'
import { ImportPanel } from './ImportPanel'

// PortabilityTab owns its own API wiring; stub it — this test isolates the
// panel composition (agent import card + configuration backup section).
vi.mock('../overview/PortabilityTab', () => ({
  default: () => <div data-testid="portability-tab" />,
}))

describe('ImportPanel', () => {
  it('reopens the foreign-agent import flow', () => {
    const listener = vi.fn()
    window.addEventListener('mc-start-import', listener)
    render(<ImportPanel />)

    fireEvent.click(screen.getByRole('button', { name: /import from another agent/i }))

    expect(listener).toHaveBeenCalledOnce()
    window.removeEventListener('mc-start-import', listener)
  })

  // The first-run host tells this replay of the importer alone from `/onboarding`,
  // which replays all of first run, by `continueOnboarding`. An empty scan keeps
  // its result on screen only for the importer alone, so the button must not send it.
  it('replays the importer alone, without continueOnboarding', () => {
    const listener = vi.fn()
    window.addEventListener('mc-start-import', listener)
    onTestFinished(() => window.removeEventListener('mc-start-import', listener))
    render(<ImportPanel />)

    fireEvent.click(screen.getByRole('button', { name: /import from another agent/i }))

    const event = listener.mock.calls[0][0] as CustomEvent<{ continueOnboarding?: boolean } | null>
    expect(event.detail?.continueOnboarding).toBeFalsy()
  })

  it('hosts the configuration backup section (moved from Overview > Import/Export)', () => {
    render(<ImportPanel />)
    expect(screen.getByText('Back up & restore configuration')).toBeInTheDocument()
    expect(screen.getByTestId('portability-tab')).toBeInTheDocument()
  })
})
