// SettingsModal model picker: the selector offers the advertised list minus
// 'auto' plus an inherit row, seeds from the stored setting, and persists the
// pick through saveSettings — with '' round-tripping as inherit, never as a
// literal model name.
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import SettingsModal from '../apps/spec-builder/components/SettingsModal'
import { specApi } from '../apps/spec-builder/api'

// The picker reads the advertised list via useAvailableModels. Mock the hook
// module (same pattern as ResearchLabPage.test.tsx): the real hook fetches
// through the provider context, which this harness does not stand up.
// Non-vendor test ids only.
vi.mock('../hooks/useAvailableModels', () => ({
  useAvailableModels: () => [
    { name: 'auto', description: '' },
    { name: 'test-model-x', description: 'Test model X' },
    { name: 'test-model-y', description: 'Test model Y' },
  ],
}))

/* Plain-DOM stand-in for SimpleSelect, for the documented harness limit: the
   real one is a Radix Select, whose discrete events commit via flushSync and
   throw "Should not already be working." inside Testing Library's act().
   test/IssueRadarCrewEditor.test.tsx stubs the same component for the same
   reason. The stub MIRRORS the real trigger gate (SimpleSelect's selectable():
   a value not in options renders triggerFallback ?? clearLabel, '' renders
   clearLabel) so the unavailable-pin display case is testable here — a stub
   that always echoed the value would certify seeding while being structurally
   unable to catch a trigger regression. */
vi.mock('../components/SimpleSelect', () => ({
  default: ({
    options,
    value,
    onChange,
    clearLabel,
    triggerFallback,
    'aria-label': ariaLabel,
  }: {
    options: string[]
    value: string
    onChange: (v: string) => void
    clearLabel?: string
    triggerFallback?: string
    'aria-label'?: string
  }) => {
    const emptySelectable = clearLabel !== undefined || options.includes('')
    const selectable = options.includes(value) || (value === '' && emptySelectable)
    const trigger = selectable
      ? value === '' && clearLabel !== undefined
        ? clearLabel
        : value
      : (triggerFallback ?? clearLabel ?? '')
    return (
      <div>
        <div role="combobox" aria-label={ariaLabel} aria-controls="sb-model-options" aria-expanded tabIndex={0}>
          {trigger}
        </div>
        {/* Native buttons so the stub carries focus + keyboard for free (no
            jsx-a11y warnings) — the real Radix rows are focusable options. */}
        <div id="sb-model-options" role="listbox" aria-label={ariaLabel}>
          {clearLabel !== undefined && (
            <button type="button" role="option" aria-selected={value === ''} onClick={() => onChange('')}>
              {clearLabel}
            </button>
          )}
          {options.map((o) => (
            <button key={o} type="button" role="option" aria-selected={o === value} onClick={() => onChange(o)}>
              {o}
            </button>
          ))}
        </div>
      </div>
    )
  },
}))

function renderModal() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  render(
    <QueryClientProvider client={qc}>
      <SettingsModal onClose={() => {}} setErr={() => {}} />
    </QueryClientProvider>,
  )
}

const saveButton = () => screen.getByRole('button', { name: /save/i })

describe('SettingsModal model picker', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('offers the advertised models minus auto, plus the inherit row', async () => {
    vi.spyOn(specApi, 'getSettings').mockResolvedValue({ base_path: '', model: '' })
    renderModal()
    await waitFor(() => expect(saveButton()).toBeEnabled())

    const options = screen.getAllByRole('option').map((o) => o.textContent)
    expect(options).toContain('test-model-x')
    expect(options).toContain('test-model-y')
    expect(options).not.toContain('auto')
    // The inherit row is present and selected while the value is ''.
    expect(screen.getByRole('combobox', { name: /spec generation model/i })).toHaveTextContent(
      /default \(inherit\)/i,
    )
  })

  it('seeds the picker from the stored setting', async () => {
    vi.spyOn(specApi, 'getSettings').mockResolvedValue({ base_path: '', model: 'test-model-y' })
    renderModal()
    await waitFor(() =>
      expect(screen.getByRole('combobox', { name: /spec generation model/i })).toHaveTextContent(
        'test-model-y',
      ),
    )
  })

  it('keeps a retained pin visible when it is not in the advertised list', async () => {
    // The exact state the KEPT-unknown-names decision produces: the stored
    // model is no longer served (or the list has not loaded yet). The trigger
    // must show the raw pin, NOT claim "Default (inherit)" while the stamp
    // keeps applying that model to new spec slots.
    vi.spyOn(specApi, 'getSettings').mockResolvedValue({ base_path: '', model: 'test-model-gone' })
    renderModal()
    const combo = () => screen.getByRole('combobox', { name: /spec generation model/i })
    await waitFor(() => expect(combo()).toHaveTextContent('test-model-gone'))
    expect(combo()).not.toHaveTextContent(/default \(inherit\)/i)
  })

  it('persists a pick through saveSettings alongside the base path', async () => {
    vi.spyOn(specApi, 'getSettings').mockResolvedValue({ base_path: '/srv/specs', model: '' })
    const saveSpy = vi.spyOn(specApi, 'saveSettings').mockResolvedValue({ ok: true })
    renderModal()
    await waitFor(() => expect(saveButton()).toBeEnabled())

    fireEvent.click(screen.getByRole('option', { name: 'test-model-x' }))
    fireEvent.click(saveButton())

    await waitFor(() => expect(saveSpy).toHaveBeenCalledWith('/srv/specs', 'test-model-x'))
  })

  it("round-trips an empty selection as inherit, not a literal model name", async () => {
    vi.spyOn(specApi, 'getSettings').mockResolvedValue({ base_path: '', model: 'test-model-x' })
    const saveSpy = vi.spyOn(specApi, 'saveSettings').mockResolvedValue({ ok: true })
    renderModal()
    await waitFor(() => expect(saveButton()).toBeEnabled())

    // Clear the pick back to inherit, then save: '' must be sent, not dropped.
    fireEvent.click(screen.getByRole('option', { name: /default \(inherit\)/i }))
    fireEvent.click(saveButton())

    await waitFor(() => expect(saveSpy).toHaveBeenCalledWith('', ''))
  })
})
