import { describe, it, expect } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import StaleConfigBadge from './StaleConfigBadge'
import { ChatHeaderMenu } from '../pages/chat/ChatPageMessageContent'
import { renderWithProviders, createTestStore } from '../test/helpers'
import { sseSlots } from '../store/dashboardSlice'
import type { ChatSlot } from '../types'

describe('StaleConfigBadge', () => {
  it('renders nothing for a chat on its current config', () => {
    const { container } = render(<StaleConfigBadge slot={{ config_stale: false, config_stale_inputs: '' }} />)
    expect(container).toBeEmptyDOMElement()
    render(<StaleConfigBadge slot={undefined} />)
    expect(screen.queryByTestId('stale-config-badge')).toBeNull()
  })

  it('names what changed and the remedy in its tooltip', () => {
    render(<StaleConfigBadge slot={{ config_stale: true, config_stale_inputs: '~/.kiro/agents/kirocrew.json' }} />)
    const badge = screen.getByTestId('stale-config-badge')
    expect(badge).toHaveTextContent('stale config')
    expect(badge).toHaveAttribute(
      'title',
      'Stale config: ~/.kiro/agents/kirocrew.json changed since this session started. Click to open the session menu, then choose Reload session.',
    )
  })

  it('drops the label below the sm breakpoint and keeps the icon and accessible name', () => {
    render(<StaleConfigBadge slot={{ config_stale: true, config_stale_inputs: '~/.kiro/agents/kirocrew.json' }} />)
    const badge = screen.getByTestId('stale-config-badge')
    const label = screen.getByText('stale config')
    expect(label).toHaveClass('hidden', 'sm:inline')
    expect(badge.querySelector('svg')).not.toBeNull()
    expect(badge.getAttribute('aria-label')).toMatch(/^Stale config: /)
  })

  it('falls back to a generic tooltip when no input is named', () => {
    render(<StaleConfigBadge slot={{ config_stale: true }} />)
    expect(screen.getByTestId('stale-config-badge')).toHaveAttribute(
      'title',
      "Stale config: this session's config changed since it started. Click to open the session menu, then choose Reload session.",
    )
  })

  it('promises no menu on the compact sidebar mark, whose click opens the chat', () => {
    const { unmount } = render(
      <StaleConfigBadge slot={{ config_stale: true, config_stale_inputs: '.kiro/settings/mcp.json' }} compact />,
    )
    expect(screen.getByTestId('stale-config-badge')).toHaveAttribute(
      'title',
      'Stale config: .kiro/settings/mcp.json changed since this session started. Reload the session to apply.',
    )
    unmount()
    render(<StaleConfigBadge slot={{ config_stale: true }} compact />)
    const title = screen.getByTestId('stale-config-badge').getAttribute('title')
    expect(title).toBe("Stale config: this session's config changed since it started. Reload the session to apply.")
    expect(title).not.toMatch(/Click/)
  })

  it('rides the session-menu trigger in the chat header, so a click opens the menu with Reload', () => {
    const store = createTestStore()
    store.dispatch(sseSlots([{ key: 'zzq-slot', messages: 0, running: false, config_stale: true, config_stale_inputs: '.kiro/settings/mcp.json' } as ChatSlot]))
    renderWithProviders(<ChatHeaderMenu activeSlot="zzq-slot" agent="kirocrew" mode="" />, { store })
    const badge = screen.getByTestId('stale-config-badge')
    fireEvent.pointerDown(badge, { button: 0, ctrlKey: false })
    expect(screen.getByRole('menuitem', { name: /Reload session/ })).toBeInTheDocument()
  })

  it('is absent from the header once the config is current again', () => {
    const store = createTestStore()
    store.dispatch(sseSlots([{ key: 'zzq-slot', messages: 0, running: false, config_stale: false } as ChatSlot]))
    renderWithProviders(<ChatHeaderMenu activeSlot="zzq-slot" agent="kirocrew" mode="" />, { store })
    expect(screen.queryByTestId('stale-config-badge')).toBeNull()
  })
})
