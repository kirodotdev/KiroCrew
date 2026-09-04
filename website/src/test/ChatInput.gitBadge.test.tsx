import React from 'react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'

// Pins the footer git-status badge contract: beside the branch label, a
// PASSIVE readout summarizes the working tree (dirty file count, commits
// ahead/behind upstream). It renders only when there is signal — a clean
// in-sync tree keeps the footer exactly as it was — and it is deliberately
// NOT a button: the shelf row already carries three actions on base and
// max-two-buttons-per-row forbids growing a 3+ row.

const defaultProps = {
  value: '',
  onChange: vi.fn(),
  onSend: vi.fn(),
  onProjectClick: vi.fn(),
  project: '/home/u/work/KiroCrew',
  projectBranch: 'main',
}

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
})

const badge = () => screen.getByRole('status', { name: /uncommitted|\u2191|\u2193/ })

describe('ChatInput footer git-status badge', () => {
  it('shows dirty count plus ahead/behind as a passive readout, not a button', () => {
    renderWithProviders(
      <ChatInput {...defaultProps} projectGitDirty={3} projectGitAhead={1} projectGitBehind={2} />,
    )
    const el = badge()
    expect(el).toHaveTextContent('3')
    expect(el).toHaveTextContent('\u21911')
    expect(el).toHaveTextContent('\u21932')
    expect(el.getAttribute('title')).toContain('3 uncommitted')
    // The row must not gain a fourth action control (max-two-buttons-per-row):
    // the readout is a span, so no button carries the badge's label.
    expect(el.tagName).toBe('SPAN')
    expect(screen.queryByRole('button', { name: /uncommitted/ })).toBeNull()
  })

  it('renders arrows alone when the tree is clean but out of sync', () => {
    renderWithProviders(<ChatInput {...defaultProps} projectGitDirty={0} projectGitBehind={4} />)
    const el = badge()
    expect(el).toHaveTextContent('\u21934')
    expect(el.getAttribute('title')).not.toContain('uncommitted')
  })

  it('renders the dirty count alone when in sync', () => {
    renderWithProviders(<ChatInput {...defaultProps} projectGitDirty={7} />)
    expect(badge().getAttribute('title')).toBe('7 uncommitted')
  })

  it('keeps the badge at narrow (compact) shelf widths, icon-only chips notwithstanding', async () => {
    // shelfCompact flips when the measured shelf width drops below 340px.
    // Report a 320px shelf through the ResizeObserver, then assert the badge
    // survives while text labels collapse (narrow-viewport-required: the
    // badge is the only tree-state signal).
    const RealRO = globalThis.ResizeObserver
    class NarrowRO {
      cb: ResizeObserverCallback
      constructor(cb: ResizeObserverCallback) {
        this.cb = cb
      }
      observe(_el: Element) {
        this.cb(
          [{ contentRect: { width: 320 } } as unknown as ResizeObserverEntry],
          this as unknown as ResizeObserver,
        )
      }
      unobserve() {}
      disconnect() {}
    }
    globalThis.ResizeObserver = NarrowRO as unknown as typeof ResizeObserver
    try {
      const { act } = await import('@testing-library/react')
      await act(async () => {
        renderWithProviders(
          <ChatInput {...defaultProps} agentName="kirocrew" onAgentClick={vi.fn()} projectGitDirty={3} />,
        )
      })
      // Compact confirmed: the agent chip dropped its text label.
      expect(screen.queryByText('kirocrew')).toBeNull()
      // The badge is still present.
      expect(badge()).toHaveTextContent('3')
    } finally {
      globalThis.ResizeObserver = RealRO
    }
  })

  it('renders nothing when the tree is clean and in sync', () => {
    renderWithProviders(
      <ChatInput {...defaultProps} projectGitDirty={0} projectGitAhead={0} projectGitBehind={0} />,
    )
    expect(screen.queryByRole('status', { name: /uncommitted/ })).toBeNull()
    // The branch label itself is untouched.
    expect(screen.getByRole('button', { name: /Copy branch name/ })).toBeTruthy()
  })

  it('renders while a response is running (read-only signal, no action to gate)', () => {
    renderWithProviders(
      <ChatInput {...defaultProps} isRunning onStop={vi.fn()} projectGitDirty={1} />,
    )
    expect(badge()).toHaveTextContent('1')
  })
})
