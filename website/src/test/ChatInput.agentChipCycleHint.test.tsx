/**
 * Composer agent chip — the keyboard route to an agent switch is advertised on
 * the chip itself.
 *
 * The registry binds `cycle-agent` / `cycle-prev-agent`, but the only place a
 * user sees an agent switch happen is the chip, so the chip is where the
 * chords get taught: a second tooltip line naming both, plus
 * `aria-keyshortcuts` with the live next-agent chord. The line is a catalog
 * string, so it only appears while both chords are still the factory default;
 * it disappears when shortcuts are off and while a turn runs.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'
import { SHORTCUTS_ENABLED_EVENT, SHORTCUTS_ENABLED_KEY } from '../hooks/useKeyboardShortcuts'
import { SHORTCUT_OVERRIDES_EVENT, SHORTCUT_OVERRIDES_KEY } from '../lib/shortcutRegistry'
import '../i18n/all'

vi.mock('../api/client', () => ({ api: {} }))

const props = (over: Record<string, unknown> = {}) => ({
  value: '',
  onChange: vi.fn(),
  onSend: vi.fn(),
  connected: true,
  onAgentClick: vi.fn(),
  onProjectClick: vi.fn(),
  agentName: 'kirocrew',
  ...over,
})

function agentChip(): HTMLButtonElement {
  const bot = document.querySelector('button > svg.lucide-bot') as SVGElement | null
  const btn = bot?.closest('button') as HTMLButtonElement | null
  if (!btn) throw new Error('no agent chip rendered')
  return btn
}

describe('ChatInput — agent chip advertises the cycle-agent chords', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.removeItem(SHORTCUTS_ENABLED_KEY)
    localStorage.removeItem(SHORTCUT_OVERRIDES_KEY)
  })
  afterEach(() => {
    localStorage.removeItem(SHORTCUTS_ENABLED_KEY)
    localStorage.removeItem(SHORTCUT_OVERRIDES_KEY)
  })

  it('names both factory chords on a second tooltip line', () => {
    // jsdom reports a non-Mac platform, so the Alt+Shift spelling applies.
    renderWithProviders(<ChatInput {...props()} />)
    const title = agentChip().getAttribute('title') ?? ''
    const [first, second] = title.split('\n')
    expect(first).toBe('Agent: kirocrew')
    expect(second).toBe('Next agent: Alt+Shift+A · Previous: Alt+Shift+Z')
  })

  it('exposes the live next-agent chord to assistive tech', () => {
    renderWithProviders(<ChatInput {...props()} />)
    expect(agentChip()).toHaveAttribute('aria-keyshortcuts', 'Alt+Shift+A')
    // The accessible name stays the plain label; the chord has its own channel.
    expect(agentChip()).toHaveAttribute('aria-label', 'Agent: kirocrew')
  })

  it('drops the factory line but keeps aria-keyshortcuts live after a rebind', () => {
    localStorage.setItem(SHORTCUT_OVERRIDES_KEY, JSON.stringify({ 'cycle-agent': { key: 'j', alt: true, shift: true } }))
    renderWithProviders(<ChatInput {...props()} />)
    expect(agentChip().getAttribute('title')).toBe('Agent: kirocrew')
    expect(agentChip()).toHaveAttribute('aria-keyshortcuts', 'Alt+Shift+J')
  })

  it('advertises nothing while shortcuts are turned off', () => {
    renderWithProviders(<ChatInput {...props()} />)
    act(() => {
      localStorage.setItem(SHORTCUTS_ENABLED_KEY, '0')
      window.dispatchEvent(new Event(SHORTCUTS_ENABLED_EVENT))
    })
    expect(agentChip().getAttribute('title')).toBe('Agent: kirocrew')
    expect(agentChip()).not.toHaveAttribute('aria-keyshortcuts')
  })

  it('advertises nothing while a turn runs and the chip is disabled', () => {
    renderWithProviders(<ChatInput {...props({ isRunning: true })} />)
    expect(agentChip().getAttribute('title')).not.toContain('Alt+Shift+A')
    expect(agentChip()).not.toHaveAttribute('aria-keyshortcuts')
  })

  it('follows an override written while mounted', () => {
    renderWithProviders(<ChatInput {...props()} />)
    act(() => {
      localStorage.setItem(SHORTCUT_OVERRIDES_KEY, JSON.stringify({ 'cycle-agent': null }))
      window.dispatchEvent(new Event(SHORTCUT_OVERRIDES_EVENT))
    })
    // Unbound: nothing fires, so nothing is taught.
    expect(agentChip().getAttribute('title')).toBe('Agent: kirocrew')
    expect(agentChip()).not.toHaveAttribute('aria-keyshortcuts')
  })
})
