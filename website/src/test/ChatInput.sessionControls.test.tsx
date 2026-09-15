/**
 * Tests for the app-contributed session-control chips in the composer.
 *
 * The chip's job is to be visible and honest before it is opened: its tint says
 * whether the app reports state, and its tooltip is the app's own words when it
 * has any. Neither is observable from the control's own module, so it is tested
 * here rather than in SessionControlHost.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'

vi.mock('@radix-ui/react-dropdown-menu', async () => await import('./__mocks__/@radix-ui/react-dropdown-menu'))
vi.mock('@radix-ui/react-popover', async () => await import('./__mocks__/@radix-ui/react-popover'))

import { screen, fireEvent } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'

vi.mock('../api/client', () => ({ api: {} }))

const chip = (over: Record<string, unknown> = {}) => ({
  key: 'test-app:scope',
  label: 'Scope',
  icon: 'Tag',
  ...over,
})

const props = (over: Record<string, unknown> = {}) => ({
  value: '',
  onChange: vi.fn(),
  onSend: vi.fn(),
  connected: true,
  ...over,
})

describe('ChatInput — session control chips', () => {
  beforeEach(() => vi.clearAllMocks())

  it('renders a chip per contributed control', () => {
    renderWithProviders(<ChatInput {...props({ sessionControls: [chip()] })} />)
    expect(screen.getByRole('button', { name: 'Scope' })).toBeInTheDocument()
  })

  it('draws the shelf for a chip alone, with no other pill present', () => {
    // Regression: the shelf was gated on the workspace/project/model pills, so a
    // contributed control was declared, mounted and invisible whenever none of
    // those happened to be there.
    renderWithProviders(
      <ChatInput {...props({ sessionControls: [chip()] })} />,
    )
    expect(screen.getByRole('button', { name: 'Scope' })).toBeVisible()
  })

  it('renders nothing when no app contributes one', () => {
    renderWithProviders(<ChatInput {...props()} />)
    expect(screen.queryByRole('button', { name: 'Scope' })).toBeNull()
  })

  it('tints the chip with --ok when the app reports ok', () => {
    // This is what makes a bound scope visible without opening the popover.
    renderWithProviders(
      <ChatInput {...props({ sessionControls: [chip({ state: 'ok' })] })} />,
    )
    expect(screen.getByRole('button', { name: 'Scope — Ready' }).className).toContain('text-ok')
  })

  it('tints with --warn when the app reports warn', () => {
    renderWithProviders(
      <ChatInput {...props({ sessionControls: [chip({ state: 'warn' })] })} />,
    )
    expect(screen.getByRole('button', { name: 'Scope — Needs attention' }).className).toContain(
      'text-warn',
    )
  })

  it('leaves the chip muted when the app reports no state', () => {
    // An unbound control must not look like a problem — "none" is a normal state.
    renderWithProviders(
      <ChatInput {...props({ sessionControls: [chip({ state: 'none' })] })} />,
    )
    const cls = screen.getByRole('button', { name: 'Scope' }).className
    expect(cls).toContain('text-muted')
    expect(cls).not.toContain('text-ok')
  })

  it('an open chip reads as active regardless of reported state', () => {
    // Open wins, so the chip you are pointing at is always the one that looks
    // selected — otherwise a green chip and an open chip are indistinguishable.
    renderWithProviders(
      <ChatInput {...props({ sessionControls: [chip({ state: 'ok', active: true })] })} />,
    )
    const cls = screen.getByRole('button', { name: 'Scope — Ready' }).className
    expect(cls).toContain('text-accent')
    expect(cls).not.toContain('text-ok')
  })

  it("appends the app's tooltip to the label rather than replacing it", () => {
    renderWithProviders(
      <ChatInput
        {...props({
          sessionControls: [
            chip({ state: 'ok', statusTooltip: 'Scope: scope/S-abc12345 (folder "Backend")' }),
          ],
        })}
      />,
    )
    // The label is what identifies the control, so it must survive whatever
    // the app reports — replacing it took the control's name away from screen
    // readers entirely.
    const btn = screen.getByRole('button', {
      name: 'Scope — Scope: scope/S-abc12345 (folder "Backend")',
    })
    expect(btn.getAttribute('title')).toBe(
      'Scope — Scope: scope/S-abc12345 (folder "Backend")',
    )
  })

  it('marks the chip so the host does not treat it as an outside click', () => {
    // Without the marker, mousedown-close races the chip's click-toggle and the
    // popover flickers instead of dismissing. Regression for AutoSDE f-fc907279.
    renderWithProviders(<ChatInput {...props({ sessionControls: [chip()] })} />)
    expect(
      screen.getByRole('button', { name: 'Scope' }).hasAttribute('data-session-control-chip'),
    ).toBe(true)
  })

  it('reports the chip key and its rect on click', () => {
    // The rect is how the host anchors the popover; without it nothing opens.
    const onSessionControlClick = vi.fn()
    renderWithProviders(
      <ChatInput {...props({ sessionControls: [chip()], onSessionControlClick })} />,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Scope' }))
    expect(onSessionControlClick).toHaveBeenCalledTimes(1)
    expect(onSessionControlClick.mock.calls[0][0]).toBe('test-app:scope')
    expect(onSessionControlClick.mock.calls[0][1]).toBeTruthy()
  })
})
