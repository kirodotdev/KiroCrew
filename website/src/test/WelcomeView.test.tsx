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
    rerender(<WelcomeView {...defaultProps} memoryMode="persistent" />)
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

  it('renders Autopilot heading in orchestrator mode', () => {
    renderWithProviders(<WelcomeView {...defaultProps} mode="orchestrator" />)
    expect(screen.getByText('Autopilot')).toBeInTheDocument()
  })

  it('shows the orchestrator try button only in orchestrator mode', () => {
    const { rerender } = renderWithProviders(<WelcomeView {...defaultProps} />)
    expect(screen.queryByText(/Try:/)).not.toBeInTheDocument()
    rerender(<WelcomeView {...defaultProps} mode="orchestrator" />)
    expect(screen.getByText(/Try:/)).toBeInTheDocument()
  })

  it('keeps the memory chip out of the non-orchestrator body (ChatPage puts it above the composer)', () => {
    renderWithProviders(<WelcomeView {...defaultProps} onSwitchMode={vi.fn()} />)
    expect(screen.queryByText('Choose memory mode')).not.toBeInTheDocument()
  })

  it('still shows the memory chip inside the orchestrator view', () => {
    renderWithProviders(<WelcomeView {...defaultProps} mode="orchestrator" onSwitchMode={vi.fn()} />)
    expect(screen.getByText('Choose memory mode')).toBeInTheDocument()
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

    it('has no header row; refresh is a "Refresh suggestions" text button after the grid', () => {
      renderWithProviders(<WelcomeView {...defaultProps} />)
      expect(screen.queryByText('Suggested for you')).not.toBeInTheDocument()
      const refresh = screen.getByRole('button', { name: 'Refresh suggestions' })
      expect(refresh.textContent).toBe('Refresh suggestions')
      const card = screen.getByRole('button', { name: FALLBACK_PILL })
      expect(card.compareDocumentPosition(refresh) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    })

    it('stacks the refresh row above a hovered card so an expanded last-row card cannot cover it', () => {
      renderWithProviders(<WelcomeView {...defaultProps} />)
      const row = screen.getByRole('button', { name: 'Refresh suggestions' }).parentElement!
      const cell = screen.getByRole('button', { name: FALLBACK_PILL }).parentElement!
      expect(row.className).toContain('relative')
      expect(row.className).toContain('z-20')
      expect(cell.className).toContain('hover:z-10')
    })

    it('uses a start-aligned narrow stack and switches to the spread grid only on wide, tall viewports', () => {
      renderWithProviders(<WelcomeView {...defaultProps} />)
      const layout = screen.getByTestId('welcome-layout')
      expect(layout.className).toContain('flex')
      expect(layout.className).toContain('flex-col')
      expect(layout.className).toContain('justify-start')
      expect(layout.className).not.toMatch(/(?:^|\s)justify-center(?:\s|$)/)
      expect(layout.className).toContain('[@media(min-width:640px)_and_(min-height:600px)]:grid')
      expect(layout.className).toContain('[@media(min-width:640px)_and_(min-height:600px)]:grid-rows-[1.3fr_auto_0.7fr]')
      const rows = Array.from(layout.children)
      expect(rows).toHaveLength(3)
      expect(rows[0].querySelector('h2')).toBeTruthy()
      expect(rows[1].contains(screen.getByRole('button', { name: FALLBACK_PILL }))).toBe(true)
    })

    it('orchestrator mode does not use the spread layout', () => {
      renderWithProviders(<WelcomeView {...defaultProps} mode="orchestrator" />)
      expect(screen.queryByTestId('welcome-layout')).not.toBeInTheDocument()
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
  })
})
