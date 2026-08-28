import React from 'react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'

// Pins the plan-quota section of the context popover, fed by the backend's
// `_meta["_claude/rateLimit"]` reading on the `context_usage` frame. Its whole
// job is answering "can I keep going", so the two things that must not regress
// are the severity a status renders at and the silence when a harness reports no
// quota at all — an empty "Plan limit" heading reads as a broken readout.

// The context chip lives on the shelf row, which only mounts when the host
// supplies at least one shelf control — onProjectClick is the cheapest.
const defaultProps = {
  value: '',
  onChange: vi.fn(),
  onSend: vi.fn(),
  onProjectClick: vi.fn(),
  contextPct: 44,
  contextUsedTokens: 88000,
  contextWindowTokens: 200000,
  modelName: 'auto',
}

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
})

function openPopover() {
  fireEvent.click(screen.getByLabelText('Context usage'))
}

describe('ChatInput context popover plan quota', () => {
  it('renders status, utilization, reset time and limit type', () => {
    // 2026-08-21T12:00:00Z + 3h, in unix SECONDS as the backend sends it.
    const now = Date.parse('2026-08-21T12:00:00Z')
    vi.spyOn(Date, 'now').mockReturnValue(now)
    renderWithProviders(
      <ChatInput
        {...defaultProps}
        rateLimit={{ status: 'allowed_warning', limit_type: 'five_hour', utilization: 81, resets_at: (now + 3 * 3600_000) / 1000 }}
      />,
    )
    openPopover()
    expect(screen.getByText('Plan limit')).toBeInTheDocument()
    expect(screen.getByText('Near limit')).toBeInTheDocument()
    expect(screen.getByText('81%')).toBeInTheDocument()
    expect(screen.getByText('Resets')).toBeInTheDocument()
    expect(screen.getByText('in 3h')).toBeInTheDocument()
    // The plan's own identifier, verbatim: translating it would break the match
    // against what the provider's docs call it.
    expect(screen.getByText('five_hour')).toBeInTheDocument()
  })

  it('colors the status by whether a turn can still be sent', () => {
    const { unmount } = renderWithProviders(
      <ChatInput {...defaultProps} rateLimit={{ status: 'rejected' }} />,
    )
    openPopover()
    expect((screen.getByText('Rate limited') as HTMLElement).style.color).toBe('var(--danger)')
    unmount()

    renderWithProviders(<ChatInput {...defaultProps} rateLimit={{ status: 'allowed_warning' }} />)
    openPopover()
    expect((screen.getByText('Near limit') as HTMLElement).style.color).toBe('var(--warn)')
  })

  it('leaves the normal state untinted', () => {
    // `allowed` is the boring case; spending the popover's one accent colour on
    // it would train the eye to ignore the tint that matters.
    renderWithProviders(<ChatInput {...defaultProps} rateLimit={{ status: 'allowed' }} />)
    openPopover()
    expect((screen.getByText('OK') as HTMLElement).style.color).toBe('')
  })

  it('shows nothing at all when the harness reports no quota', () => {
    renderWithProviders(<ChatInput {...defaultProps} />)
    openPopover()
    // The context rows are still there — only the quota section is absent.
    expect(screen.getByText('Context window')).toBeInTheDocument()
    expect(screen.queryByText('Plan limit')).not.toBeInTheDocument()
  })

  it('shows nothing when the block carries no renderable field', () => {
    // A frame the backend should never send (its parser drops an empty reading),
    // guarded because a lone heading over an empty row looks broken.
    renderWithProviders(<ChatInput {...defaultProps} rateLimit={{}} />)
    openPopover()
    expect(screen.queryByText('Plan limit')).not.toBeInTheDocument()
  })

  it('promotes the utilization into the heading row when there is no status', () => {
    // The honest degrade: a figure with no verdict, never shown twice.
    renderWithProviders(<ChatInput {...defaultProps} rateLimit={{ utilization: 37 }} />)
    openPopover()
    expect(screen.getByText('Plan limit')).toBeInTheDocument()
    expect(screen.getAllByText('37%')).toHaveLength(1)
    expect(screen.queryByText('Near limit')).not.toBeInTheDocument()
  })

  it('ignores a status spelling it has no severity for', () => {
    // The backend drops an unknown status, so this is the belt-and-braces half:
    // an unmapped state must not render at the no-verdict severity as if known.
    renderWithProviders(
      <ChatInput {...defaultProps} rateLimit={{ status: 'throttled_soon', utilization: 90 }} />,
    )
    openPopover()
    expect(screen.queryByText('throttled_soon')).not.toBeInTheDocument()
    expect(screen.getByText('90%')).toBeInTheDocument()
  })

  it('does not render the -1 utilization sentinel as a percentage', () => {
    // The backend omits an unreported utilization, but the sentinel's spelling is
    // internal and a stale client could see it; "-100%" is not a reading.
    renderWithProviders(
      <ChatInput {...defaultProps} rateLimit={{ status: 'allowed', utilization: -1 }} />,
    )
    openPopover()
    expect(screen.getByText('OK')).toBeInTheDocument()
    expect(screen.queryByText('-100%')).not.toBeInTheDocument()
  })
})
