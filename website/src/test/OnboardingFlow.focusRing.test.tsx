import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, within, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import OnboardingFlow from '../components/OnboardingFlow'
import PrivacyChapter from '../components/PrivacyChapter'

// Keyboard-only first run (WCAG 2.4.7 Focus Visible): on every onboarding
// dialog, focus must move into the dialog on open, stay inside it, and show a
// visible ring on every Tab stop. The bare global accent outline
// (index.css: :focus-visible { outline: 2px solid var(--accent) }) is invisible
// on an accent-filled control -- a selected chip or the accent Continue button
// -- because the ring and the fill are the same colour. Those controls opt into
// the house .focus-ring-accent utility, whose opaque --text-strong hairline
// stays visible against the accent. A control that keeps the global outline
// passes too, as long as it has not opted out via outline-hidden / outline-none.

vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return {
    ...mod,
    api: {
      ...mod.api,
      kirocrewConfig: vi.fn().mockResolvedValue({
        dashboard: { user_role: '', user_technical_level: '' },
      }),
      patchConfig: vi.fn().mockResolvedValue({}),
      themeBoot: vi.fn().mockResolvedValue({ mode: '', color: '', onboarded: false }),
      beaconStatus: vi.fn().mockResolvedValue({
        enabled: true,
        would_send: true,
        reason: 'ready',
        reason_code: 'ready',
        endpoint_configured: true,
        env_override: false,
        env_var: 'KIROCREW_TELEMETRY_DISABLED',
        governance_override: false,
      }),
      collectionStatus: vi.fn().mockResolvedValue({
        enabled: true,
        env_pinned: false,
        env_var: 'KIROCREW_TELEMETRY',
      }),
    },
  }
})

// A SELECTED chip is drawn with the accent as its OWN fill (inline
// `color: var(--accent)` over an accent-tinted background), so the global accent
// outline blends into it and is not a valid cue -- exactly the bug the reporter
// saw ("the ring is drawn in --accent right next to a selected chip that is also
// filled with the accent colour"). A selected chip must therefore carry a
// .focus-ring-accent* utility, whose opaque --text-strong hairline stays visible
// against the accent fill.
function isSelectedChip(el: HTMLElement): boolean {
  if (el.getAttribute('role') !== 'radio') return false
  if (el.getAttribute('aria-checked') !== 'true') return false
  return (el.getAttribute('style') ?? '').includes('var(--accent)')
}

// A keyboard Tab stop shows a visible focus ring when it carries one of the
// house focus-ring utilities, OR when it keeps the global :focus-visible accent
// outline (i.e. has NOT opted out with outline-hidden / outline-none). Two
// exceptions: a selected chip is accent-on-accent so the global outline does not
// count for it (see isSelectedChip); and a control that opts its own outline out
// but paints the cue on a descendant knob (group-focus-visible:ring-*) is fine.
function hasVisibleFocusCue(el: HTMLElement): boolean {
  const cls = el.className.split(/\s+/)
  const hasRingUtil = cls.some(c => c.startsWith('focus-ring'))
  if (hasRingUtil) return true
  const carriesKnobCue = el.querySelector('[class*="group-focus-visible:ring"]') !== null
  if (carriesKnobCue) return true
  if (isSelectedChip(el)) return false
  const optedOut = cls.includes('outline-hidden') || cls.includes('outline-none')
  return !optedOut
}

// Every element the keyboard can land on inside the dialog, in Tab order enough
// for this check (we assert the set, not the order).
function tabStops(dialog: HTMLElement): HTMLElement[] {
  return Array.from(
    dialog.querySelectorAll<HTMLElement>(
      'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
    ),
  ).filter(el => !el.hasAttribute('disabled') && el.getAttribute('aria-hidden') !== 'true')
}

function assertEveryStopShowsARing(dialog: HTMLElement) {
  const stops = tabStops(dialog)
  expect(stops.length).toBeGreaterThan(0)
  const invisible = stops.filter(el => !hasVisibleFocusCue(el))
  expect(
    invisible.map(el => `${el.tagName.toLowerCase()}:${(el.textContent || el.getAttribute('aria-label') || '').trim().slice(0, 24)}`),
  ).toEqual([])
}

describe('OnboardingFlow / PrivacyChapter - keyboard focus on first-run dialogs', () => {
  beforeEach(() => { localStorage.removeItem('mc-onboarding-step') })

  it('Customize step 1 (Pick your look): every Tab stop shows a visible ring', () => {
    renderWithProviders(<OnboardingFlow initialOpen onComplete={vi.fn()} />)
    expect(screen.getByText('Pick your look')).toBeInTheDocument()
    assertEveryStopShowsARing(screen.getByRole('dialog'))
  })

  it('Customize step 2 (About you): every Tab stop shows a visible ring', () => {
    renderWithProviders(<OnboardingFlow initialOpen onComplete={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    expect(screen.getByText('Tell Kiro about you')).toBeInTheDocument()
    assertEveryStopShowsARing(screen.getByRole('dialog'))
  })

  it('Privacy: every Tab stop shows a visible ring', async () => {
    renderWithProviders(<PrivacyChapter open onContinue={vi.fn()} />)
    // The action buttons wait for the status reads; once "Keep on" is enabled
    // the dialog's controls are all mounted.
    const keepOn = await screen.findByRole('button', { name: 'Keep on' })
    await waitFor(() => expect(keepOn).toBeEnabled())
    assertEveryStopShowsARing(screen.getByRole('dialog'))
  })

  it('the selected chips and the accent Continue button carry the house focus-ring-accent cue', () => {
    renderWithProviders(<OnboardingFlow initialOpen onComplete={vi.fn()} />)
    const dialog = screen.getByRole('dialog')
    for (const chip of within(dialog).getAllByRole('radio')) {
      expect(chip.className.split(/\s+/)).toContain('focus-ring-accent')
    }
    const cont = within(dialog).getByRole('button', { name: 'Continue' })
    expect(cont.className.split(/\s+/)).toContain('focus-ring-accent')
  })

  it('focus moves into the Customize dialog on open and the Tab trap keeps it inside', () => {
    renderWithProviders(<OnboardingFlow initialOpen onComplete={vi.fn()} />)
    const dialog = screen.getByRole('dialog')
    // Focus landed on a control inside the dialog (the first focusable chip).
    expect(dialog.contains(document.activeElement)).toBe(true)
    // Forward Tab from the LAST stop wraps back to the first, so focus never
    // escapes to <body>.
    const stops = tabStops(dialog)
    const last = stops[stops.length - 1]
    last.focus()
    fireEvent.keyDown(document.activeElement as HTMLElement, { key: 'Tab' })
    expect(dialog.contains(document.activeElement)).toBe(true)
    expect(document.activeElement).toBe(stops[0])
  })

  it('focus moves into the Privacy dialog on open and the Tab trap keeps it inside', async () => {
    renderWithProviders(<PrivacyChapter open onContinue={vi.fn()} />)
    const dialog = await screen.findByRole('dialog')
    await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true))
    const stops = tabStops(dialog)
    const last = stops[stops.length - 1]
    last.focus()
    fireEvent.keyDown(document.activeElement as HTMLElement, { key: 'Tab' })
    expect(dialog.contains(document.activeElement)).toBe(true)
  })
})
