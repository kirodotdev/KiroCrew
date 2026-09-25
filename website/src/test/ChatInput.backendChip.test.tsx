/**
 * Composer backend chip — names the AI backend serving this chat.
 *
 * Per-chat backend selection happens ONCE, on the welcome screen; after that
 * the binding is immutable. Without a chip an open chat gives no way to know
 * which backend is answering — indistinguishable from the global default —
 * which matters exactly when descriptor-defined backends exist. These tests
 * pin that the chip renders the bound backend's display name, that it answers
 * a click by explaining itself inline (it sits between two chips that open
 * pickers, so a silent no-op would read as broken) while never opening a
 * picker, that the tooltip carries the pinned/default distinction, and that
 * an absent label hides the chip entirely.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { fireEvent } from '@testing-library/react'
import i18next from 'i18next'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'
import '../i18n/all'

vi.mock('../api/client', () => ({ api: {} }))

const props = (over: Record<string, unknown> = {}) => ({
  value: '',
  onChange: vi.fn(),
  onSend: vi.fn(),
  connected: true,
  // The control shelf that hosts the chip draws when the composer has a
  // project control; production always passes one. Without it the shelf
  // (and the chip) never renders, so give the chip a shelf to live in.
  onProjectClick: vi.fn(),
  ...over,
})

function backendChip(): HTMLElement | null {
  return document.querySelector('[data-testid="chat-input-backend-chip"]')
}

describe('ChatInput — backend chip', () => {
  afterEach(async () => { await i18next.changeLanguage('en') })

  it('renders the bound backend name and explains itself on click instead of opening a picker', () => {
    renderWithProviders(
      <ChatInput {...props({ backendLabel: 'Acme Agent' })} />,
    )
    const chip = backendChip()
    expect(chip).not.toBeNull()
    // The visible text carries the qualifier: beside the agent chip, a bare
    // name is indistinguishable from an agent.
    expect(chip!.textContent).toContain('Backend: Acme Agent')
    // Nothing is explained until asked; the caption is the click's only effect.
    expect(document.querySelector('[data-testid="chat-input-backend-chip-explained"]')).toBeNull()
    expect(chip!.getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(chip!)
    const caption = document.querySelector('[data-testid="chat-input-backend-chip-explained"]')
    expect(caption).not.toBeNull()
    expect(caption!.getAttribute('role')).toBe('status')
    expect(caption!.textContent).toMatch(/chosen when this chat was created and can't be changed/)
    expect(chip!.getAttribute('aria-expanded')).toBe('true')
    // No listbox/menu opened: the binding is immutable.
    expect(document.querySelector('[role="listbox"]')).toBeNull()
    fireEvent.click(chip!)
    expect(document.querySelector('[data-testid="chat-input-backend-chip-explained"]')).toBeNull()
  })

  it('dismisses the explanation on Escape and on a click outside the chip region', () => {
    renderWithProviders(
      <ChatInput {...props({ backendLabel: 'Acme Agent' })} />,
    )
    const chip = backendChip()!
    const caption = () => document.querySelector('[data-testid="chat-input-backend-chip-explained"]')
    fireEvent.click(chip)
    expect(caption()).not.toBeNull()
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(caption()).toBeNull()
    fireEvent.click(chip)
    expect(caption()).not.toBeNull()
    // A click inside the bubble itself keeps it; a click anywhere else closes it.
    fireEvent.mouseDown(caption()!)
    expect(caption()).not.toBeNull()
    fireEvent.mouseDown(document.body)
    expect(caption()).toBeNull()
    expect(chip.getAttribute('aria-expanded')).toBe('false')
  })

  it('lives in its own separated region with a viewport-clamped bubble, so it fits a 320px viewport', () => {
    // Two rules meet here. `max-two-buttons-per-row`: the chip is an action
    // control, and the agent/project group holds legacy status at its cap, so the
    // chip must not be a sibling in that group -- it gets its own region with a
    // leading divider (the rule's stated exemption). `narrow-viewport-required`:
    // the explanation bubble anchors to that region and its width is capped at
    // 320px AND the viewport minus the composer inset, never a bare pixel width.
    renderWithProviders(
      <ChatInput {...props({ backendLabel: 'Acme Agent' })} />,
    )
    const chip = backendChip()!
    const region = chip.parentElement as HTMLElement
    expect(region.className).toMatch(/\brelative\b/)
    expect(region.className).toMatch(/\bborder-l\b/)
    // No other action control shares the region.
    expect(region.querySelectorAll('button')).toHaveLength(1)
    fireEvent.click(chip)
    const caption = document.querySelector('[data-testid="chat-input-backend-chip-explained"]') as HTMLElement
    expect(caption).not.toBeNull()
    expect(region.contains(caption)).toBe(true)
    expect(caption.className).toContain('w-[min(320px,calc(100vw-2rem))]')
    expect(caption.className).not.toMatch(/\bw-\[\d+px\]/)
    expect(caption.className).toMatch(/\bright-0\b/)
  })

  it('an inheriting chat explains that it follows the default, not that a pin was fixed', () => {
    renderWithProviders(
      <ChatInput {...props({ backendLabel: 'Kiro CLI', backendIsInheritedDefault: true })} />,
    )
    fireEvent.click(backendChip()!)
    const caption = document.querySelector('[data-testid="chat-input-backend-chip-explained"]')
    expect(caption!.textContent).toMatch(/follows the default backend/)
    expect(caption!.textContent).not.toMatch(/The backend was fixed when this chat was created/)
    // The lock glyph claims "pinned", so the inheriting chip has none; glyph and
    // caption must agree.
    expect(backendChip()!.querySelector('svg.lucide-lock')).toBeNull()
  })

  it('a pinned chat shows the lock glyph', () => {
    renderWithProviders(
      <ChatInput {...props({ backendLabel: 'Acme Agent' })} />,
    )
    expect(backendChip()!.querySelector('svg.lucide-lock')).not.toBeNull()
  })

  it('carries the pinned-variant tooltip on hover and for screen readers', () => {
    renderWithProviders(
      <ChatInput
        {...props({
          backendLabel: 'Acme Agent',
          backendTitle: 'AI backend serving this chat: Acme Agent — fixed when the chat was created',
        })}
      />,
    )
    const chip = backendChip()!
    expect(chip.getAttribute('title')).toContain('fixed when the chat was created')
    // The title is also the aria-label, and the visible label truncates, so the
    // NAME must be in it for a screen-reader user to hear which backend.
    expect(chip.getAttribute('aria-label')).toContain('Acme Agent')
    expect(chip.getAttribute('aria-label')).toContain('fixed when the chat was created')
  })

  it('falls back to the label for title/aria when no variant title is given', () => {
    renderWithProviders(<ChatInput {...props({ backendLabel: 'Kiro CLI' })} />)
    const chip = backendChip()!
    expect(chip.getAttribute('title')).toBe('Kiro CLI')
    expect(chip.getAttribute('aria-label')).toBe('Kiro CLI')
  })

  it('renders no chip at all without a label', () => {
    renderWithProviders(<ChatInput {...props()} />)
    expect(backendChip()).toBeNull()
  })
})
