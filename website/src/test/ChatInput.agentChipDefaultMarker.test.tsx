/**
 * Composer agent chip — the INHERITED-DEFAULT marker (issue #8770).
 *
 * The chip names the agent a session runs under. An agent-less slot resolves
 * the CURRENT default at run time, so the accurate chip label is the resolved
 * alias marked `<alias> · default` (the one shared spelling from
 * `agentOrDefaultLabel`), which is what distinguishes it from a slot explicitly
 * pinned to that same alias. Before this fix both rendered the bare alias, so
 * the two states were indistinguishable on the chip — the same collapse the
 * sidebar rows carried before #8756.
 *
 * The chosen shape (acceptance item 4): the chip renders a separate display
 * label (`agentLabel`) while `agentName` stays the raw alias for everything
 * else keyed on it (the skills query, the switch title). These tests pin that
 * the chip shows the marker for an inherited default, the bare alias for a pin,
 * and that the raw alias still drives the chip's switch tooltip.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import i18next from 'i18next'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'
import { agentOrDefaultLabel } from '../utils/agentLabel'
import '../i18n/all'

vi.mock('../api/client', () => ({ api: {} }))

const props = (over: Record<string, unknown> = {}) => ({
  value: '',
  onChange: vi.fn(),
  onSend: vi.fn(),
  connected: true,
  onAgentClick: vi.fn(),
  // The control shelf that hosts the agent chip draws when the composer has a
  // project/model control; production always passes one. Without it the shelf
  // (and the chip) never renders, so give the chip a shelf to live in.
  onProjectClick: vi.fn(),
  ...over,
})

// The chip's visible label lives in a span, not in an accessible name (the
// `components.chatInput.agent` title key is interpolated), so read the span.
function agentChip(): HTMLButtonElement {
  const bot = document.querySelector('button > svg.lucide-bot') as SVGElement | null
  const btn = bot?.closest('button') as HTMLButtonElement | null
  if (!btn) throw new Error('no agent chip rendered')
  return btn
}

function agentChipText(): string {
  return agentChip().querySelector('span')?.textContent ?? ''
}

describe('ChatInput — agent chip inherited-default marker', () => {
  beforeEach(() => vi.clearAllMocks())
  afterEach(async () => { await i18next.changeLanguage('en') })

  it('marks an inherited default so it differs from a pin to the same alias', () => {
    // agent-less slot resolving to `kirocrew` -> the call site composes this.
    const inherited = agentOrDefaultLabel(undefined, 'kirocrew')
    const pinned = agentOrDefaultLabel('kirocrew', 'kirocrew')
    // The two states MUST render differently on the chip; that is the whole bug.
    expect(inherited).not.toEqual(pinned)

    renderWithProviders(
      <ChatInput {...props({ agentName: 'kirocrew', agentLabel: inherited })} />,
    )
    expect(agentChipText()).toBe(inherited)
    expect(agentChipText()).toContain('kirocrew')
    expect(agentChipText()).toContain('default')
  })

  it('shows the built-in default alias once when a remote-bound slot inherits it', () => {
    const inherited = agentOrDefaultLabel(undefined, 'default')
    renderWithProviders(
      <ChatInput
        {...props({
          agentName: 'default',
          agentLabel: inherited,
          agentIsInheritedDefault: true,
        })}
      />,
    )

    expect(agentChipText()).toBe('default')
    expect(agentChipText()).not.toContain('default · default')
    expect(agentChip()).toHaveAttribute('title', expect.stringContaining('follows the default'))
  })

  it('keeps the literal fallback while no default has loaded', async () => {
    await i18next.changeLanguage('zh-CN')
    expect(agentOrDefaultLabel(undefined, '')).toBe('default')
  })

  it('keeps the real alias when the localized default marker differs', async () => {
    await i18next.changeLanguage('zh-CN')
    const defaultLabel = i18next.t('components.agentSelector.default')
    expect(defaultLabel).not.toBe('default')
    expect(agentOrDefaultLabel(undefined, 'default')).toBe(`default · ${defaultLabel}`)
  })

  it('keeps a named alias marked when it matches the localized marker', async () => {
    await i18next.changeLanguage('de')
    const defaultLabel = i18next.t('components.agentSelector.default')
    expect(defaultLabel).not.toBe('default')
    expect(agentOrDefaultLabel(undefined, defaultLabel)).toBe(`${defaultLabel} · ${defaultLabel}`)
  })

  it('shows the bare alias when the slot is pinned to that agent', () => {
    renderWithProviders(
      <ChatInput
        {...props({
          agentName: 'kirocrew',
          agentLabel: agentOrDefaultLabel('kirocrew', 'kirocrew'),
        })}
      />,
    )
    expect(agentChipText()).toBe('kirocrew')
  })

  it('falls back to the raw alias when no display label is supplied', () => {
    // Callers that do not compose a label (nothing to distinguish) keep the
    // pre-fix behaviour: the chip renders `agentName` verbatim.
    renderWithProviders(<ChatInput {...props({ agentName: 'mochi' })} />)
    expect(agentChipText()).toBe('mochi')
  })

  it('keeps the raw alias in the switch tooltip, not the display label', () => {
    // Everything keyed on the agent (the skills query, the `agent` prop, the
    // switch title) must see the real alias, never the ` · default`-suffixed
    // label — otherwise the chip would try to switch to an agent named
    // "kirocrew · default".
    renderWithProviders(
      <ChatInput
        {...props({ agentName: 'kirocrew', agentLabel: agentOrDefaultLabel(undefined, 'kirocrew') })}
      />,
    )
    const title = agentChip().getAttribute('title') ?? ''
    expect(title).toContain('kirocrew')
    expect(title).not.toContain('· default')
  })

  it('explains the inherited default on hover and keyboard focus', () => {
    // The marker alone reads as opaque (#8770 UX). The inherited case carries an
    // explanatory tooltip reachable BOTH by hover (title) and by keyboard focus /
    // screen readers (aria-label) -- no glyph, no layout change.
    renderWithProviders(
      <ChatInput
        {...props({
          agentName: 'kirocrew',
          agentLabel: agentOrDefaultLabel(undefined, 'kirocrew'),
          agentIsInheritedDefault: true,
        })}
      />,
    )
    const btn = agentChip()
    const title = btn.getAttribute('title') ?? ''
    const aria = btn.getAttribute('aria-label') ?? ''
    // Explains WHAT the marker means, not just "Agent: kirocrew".
    expect(title).toContain('follows the default')
    // Reachable without a mouse: the same explanation is in the accessible name.
    expect(aria).toBe(title)
  })

  it('does not put the explanation on a pinned chip', () => {
    // A pinned chip has nothing to explain; a tooltip on both would dilute the
    // signal. It keeps the plain switch hint.
    renderWithProviders(
      <ChatInput
        {...props({
          agentName: 'kirocrew',
          agentLabel: agentOrDefaultLabel('kirocrew', 'kirocrew'),
          agentIsInheritedDefault: false,
        })}
      />,
    )
    const title = agentChip().getAttribute('title') ?? ''
    expect(title).not.toContain('follows the default')
  })
})
