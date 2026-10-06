import { describe, it, expect, vi, afterEach } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import WelcomeView, { normalizeSuggestion, welcomeGreetingKeys, welcomeGreetings } from '../components/WelcomeView'
import { i18nT } from '../i18n/t'
import { MemoryModeChip } from '../components/MemoryModeChip'

const defaultProps = {
  setInput: vi.fn(),
}

describe('WelcomeView', () => {
  afterEach(() => { vi.restoreAllMocks() })

  it('renders the first pool greeting when Math.random picks index 0', () => {
    vi.spyOn(Math, 'random').mockReturnValue(0)
    renderWithProviders(<WelcomeView {...defaultProps} />)
    expect(screen.getByText('What can I do for you?')).toBeInTheDocument()
  })

  it('renders a heading drawn from the greeting pool', () => {
    renderWithProviders(<WelcomeView {...defaultProps} />)
    expect(welcomeGreetings()).toContain(screen.getByRole('heading', { level: 2 }).textContent)
  })

  it('keeps the same greeting across re-renders', () => {
    const random = vi.spyOn(Math, 'random').mockReturnValue(0.3)
    const { rerender } = renderWithProviders(<WelcomeView {...defaultProps} />)
    const first = screen.getByRole('heading', { level: 2 }).textContent
    random.mockReturnValue(0.99)
    rerender(<WelcomeView {...defaultProps} />)
    rerender(<WelcomeView setInput={vi.fn()} />)
    expect(screen.getByRole('heading', { level: 2 }).textContent).toBe(first)
  })

  it('resolves the time-of-day greeting from the local hour', () => {
    const last = (h: number) => i18nT(welcomeGreetingKeys(h).at(-1)!)
    expect(last(8)).toBe('Good morning. Where should we start?')
    expect(last(12)).toBe('Good afternoon. Where should we start?')
    expect(last(17)).toBe('Good afternoon. Where should we start?')
    expect(last(18)).toBe('Good evening. Where should we start?')
  })

  it('keeps the memory chip out of the welcome body (ChatPage puts it above the composer)', () => {
    renderWithProviders(<WelcomeView {...defaultProps} />)
    expect(screen.queryByText('Choose memory mode')).not.toBeInTheDocument()
  })

  it('MemoryModeChip shows the chooser label in persistent mode', () => {
    renderWithProviders(<MemoryModeChip onSwitchMode={vi.fn()} />)
    expect(screen.getByText('Choose memory mode')).toBeInTheDocument()
  })

  it('names the active Incognito mode and persistent reset action', () => {
    renderWithProviders(<MemoryModeChip onSwitchMode={vi.fn()} memoryMode="incognito" />)
    expect(screen.getByText('Incognito — switch to persistent mode')).toBeInTheDocument()
  })

  describe('suggestion pills', () => {
    // Falls back to FALLBACK_SUGGESTIONS when api.suggestions is unmocked.
    const FALLBACK_PILL = 'Check my pipeline status'

    it('pill is type=button and prevents mousedown default so the textarea keeps focus', () => {
      renderWithProviders(<WelcomeView {...defaultProps} />)
      const pill = screen.getByRole('button', { name: FALLBACK_PILL })
      expect(pill).toHaveAttribute('type', 'button')
      // fireEvent returns false when the (cancelable) event had preventDefault
      // called — i.e. focus stays in the textarea instead of moving to the pill,
      // so a follow-up Enter sends instead of re-activating this button.
      const notCancelled = fireEvent.mouseDown(pill)
      expect(notCancelled).toBe(false)
    })

    it('clicking a pill sets the input to the suggestion text', () => {
      const setInput = vi.fn()
      renderWithProviders(<WelcomeView setInput={setInput} />)
      fireEvent.click(screen.getByRole('button', { name: FALLBACK_PILL }))
      expect(setInput).toHaveBeenCalledWith(FALLBACK_PILL)
    })

    it('has no header row and no refresh control: the grid is the whole surface', () => {
      renderWithProviders(<WelcomeView {...defaultProps} />)
      expect(screen.queryByText('Suggested for you')).not.toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'Refresh suggestions' })).not.toBeInTheDocument()
      // Every button is a suggestion card; nothing trails the grid.
      const buttons = screen.getAllByRole('button')
      expect(buttons).toHaveLength(6)
      for (const b of buttons) expect(b).toHaveAttribute('data-kind')
    })

    it('phone rows are bare and tight; the sm+ card chrome is all sm-prefixed', () => {
      renderWithProviders(<WelcomeView {...defaultProps} />)
      const card = screen.getByRole('button', { name: FALLBACK_PILL })
      const grid = card.parentElement!.parentElement!
      expect(grid.className).toContain('gap-0.5')
      expect(grid.className).toContain('sm:gap-3')
      expect(card.className).toContain('border-transparent')
      expect(card.className).toContain('sm:border-border')
      expect(card.className).toContain('bg-transparent')
      expect(card.className).toContain('sm:bg-card')
      expect(card.className).toContain('py-1.5')
      expect(card.className).toContain('hover:bg-bg-hover')
      // A hovered sm+ card still rises above its neighbours.
      expect(card.parentElement!.className).toContain('hover:z-10')
    })

    it('safe-centres the phone stack, start-aligns short wide windows, and switches to the spread grid only on wide, tall viewports', () => {
      renderWithProviders(<WelcomeView {...defaultProps} />)
      const layout = screen.getByTestId('welcome-layout')
      expect(layout.className).toContain('flex')
      expect(layout.className).toContain('flex-col')
      expect(layout.className).toContain('[justify-content:safe_center]')
      expect(layout.className).toContain('sm:justify-start')
      expect(layout.className).not.toMatch(/(?:^|\s)justify-center(?:\s|$)/)
      expect(layout.className).toContain('[@media(min-width:640px)_and_(min-height:600px)]:grid')
      expect(layout.className).toContain('[@media(min-width:640px)_and_(min-height:600px)]:grid-rows-[1.3fr_auto_0.7fr]')
      const rows = Array.from(layout.children)
      expect(rows).toHaveLength(3)
      expect(rows[0].querySelector('h2')).toBeTruthy()
      expect(rows[1].contains(screen.getByRole('button', { name: FALLBACK_PILL }))).toBe(true)
    })

    it('cards sit in fixed-height cells and expose their full text as the accessible name', () => {
      renderWithProviders(<WelcomeView {...defaultProps} />)
      const card = screen.getByRole('button', { name: FALLBACK_PILL })
      // The clamp is visual only: the whole suggestion is still the button's name.
      expect(card).toHaveAccessibleName(FALLBACK_PILL)
      expect(card.className).toContain('absolute')
      expect(card.parentElement!.className).toContain('h-[108px]')
    })

    it('fallback cards carry their mapped kind', () => {
      renderWithProviders(<WelcomeView {...defaultProps} />)
      expect(screen.getByRole('button', { name: FALLBACK_PILL })).toHaveAttribute('data-kind', 'ops')
      expect(screen.getByRole('button', { name: 'Review my latest CR' })).toHaveAttribute('data-kind', 'review')
    })
  })

  describe('normalizeSuggestion', () => {
    it('treats a legacy string item as general', () => {
      expect(normalizeSuggestion('zzq plain')).toEqual({ text: 'zzq plain', kind: 'general' })
    })

    it('keeps a known kind from an object item', () => {
      expect(normalizeSuggestion({ text: 'zzq obj', kind: 'schedule' })).toEqual({ text: 'zzq obj', kind: 'schedule' })
    })

    it('maps an unknown or missing kind to general', () => {
      expect(normalizeSuggestion({ text: 'zzq odd', kind: 'mystery' }).kind).toBe('general')
      expect(normalizeSuggestion({ text: 'zzq none' }).kind).toBe('general')
      expect(normalizeSuggestion({ text: 'zzq proto', kind: 'toString' }).kind).toBe('general')
    })

    it('drops a nested {text} object and other non-strings to empty (React #31 guard)', () => {
      // The server payload is LLM-generated; a nested object here rendered raw
      // as a React child threw minified error #31 with keys {text, kind}.
      // Strict-drop: any non-string text becomes '' so the caller filters it out
      // rather than reshaping malformed model output into a card.
      expect(normalizeSuggestion({ text: { text: 'zzq nested', kind: 'ops' } as unknown as string, kind: 'ops' }).text)
        .toBe('')
      expect(normalizeSuggestion({ text: 42 as unknown as string, kind: 'ops' }).text).toBe('')
      expect(normalizeSuggestion({ text: null as unknown as string }).text).toBe('')
    })

    it('coerces a null or non-object item to an empty suggestion instead of throwing', () => {
      expect(normalizeSuggestion(null as unknown as string)).toEqual({ text: '', kind: 'general' })
      expect(normalizeSuggestion(7 as unknown as string)).toEqual({ text: '', kind: 'general' })
    })
  })
})
