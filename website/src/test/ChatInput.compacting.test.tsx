import React from 'react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, render } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'
import StopEventCard from '../pages/chat/StopEventCard'
import { stubStripHeights } from './stripHeights'
import type { ChatMessage } from '../types'

/**
 * #14841: an automatic context compaction holds the session but is NOT a turn,
 * so before this the composer read idle while it ran, and the only thing a
 * user could do about an apparent stall was press Stop -- which cancelled the
 * compaction and restarted the session. The composer now shows the compaction
 * and withholds the Stop affordance while it runs; the stop card has a state
 * for a press the backend declined.
 */

const defaultProps = {
  value: '',
  onChange: vi.fn(),
  onSend: vi.fn(),
}

beforeEach(() => {
  vi.restoreAllMocks()
  stubStripHeights()
})

describe('ChatInput while the session compacts', () => {
  it('shows the compacting state as a spinner with a visible "Stop is unavailable" hint and no Send', () => {
    const onStop = vi.fn()
    renderWithProviders(<ChatInput {...defaultProps} compacting onStop={onStop} />)
    // No button: a stop-shaped control that does nothing invites a press.
    expect(screen.queryByTestId('stop-button-compacting')).toBeNull()
    expect(screen.getByTestId('compacting-indicator').querySelector('button')).toBeNull()
    expect(screen.getByTestId('compacting-spinner')).toHaveAttribute('aria-hidden', 'true')
    expect(screen.getByTestId('compacting-hint')).toHaveTextContent(
      'Compacting context — Stop is unavailable until it finishes',
    )
    fireEvent.click(screen.getByTestId('compacting-spinner'))
    expect(onStop).not.toHaveBeenCalled()
    expect(screen.queryByTestId('stop-button-armed')).toBeNull()
  })

  it('yields to a typed draft: the idle Send stays so the message queues behind the compaction', () => {
    renderWithProviders(<ChatInput {...defaultProps} value="carry on" compacting onStop={vi.fn()} />)
    expect(screen.queryByTestId('stop-button-compacting')).toBeNull()
    expect(screen.queryByTestId('compacting-hint')).toBeNull()
  })

  it('names the next press as the force stop after a declined Stop', () => {
    const onStop = vi.fn()
    renderWithProviders(<ChatInput {...defaultProps} isRunning compacting stopDeclined onStop={onStop} />)
    expect(screen.getByTestId('stop-declined-hint')).toHaveTextContent(
      'Click again to force stop (restarts the session; your messages stay, the agent keeps a recent excerpt)',
    )
    fireEvent.click(screen.getByTestId('stop-button-armed'))
    expect(onStop).toHaveBeenCalled()
  })

  it('yields to a live turn: the armed Stop stays while a turn shares the session', () => {
    const onStop = vi.fn()
    renderWithProviders(<ChatInput {...defaultProps} compacting isRunning onStop={onStop} />)
    expect(screen.queryByTestId('stop-button-compacting')).toBeNull()
    fireEvent.click(screen.getByTestId('stop-button-armed'))
    expect(onStop).toHaveBeenCalled()
  })

  it('yields to a stop the user already chose (soft_pending)', () => {
    const onStop = vi.fn()
    renderWithProviders(<ChatInput {...defaultProps} compacting isRunning onStop={onStop} stopState="soft_pending" />)
    expect(screen.queryByTestId('stop-button-compacting')).toBeNull()
    expect(screen.getByTestId('stop-button-pulsing')).toBeInTheDocument()
  })

  it('renders the ordinary armed Stop when nothing compacts', () => {
    renderWithProviders(<ChatInput {...defaultProps} isRunning onStop={vi.fn()} />)
    expect(screen.queryByTestId('stop-button-compacting')).toBeNull()
    expect(screen.getByTestId('stop-button-armed')).toBeInTheDocument()
  })
})

describe('StopEventCard stop_declined_compacting', () => {
  function row(state: string): ChatMessage {
    const data = { kind: 'stop_event', id: 'stop-14841', state, outcome: null }
    const cls = JSON.stringify(data)
    return { role: 'system', content: cls, cls, meta: data, ts: '2026-01-01T00:00:00Z' } as unknown as ChatMessage
  }

  it('is a status row that says the stop was declined, not a danger row', () => {
    render(<StopEventCard message={row('stop_declined_compacting')} />)
    const card = screen.getByTestId('stop-event-card')
    expect(card).toHaveAttribute('data-state', 'stop_declined_compacting')
    expect(card).toHaveAttribute('role', 'status')
    expect(card).toHaveTextContent(
      '[Compacting: a Stop was declined while the context compacted; nothing was stopped and the compaction finished on its own]',
    )
    expect(card.className).not.toContain('text-danger')
    // A settled history row: no spinner, or it claims live activity forever.
    expect(card.querySelector('.animate-spin')).toBeNull()
  })

  it('reads differently from the three existing states', () => {
    const texts = ['stopping', 'stopped', 'stop_failed_reset', 'stop_declined_compacting'].map((s) => {
      const { unmount } = render(<StopEventCard message={row(s)} />)
      const text = screen.getByTestId('stop-event-card').textContent
      unmount()
      return text
    })
    expect(new Set(texts).size).toBe(4)
  })
})
