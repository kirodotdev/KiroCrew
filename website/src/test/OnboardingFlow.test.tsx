import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, waitFor, act } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import OnboardingFlow from '../components/OnboardingFlow'
import { api } from '../api/client'
import { LIQUID_GLASS_SEEDED_KEY, LIQUID_GLASS_STORAGE_KEY } from '../utils/liquidGlass'
import { lookPreviewSrc } from '../utils/lookPreview'

// Partial api mock: profile read/write + theme boot. Everything else keeps its
// real implementation (ThemeProvider's ancillary fetches no-op in jsdom).
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
        endpoint_configured: true,
        env_override: false,
        env_var: 'KIROCREW_TELEMETRY_DISABLED',
      }),
    },
  }
})

const patchConfig = vi.mocked(api.patchConfig)
const kirocrewConfig = vi.mocked(api.kirocrewConfig)

const advanceToStep2 = () => {
  fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
  expect(screen.getByText('Tell Kiro about you')).toBeInTheDocument()
}

describe('OnboardingFlow — About You step', () => {
  beforeEach(() => {
    // Full reset + re-arm defaults so per-test overrides (mockRejectedValue)
    // can't leak across tests.
    patchConfig.mockReset()
    patchConfig.mockResolvedValue({})
    kirocrewConfig.mockReset()
    kirocrewConfig.mockResolvedValue({
      dashboard: { user_role: '', user_technical_level: '' },
    })
  })

  it('step 1 → Next shows the About You modal with role and technical options', () => {
    renderWithProviders(<OnboardingFlow initialOpen onComplete={vi.fn()} />)
    expect(screen.getByText('Pick your look')).toBeInTheDocument()
    advanceToStep2()
    expect(screen.getByText('Your role')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'UX Designer' })).toBeInTheDocument()
    expect(screen.getByText('How technical are you?')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'I write code' })).toBeInTheDocument()
  })

  it('persists selected role + technical level on Next and advances to the tour', async () => {
    renderWithProviders(<OnboardingFlow initialOpen onComplete={vi.fn()} />)
    advanceToStep2()
    fireEvent.click(screen.getByRole('button', { name: 'UX Designer' }))
    fireEvent.click(screen.getByRole('button', { name: 'Somewhat' }))
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    await waitFor(() => {
      expect(patchConfig).toHaveBeenCalledWith('dashboard.user_role', 'designer')
      expect(patchConfig).toHaveBeenCalledWith(
        'dashboard.user_technical_level',
        'somewhat-technical',
      )
    })
    // Tour popover (step 3) is up next
    expect(await screen.findByText('Work that runs on time')).toBeInTheDocument()
  })

  it('does not write config when nothing was selected', async () => {
    renderWithProviders(<OnboardingFlow initialOpen onComplete={vi.fn()} />)
    advanceToStep2()
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    expect(await screen.findByText('Work that runs on time')).toBeInTheDocument()
    expect(patchConfig).not.toHaveBeenCalled()
  })

  it('deselecting an answer before Next results in no write', async () => {
    renderWithProviders(<OnboardingFlow initialOpen onComplete={vi.fn()} />)
    advanceToStep2()
    const chip = screen.getByRole('button', { name: 'Developer' })
    fireEvent.click(chip)
    expect(chip).toHaveAttribute('aria-pressed', 'true')
    fireEvent.click(chip) // toggle off — back to the initial ''
    expect(chip).toHaveAttribute('aria-pressed', 'false')
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    expect(await screen.findByText('Work that runs on time')).toBeInTheDocument()
    expect(patchConfig).not.toHaveBeenCalled()
  })

  it('Skip on the About You step still persists answers already selected', async () => {
    const onComplete = vi.fn()
    renderWithProviders(<OnboardingFlow initialOpen onComplete={onComplete} />)
    advanceToStep2()
    fireEvent.click(screen.getByRole('button', { name: 'Product Manager' }))
    fireEvent.click(screen.getByRole('button', { name: /Skip/ }))
    await waitFor(() => {
      expect(patchConfig).toHaveBeenCalledWith('dashboard.user_role', 'product-manager')
    })
    await waitFor(() => expect(onComplete).toHaveBeenCalled())
  })

  it('Skip with a failing save keeps the modal open; a second Skip discards explicitly', async () => {
    const onComplete = vi.fn()
    patchConfig.mockRejectedValue(new Error('gateway down'))
    renderWithProviders(<OnboardingFlow initialOpen onComplete={onComplete} />)
    advanceToStep2()
    fireEvent.click(screen.getByRole('button', { name: 'UX Designer' }))
    // First Skip: save fails → informed, NOT dismissed
    fireEvent.click(screen.getByRole('button', { name: /Skip/ }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/Skip again/)
    expect(onComplete).not.toHaveBeenCalled()
    expect(screen.getByText('Tell Kiro about you')).toBeInTheDocument()
    // Second Skip: explicit discard → dismissed
    fireEvent.click(screen.getByRole('button', { name: /Skip/ }))
    await waitFor(() => expect(onComplete).toHaveBeenCalled())
  })

  it('Escape on the About You step dismisses the flow (modal a11y)', async () => {
    const onComplete = vi.fn()
    renderWithProviders(<OnboardingFlow initialOpen onComplete={onComplete} />)
    advanceToStep2()
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(onComplete).toHaveBeenCalled())
  })

  it('preselects previously saved answers for /onboarding replays', async () => {
    kirocrewConfig.mockResolvedValue({
      dashboard: { user_role: 'developer', user_technical_level: 'codes' },
    })
    renderWithProviders(<OnboardingFlow initialOpen onComplete={vi.fn()} />)
    advanceToStep2()
    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Developer' })).toHaveAttribute(
        'aria-pressed',
        'true',
      )
      expect(screen.getByRole('button', { name: 'I write code' })).toHaveAttribute(
        'aria-pressed',
        'true',
      )
    })
    // Unchanged answers → Next writes nothing
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    expect(await screen.findByText('Work that runs on time')).toBeInTheDocument()
    expect(patchConfig).not.toHaveBeenCalled()
  })

  it('failed write keeps the modal open with an error and never advances', async () => {
    patchConfig.mockRejectedValueOnce(new Error('gateway down'))
    renderWithProviders(<OnboardingFlow initialOpen onComplete={vi.fn()} />)
    advanceToStep2()
    fireEvent.click(screen.getByRole('button', { name: 'UX Designer' }))
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    // Error surfaces, still on step 2, tour NOT shown
    expect(await screen.findByRole('alert')).toHaveTextContent(/Couldn't save/)
    expect(screen.getByText('Tell Kiro about you')).toBeInTheDocument()
    expect(screen.queryByText('Work that runs on time')).not.toBeInTheDocument()
  })

  it('Next retries a failed write and advances once it succeeds', async () => {
    patchConfig.mockRejectedValueOnce(new Error('gateway down'))
    renderWithProviders(<OnboardingFlow initialOpen onComplete={vi.fn()} />)
    advanceToStep2()
    fireEvent.click(screen.getByRole('button', { name: 'UX Designer' }))
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    await screen.findByRole('alert')
    // Baseline must NOT have advanced on failure — retry re-sends the field.
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    expect(await screen.findByText('Work that runs on time')).toBeInTheDocument()
    expect(patchConfig).toHaveBeenCalledTimes(2)
    expect(patchConfig).toHaveBeenLastCalledWith('dashboard.user_role', 'designer')
  })

  it('freezes chips, segments, and Skip while a save is in flight', async () => {
    // Hold the PATCH open so the in-flight window is observable.
    let release: (v: unknown) => void = () => {}
    patchConfig.mockImplementationOnce(
      () => new Promise(res => { release = res }),
    )
    const onComplete = vi.fn()
    renderWithProviders(<OnboardingFlow initialOpen onComplete={onComplete} />)
    advanceToStep2()
    fireEvent.click(screen.getByRole('button', { name: 'Developer' }))
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    // In-flight: every input frozen — changing a chip now would advance the
    // flow with a stale value persisted.
    await screen.findByRole('button', { name: 'Saving…' })
    const designerChip = screen.getByRole('button', { name: 'UX Designer' })
    expect(designerChip).toBeDisabled()
    fireEvent.click(designerChip) // no-op while frozen
    expect(designerChip).toHaveAttribute('aria-pressed', 'false')
    expect(screen.getByRole('button', { name: /Skip/ })).toBeDisabled()
    fireEvent.keyDown(document, { key: 'Escape' }) // dismissal frozen too
    expect(onComplete).not.toHaveBeenCalled()
    // Release the PATCH → flow advances with the snapshotted value.
    release({})
    expect(await screen.findByText('Work that runs on time')).toBeInTheDocument()
    expect(patchConfig).toHaveBeenCalledTimes(1)
    expect(patchConfig).toHaveBeenCalledWith('dashboard.user_role', 'developer')
  })

  it('freezes inputs during the Skip-path save too (round-4 race)', async () => {
    let release: (v: unknown) => void = () => {}
    patchConfig.mockImplementationOnce(
      () => new Promise(res => { release = res }),
    )
    const onComplete = vi.fn()
    renderWithProviders(<OnboardingFlow initialOpen onComplete={onComplete} />)
    advanceToStep2()
    fireEvent.click(screen.getByRole('button', { name: 'Developer' }))
    fireEvent.click(screen.getByRole('button', { name: /Skip/ }))
    // Skip's save is in flight: chips must be frozen so the completion that
    // follows can't silently drop an edit made mid-flight.
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'UX Designer' })).toBeDisabled(),
    )
    fireEvent.click(screen.getByRole('button', { name: 'UX Designer' })) // no-op
    expect(screen.getByRole('button', { name: 'UX Designer' })).toHaveAttribute(
      'aria-pressed',
      'false',
    )
    release({})
    await waitFor(() => expect(onComplete).toHaveBeenCalled())
    expect(patchConfig).toHaveBeenCalledTimes(1)
    expect(patchConfig).toHaveBeenCalledWith('dashboard.user_role', 'developer')
  })

  // ── "Other" free-text escape hatch ──────────────────────────────────────

  it('reveals a focused text field only when Other is picked', () => {
    renderWithProviders(<OnboardingFlow initialOpen onComplete={vi.fn()} />)
    advanceToStep2()
    expect(screen.queryByLabelText('Describe your role')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Other' }))
    const input = screen.getByLabelText('Describe your role')
    expect(input).toBeInTheDocument()
    expect(input).toHaveFocus()
    // No `maxLength`: the HTML attribute counts UTF-16 code units, so a paste
    // ending in an astral character would truncate mid-surrogate-pair. The cap
    // is applied in onChange by code point instead.
    expect(input).not.toHaveAttribute('maxLength')
  })

  it('caps the typed role by code point, never splitting a surrogate pair', () => {
    renderWithProviders(<OnboardingFlow initialOpen onComplete={vi.fn()} />)
    advanceToStep2()
    fireEvent.click(screen.getByRole('button', { name: 'Other' }))
    const input = screen.getByLabelText('Describe your role') as HTMLInputElement
    // 59 BMP chars + an astral char: a code-unit slice at 60 would keep only
    // the high surrogate and persist a broken character.
    fireEvent.change(input, { target: { value: 'x'.repeat(59) + '😀' } })
    expect([...input.value]).toHaveLength(60)
    expect(input.value.endsWith('😀')).toBe(true)
    // Past the cap, the astral char is dropped whole rather than halved.
    fireEvent.change(input, { target: { value: 'x'.repeat(60) + '😀' } })
    expect(input.value).toBe('x'.repeat(60))
  })

  it('persists the typed role alongside the other slug', async () => {
    renderWithProviders(<OnboardingFlow initialOpen onComplete={vi.fn()} />)
    advanceToStep2()
    fireEvent.click(screen.getByRole('button', { name: 'Other' }))
    fireEvent.change(screen.getByLabelText('Describe your role'), {
      target: { value: '  solutions architect  ' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    await waitFor(() => {
      expect(patchConfig).toHaveBeenCalledWith('dashboard.user_role', 'other')
      // Trimmed — a stray space would render inside the prompt's quotes.
      expect(patchConfig).toHaveBeenCalledWith(
        'dashboard.user_role_other',
        'solutions architect',
      )
    })
  })

  it('Enter in the field advances instead of leaving the answer unsaved', async () => {
    renderWithProviders(<OnboardingFlow initialOpen onComplete={vi.fn()} />)
    advanceToStep2()
    fireEvent.click(screen.getByRole('button', { name: 'Other' }))
    const input = screen.getByLabelText('Describe your role')
    fireEvent.change(input, { target: { value: 'SRE' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() =>
      expect(patchConfig).toHaveBeenCalledWith('dashboard.user_role_other', 'SRE'),
    )
    expect(await screen.findByText('Work that runs on time')).toBeInTheDocument()
  })

  it('switching from Other to a real chip leaves the stored free text alone', async () => {
    kirocrewConfig.mockResolvedValue({
      dashboard: {
        user_role: 'other',
        user_role_other: 'solutions architect',
        user_technical_level: '',
      },
    })
    renderWithProviders(<OnboardingFlow initialOpen onComplete={vi.fn()} />)
    advanceToStep2()
    // Seeded from the server, so the replay shows what was saved.
    await waitFor(() =>
      expect(screen.getByLabelText('Describe your role')).toHaveValue('solutions architect'),
    )
    fireEvent.click(screen.getByRole('button', { name: 'Developer' }))
    expect(screen.queryByLabelText('Describe your role')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    await waitFor(() =>
      expect(patchConfig).toHaveBeenCalledWith('dashboard.user_role', 'developer'),
    )
    // Deliberately NOT cleared: a second PATCH could succeed while the role
    // PATCH failed, leaving `user_role=other` with its description deleted.
    // The value is inert — context.py only reads it while the role is 'other'.
    expect(patchConfig).toHaveBeenCalledTimes(1)
    expect(patchConfig).not.toHaveBeenCalledWith('dashboard.user_role_other', '')
  })

  it('Other with an empty field writes only the slug', async () => {
    renderWithProviders(<OnboardingFlow initialOpen onComplete={vi.fn()} />)
    advanceToStep2()
    fireEvent.click(screen.getByRole('button', { name: 'Other' }))
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    await waitFor(() =>
      expect(patchConfig).toHaveBeenCalledWith('dashboard.user_role', 'other'),
    )
    // '' equals the baseline, so no needless PATCH for the free text.
    expect(patchConfig).toHaveBeenCalledTimes(1)
  })

  it('keeps the caret in the field while typing (focus-trap regression)', () => {
    // The dialog's focus trap re-runs whenever `finish` changes identity, which
    // happens on every keystroke in this field. Initial focus is keyed to the
    // step so the caret stays in the field instead of the second character
    // going to "Skip all".
    renderWithProviders(<OnboardingFlow initialOpen onComplete={vi.fn()} />)
    advanceToStep2()
    fireEvent.click(screen.getByRole('button', { name: 'Other' }))
    const input = screen.getByLabelText('Describe your role')
    fireEvent.change(input, { target: { value: 'S' } })
    expect(input).toHaveFocus()
    fireEvent.change(input, { target: { value: 'SR' } })
    expect(input).toHaveFocus()
  })

  it('wraps a boundary Tab, but declines one that belongs to an IME composition', () => {
    // On WebKit the keydown that commits a candidate arrives AFTER
    // compositionend with `isComposing` already false — unguarded, the trap
    // would yank focus and abort the composition (issue class of the shared
    // hook's own guard).
    renderWithProviders(<OnboardingFlow initialOpen onComplete={vi.fn()} />)
    advanceToStep2()
    const dialog = screen.getByRole('dialog')
    const focusable = Array.from(
      dialog.querySelectorAll<HTMLElement>(
        'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
      ),
    ).filter(el => !el.hasAttribute('disabled'))
    const first = focusable[0]
    const last = focusable[focusable.length - 1]
    expect(focusable.length).toBeGreaterThan(1)

    // Positive control: a plain boundary Tab still wraps.
    last.focus()
    fireEvent.keyDown(document, { key: 'Tab' })
    expect(document.activeElement).toBe(first)

    // A boundary Tab inside the post-composition window is declined.
    last.focus()
    fireEvent.compositionStart(last)
    fireEvent.compositionEnd(last)
    fireEvent.keyDown(document, { key: 'Tab' })
    expect(document.activeElement).toBe(last)
    expect(document.activeElement).not.toBe(first)
  })

  it('re-seats focus inside the dialog when the save freeze lifts', async () => {
    // Every step-2 control is disabled during the save, so the browser drops
    // focus to <body>. On the failed-save path the modal stays open, and
    // without re-seating focus the Tab trap's first/last comparisons stop
    // matching and Tab escapes the aria-modal dialog.
    patchConfig.mockRejectedValueOnce(new Error('gateway down'))
    renderWithProviders(<OnboardingFlow initialOpen onComplete={vi.fn()} />)
    advanceToStep2()
    fireEvent.click(screen.getByRole('button', { name: 'Other' }))
    fireEvent.change(screen.getByLabelText('Describe your role'), {
      target: { value: 'founder' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    await screen.findByRole('alert')
    expect(document.activeElement).not.toBe(document.body)
    expect(screen.getByRole('button', { name: /Skip/ })).toHaveFocus()
  })

  it('keeps typed text when Other is toggled off and back on', () => {
    renderWithProviders(<OnboardingFlow initialOpen onComplete={vi.fn()} />)
    advanceToStep2()
    const other = screen.getByRole('button', { name: 'Other' })
    fireEvent.click(other)
    fireEvent.change(screen.getByLabelText('Describe your role'), {
      target: { value: 'founder' },
    })
    fireEvent.click(other) // toggle off — field hides
    expect(screen.queryByLabelText('Describe your role')).not.toBeInTheDocument()
    fireEvent.click(other) // back on — the answer is still there
    expect(screen.getByLabelText('Describe your role')).toHaveValue('founder')
  })
})

describe('OnboardingFlow — Pick your look offers the Translucent panels switch', () => {
  afterEach(() => {
    localStorage.removeItem(LIQUID_GLASS_STORAGE_KEY)
    localStorage.removeItem(LIQUID_GLASS_SEEDED_KEY)
    document.documentElement.removeAttribute('data-reduce-transparency')
  })

  it('shows the Settings switch between the mode row and the theme grid', () => {
    renderWithProviders(<OnboardingFlow initialOpen onComplete={vi.fn()} />)
    const sw = screen.getByRole('switch', { name: 'Translucent panels' })
    // Reads in order: mode row (Dark) -> the switch -> the COLOR THEME heading.
    const dark = screen.getByRole('button', { name: 'Dark' })
    const heading = screen.getByText('Color theme')
    expect(dark.compareDocumentPosition(sw) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(sw.compareDocumentPosition(heading) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('turns the glass ON for a fresh store (the key was never written) on the first-run tour', () => {
    expect(localStorage.getItem(LIQUID_GLASS_STORAGE_KEY)).toBeNull()
    renderWithProviders(<OnboardingFlow initialOpen onComplete={vi.fn()} />)
    expect(screen.getByRole('switch', { name: 'Translucent panels' })).toHaveAttribute('aria-checked', 'true')
    expect(localStorage.getItem(LIQUID_GLASS_STORAGE_KEY)).toBe('on')
    expect(localStorage.getItem(LIQUID_GLASS_SEEDED_KEY)).toBe('1')
    expect(document.documentElement.dataset.reduceTransparency).toBe('off')
  })

  it('an already-onboarded user replaying the tour (no marker yet, glass off) is not seeded', () => {
    localStorage.setItem('mc-onboarded', '1')
    try {
      renderWithProviders(<OnboardingFlow initialOpen onComplete={vi.fn()} />)
      expect(screen.getByRole('switch', { name: 'Translucent panels' })).toHaveAttribute('aria-checked', 'false')
      expect(localStorage.getItem(LIQUID_GLASS_STORAGE_KEY)).toBeNull()
      expect(localStorage.getItem(LIQUID_GLASS_SEEDED_KEY)).toBeNull()
    } finally {
      localStorage.removeItem('mc-onboarded')
    }
  })

  it('a reload before the tour is finished does not seed again: the browser remembers the default was offered', () => {
    // Turned off on the first-run step (absent key), then the page reloaded
    // with onboarding still unfinished: the step reopens with initialOpen.
    localStorage.setItem(LIQUID_GLASS_SEEDED_KEY, '1')
    renderWithProviders(<OnboardingFlow initialOpen onComplete={vi.fn()} />)
    expect(screen.getByRole('switch', { name: 'Translucent panels' })).toHaveAttribute('aria-checked', 'false')
    expect(localStorage.getItem(LIQUID_GLASS_STORAGE_KEY)).toBeNull()
    expect(document.documentElement.dataset.reduceTransparency).toBe('on')
  })

  it('turning it off, going to step 2 and coming Back keeps it off (the seed runs once per first run)', () => {
    renderWithProviders(<OnboardingFlow initialOpen onComplete={vi.fn()} />)
    fireEvent.click(screen.getByRole('switch', { name: 'Translucent panels' })) // seeded on -> off
    expect(localStorage.getItem(LIQUID_GLASS_STORAGE_KEY)).toBeNull()
    advanceToStep2()
    fireEvent.click(screen.getByRole('button', { name: 'Back' }))
    expect(screen.getByRole('switch', { name: 'Translucent panels' })).toHaveAttribute('aria-checked', 'false')
    expect(localStorage.getItem(LIQUID_GLASS_STORAGE_KEY)).toBeNull()
    expect(document.documentElement.dataset.reduceTransparency).toBe('on')
  })

  it('a closed mount (every dashboard boot) writes nothing: an absent key stays absent', () => {
    // firstRun.tsx keeps OnboardingFlow mounted for the life of the dashboard so
    // `/onboarding` can reopen it. Off is stored as the absent key, so a seed
    // that ran here would turn an existing user's glass back on at every boot.
    renderWithProviders(<OnboardingFlow initialOpen={false} onComplete={vi.fn()} />)
    expect(screen.queryByRole('switch', { name: 'Translucent panels' })).toBeNull()
    expect(localStorage.getItem(LIQUID_GLASS_STORAGE_KEY)).toBeNull()
    expect(document.documentElement.dataset.reduceTransparency).toBeUndefined()
  })

  it('a /onboarding reopen shows the CURRENT setting and does not seed', async () => {
    renderWithProviders(<OnboardingFlow initialOpen={false} onComplete={vi.fn()} />)
    // The user turned the glass on in Settings after the flow mounted...
    localStorage.setItem(LIQUID_GLASS_STORAGE_KEY, 'on')
    act(() => { window.dispatchEvent(new Event('mc-start-onboarding')) })
    expect(screen.getByRole('switch', { name: 'Translucent panels' })).toHaveAttribute('aria-checked', 'true')
    // ...and a user who turned it OFF (absent key) keeps it off on a reopen.
    localStorage.removeItem(LIQUID_GLASS_STORAGE_KEY)
    fireEvent.click(screen.getByRole('button', { name: 'Skip all setup and onboarding' }))
    await waitFor(() => expect(screen.queryByRole('switch', { name: 'Translucent panels' })).toBeNull())
    act(() => { window.dispatchEvent(new Event('mc-start-onboarding')) })
    expect(screen.getByRole('switch', { name: 'Translucent panels' })).toHaveAttribute('aria-checked', 'false')
    expect(localStorage.getItem(LIQUID_GLASS_STORAGE_KEY)).toBeNull()
  })

  it('flipping it off persists the same key and root attribute Settings does, live', () => {
    renderWithProviders(<OnboardingFlow initialOpen onComplete={vi.fn()} />)
    fireEvent.click(screen.getByRole('switch', { name: 'Translucent panels' }))
    expect(screen.getByRole('switch', { name: 'Translucent panels' })).toHaveAttribute('aria-checked', 'false')
    expect(localStorage.getItem(LIQUID_GLASS_STORAGE_KEY)).toBeNull()
    expect(document.documentElement.dataset.reduceTransparency).toBe('on')
    fireEvent.click(screen.getByRole('switch', { name: 'Translucent panels' }))
    expect(localStorage.getItem(LIQUID_GLASS_STORAGE_KEY)).toBe('on')
    expect(document.documentElement.dataset.reduceTransparency).toBe('off')
  })

  it('is a copy of the Settings row, so it carries no deep-link anchor', () => {
    renderWithProviders(<OnboardingFlow initialOpen onComplete={vi.fn()} />)
    expect(document.querySelector('[data-setting-label="Translucent panels"]')).toBeNull()
  })
})

describe('OnboardingFlow — Pick your look previews the real dashboard', () => {
  it('embeds the dashboard in a passive, scaled, same-origin frame above the controls', () => {
    renderWithProviders(<OnboardingFlow initialOpen onComplete={vi.fn()} />)
    const box = screen.getByTestId('look-preview')
    expect(box).toHaveAttribute('aria-hidden', 'true')
    const frame = box.querySelector('iframe')!
    expect(frame.getAttribute('src')).toBe(lookPreviewSrc())
    expect(frame).toHaveAttribute('inert')
    expect(frame).toHaveAttribute('tabindex', '-1')
    // Above the mode row, so the picture sits over what changes it.
    const system = screen.getByRole('button', { name: 'System' })
    expect(box.compareDocumentPosition(system) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })
})

describe('OnboardingFlow — end of the tour', () => {
  beforeEach(() => {
    patchConfig.mockReset()
    patchConfig.mockResolvedValue({})
    kirocrewConfig.mockReset()
    kirocrewConfig.mockResolvedValue({
      dashboard: { user_role: '', user_technical_level: '' },
    })
  })

  // Walk 2 → 3 → 4 → 5. Popovers 3-4 advance with "Next"; popover 5 is the last
  // step of first run and finishes with "Done". Privacy is NOT part of this flow
  // — it is its own chapter before Customize (see PrivacyChapter.test.tsx).
  const advanceToLastPopover = async () => {
    advanceToStep2()
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    expect(await screen.findByText('Work that runs on time')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Next' }))
    fireEvent.click(screen.getByRole('button', { name: 'Next' }))
    expect(await screen.findByText('Start your first session')).toBeInTheDocument()
  }

  it('ends the tour with Done and no Skip on the last popover', async () => {
    renderWithProviders(<OnboardingFlow initialOpen onComplete={vi.fn()} />)
    advanceToStep2()
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    expect(await screen.findByText('Work that runs on time')).toBeInTheDocument()

    // Popovers 3 and 4 keep Next + Skip.
    expect(screen.getByRole('button', { name: 'Skip' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Next' }))
    expect(screen.getByRole('button', { name: 'Skip' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Next' }))

    // Popover 5 is the end of the tour: Done, and Skip is gone.
    expect(await screen.findByText('Start your first session')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Done' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Next' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Skip' })).not.toBeInTheDocument()
  })

  it('Done on the last popover finishes onboarding', async () => {
    const onComplete = vi.fn()
    const onSkipAll = vi.fn()
    renderWithProviders(
      <OnboardingFlow initialOpen onComplete={onComplete} onSkipAll={onSkipAll} />,
    )
    await advanceToLastPopover()

    fireEvent.click(screen.getByRole('button', { name: 'Done' }))
    await waitFor(() => expect(onComplete).toHaveBeenCalled())
    // Finishing is NOT a skip — Privacy is already behind the user here.
    expect(onSkipAll).not.toHaveBeenCalled()
  })

  // Privacy is mandatory, so every early exit has to be distinguishable from a
  // completion: the host routes a skip back through the Privacy chapter when the
  // user has not passed it yet (shell/boot/firstRun.tsx).
  describe('abandoning the tour reports a SKIP, not a completion', () => {
    const cases: Array<[string, () => void]> = [
      ['"Skip all" on the Customize modal', () => {
        fireEvent.click(screen.getByRole('button', { name: 'Skip all setup and onboarding' }))
      }],
      ['Escape on the Customize modal', () => {
        fireEvent.keyDown(document, { key: 'Escape' })
      }],
    ]

    it.each(cases)('%s', async (_label, abandon) => {
      const onComplete = vi.fn()
      const onSkipAll = vi.fn()
      renderWithProviders(
        <OnboardingFlow initialOpen onComplete={onComplete} onSkipAll={onSkipAll} />,
      )
      abandon()

      await waitFor(() => expect(onSkipAll).toHaveBeenCalledTimes(1))
      expect(onComplete).not.toHaveBeenCalled()
    })

    it('a tour popover Skip', async () => {
      const onComplete = vi.fn()
      const onSkipAll = vi.fn()
      renderWithProviders(
        <OnboardingFlow initialOpen onComplete={onComplete} onSkipAll={onSkipAll} />,
      )
      advanceToStep2()
      fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
      expect(await screen.findByText('Work that runs on time')).toBeInTheDocument()

      fireEvent.click(screen.getByRole('button', { name: 'Skip' }))

      await waitFor(() => expect(onSkipAll).toHaveBeenCalledTimes(1))
      expect(onComplete).not.toHaveBeenCalled()
    })

    // The popover steps are non-modal and have no focus trap, so Escape needs
    // its own binding there — the rule is global, not modal-only.
    it('Escape on a tour popover', async () => {
      const onComplete = vi.fn()
      const onSkipAll = vi.fn()
      renderWithProviders(
        <OnboardingFlow initialOpen onComplete={onComplete} onSkipAll={onSkipAll} />,
      )
      advanceToStep2()
      fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
      expect(await screen.findByText('Work that runs on time')).toBeInTheDocument()

      // Twice: exiting awaits a profile PATCH, and a second Escape mid-flight
      // must not report the skip again (the host would mark onboarded twice).
      fireEvent.keyDown(document, { key: 'Escape' })
      fireEvent.keyDown(document, { key: 'Escape' })

      await waitFor(() => expect(onSkipAll).toHaveBeenCalledTimes(1))
      expect(onComplete).not.toHaveBeenCalled()
    })

    it('falls back to onComplete when the host passes no skip handler', async () => {
      const onComplete = vi.fn()
      renderWithProviders(<OnboardingFlow initialOpen onComplete={onComplete} />)

      fireEvent.click(screen.getByRole('button', { name: 'Skip all setup and onboarding' }))

      await waitFor(() => expect(onComplete).toHaveBeenCalled())
    })
  })

  it('does not render the privacy disclosure — it is its own chapter now', async () => {
    renderWithProviders(<OnboardingFlow initialOpen onComplete={vi.fn()} />)
    await advanceToLastPopover()

    expect(screen.queryByText('Anonymous daily heartbeat')).not.toBeInTheDocument()
    expect(
      screen.queryByRole('switch', { name: 'Send anonymous usage heartbeat' }),
    ).not.toBeInTheDocument()
  })
})
