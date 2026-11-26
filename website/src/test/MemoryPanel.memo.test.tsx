import { describe, it, expect, vi } from 'vitest'
import { useState } from 'react'
import { act, fireEvent, screen } from '@testing-library/react'
import { renderWithProviders } from './helpers'

/** A Settings page render (the Kiro usage poll answering) leaves the memory page alone. */
const { renders } = vi.hoisted(() => ({ renders: { count: 0 } }))
vi.mock('../pages/overview/MemoryTab', () => ({ default: () => { renders.count++; return <div data-testid="memory-tab" /> } }))
const { MemoryPanel } = await import('../pages/settings/MemoryPanel')

function Page() {
  const [tick, setTick] = useState(0)
  return <><button onClick={() => setTick(tick + 1)}>poll {tick}</button><MemoryPanel /></>
}

describe('MemoryPanel', () => {
  it('does not re-render the memory page when the Settings page re-renders', async () => {
    renderWithProviders(<Page />, { route: '/settings/memory' })
    await screen.findByTestId('memory-tab')
    const before = renders.count
    act(() => { fireEvent.click(screen.getByRole('button', { name: /poll/ })) })
    act(() => { fireEvent.click(screen.getByRole('button', { name: /poll/ })) })
    expect(screen.getByRole('button', { name: 'poll 2' })).toBeInTheDocument()
    expect(renders.count).toBe(before)
  })
})
