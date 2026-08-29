/**
 * Tests for the expanded ``SettingsPanel``, covering the defaults +
 * scanner-toggles feature parity port from an older internal version of
 * this app.
 *
 * Behavioural contracts pinned here:
 *
 * * Default audience / doc-type / tone hydrate from ``settingsQuery``
 *   on first non-null resolution and reach the ``updateSettings``
 *   payload on Save. A user's persisted default that is not one of the
 *   built-in enum keys still hydrates -- the ``DropdownWithOther``
 *   component drops into Other mode and shows the custom value in its
 *   text input.
 * * Scanner toggles are represented as a chip-button block; clicks
 *   flip the toggle and the resulting map is included in the Save
 *   payload. The map is a shallow clone of the persisted value so
 *   new keys added by the backend (future scanners) survive round-trip.
 * * Save is disabled until every field has hydrated. This matches
 *   ``NewReviewDialog``'s null-sentinel discipline and prevents a
 *   partial payload from clobbering the server state.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'

// ``SimpleSelect`` (Radix) does not respond to ``fireEvent.change`` and its
// popup is portalled behind an open-then-click cycle in a real browser. Stub
// it with a synchronous native ``<select>`` that exposes the same call
// signature so the behavioural tests below can drive it deterministically
// without pulling in a pointer-event polyfill. Same idiom as
// ``AppsPageW3Coverage.test.tsx``.
vi.mock('../../../components/SimpleSelect', () => ({
  default: ({
    options,
    optionLabels,
    value,
    onChange,
    triggerFallback,
    'aria-label': ariaLabel,
  }: {
    options: string[]
    optionLabels?: string[]
    value: string
    onChange: (nextValue: string) => void
    triggerFallback?: string
    'aria-label'?: string
  }) => (
    <select
      aria-label={ariaLabel}
      value={value}
      onChange={event => onChange(event.target.value)}
    >
      <option value="" disabled>
        {triggerFallback ?? ''}
      </option>
      {options.map((optionValue, optionIndex) => (
        <option key={optionValue} value={optionValue}>
          {optionLabels?.[optionIndex] ?? optionValue}
        </option>
      ))}
    </select>
  ),
}))

import SettingsPanel from './SettingsPanel'
import { writingReviewApi } from '../api'
import { useWritingReview } from '../context'

vi.mock('../context', () => ({
  useWritingReview: vi.fn(),
}))

vi.mock('../api', () => ({
  writingReviewApi: {
    updateSettings: vi.fn(),
  },
  WritingReviewApiError: class WritingReviewApiError extends Error {},
}))

const mockedUseWritingReview = vi.mocked(useWritingReview)
const mockedUpdateSettings = vi.mocked(writingReviewApi.updateSettings)

vi.mock('@tanstack/react-query', async () => {
  const actual =
    await vi.importActual<typeof import('@tanstack/react-query')>('@tanstack/react-query')
  return {
    ...actual,
    useQueryClient: () => ({ invalidateQueries: vi.fn().mockResolvedValue(undefined) }),
  }
})

const stubbedScannerToggles: Record<string, boolean> = {
  clarity: true,
  naturalness: true,
  structure: true,
  evidence: true,
  design: false,
  email: false,
}

function makeFakeContextValueForSettings(
  overrides: Partial<Record<string, unknown>> = {},
) {
  return {
    selectedReviewId: null,
    selectReview: vi.fn(),
    newReviewDialogOpen: false,
    openNewReviewDialog: vi.fn(),
    closeNewReviewDialog: vi.fn(),
    settingsDialogOpen: true,
    openSettingsDialog: vi.fn(),
    closeSettingsDialog: vi.fn(),
    activeJobId: null,
    setActiveJobId: vi.fn(),
    activeJobDocName: null,
    setActiveJobDocName: vi.fn(),
    activeJobPhase: null,
    setActiveJobPhase: vi.fn(),
    reviewsQuery: { data: undefined },
    reviewDetailQuery: { data: undefined },
    settingsQuery: {
      data: {
        default_audience: 'vpLeadership',
        default_doc_type: 'designDocument',
        default_tone: 'conciseExecutive',
        scanner_toggles: stubbedScannerToggles,
        max_concurrent: 6,
      },
    },
    ...overrides,
  } as unknown as ReturnType<typeof useWritingReview>
}

beforeEach(() => {
  mockedUpdateSettings.mockClear()
  mockedUseWritingReview.mockReturnValue(makeFakeContextValueForSettings())
  mockedUpdateSettings.mockResolvedValue({
    default_audience: '',
    default_doc_type: '',
    default_tone: '',
    scanner_toggles: {},
    max_concurrent: 6,
  })
})

describe('SettingsPanel defaults + scanners', () => {
  it('hydrates the default-audience picker from settings on first render', () => {
    render(<SettingsPanel />)
    // Predefined values render via the localized label the built-in
    // options provide -- the select shows "VP / Leadership", not the
    // canonical wire key ``vpLeadership``. Matching by the option's
    // visible name confirms the picker is in select-mode rather than
    // Other-mode.
    const audienceSelect = screen.getByRole('combobox', { name: /default audience/i })
    expect((audienceSelect as HTMLSelectElement).value).toBe('vpLeadership')
  })

  it('drops the default-audience picker into Other mode when the seeded value is custom', () => {
    // Persisted default is a free-form string authored by the user in a
    // previous session. The picker must land in Other mode showing that
    // value in the custom text input; the underlying select shows the
    // "Other (custom)..." sentinel option.
    mockedUseWritingReview.mockReturnValueOnce(
      makeFakeContextValueForSettings({
        settingsQuery: {
          data: {
            default_audience: 'Q3 board deck reviewers',
            default_doc_type: 'designDocument',
            default_tone: 'conciseExecutive',
            scanner_toggles: stubbedScannerToggles,
            max_concurrent: 6,
          },
        },
      }),
    )
    render(<SettingsPanel />)
    const customTextInput = screen.getByRole('textbox', {
      name: /default audience \(custom\)/i,
    })
    expect(customTextInput).toHaveValue('Q3 board deck reviewers')
  })

  it('sends the updated defaults + scanner toggles in the save payload', async () => {
    render(<SettingsPanel />)
    // Flip design scanner ON so the scanner_toggles payload differs
    // from the seeded value in a detectable way.
    const designScannerChip = screen.getByRole('button', { name: /^design$/i })
    fireEvent.click(designScannerChip)

    // Change the default doc type via the built-in picker. The wire
    // value emitted is ``teamUpdate`` (canonical key), not the label.
    const docTypePicker = screen.getByRole('combobox', {
      name: /default document type/i,
    })
    fireEvent.change(docTypePicker, { target: { value: 'teamUpdate' } })

    fireEvent.click(screen.getByRole('button', { name: /^save$/i }))

    await waitFor(() => expect(mockedUpdateSettings).toHaveBeenCalledTimes(1))
    const submittedPatch = mockedUpdateSettings.mock.calls[0][0]
    // Doc type change reached the payload as the canonical key.
    expect(submittedPatch.default_doc_type).toBe('teamUpdate')
    // Untouched defaults ride along at their hydrated values so a
    // partial submit does not accidentally blank out other fields.
    expect(submittedPatch.default_audience).toBe('vpLeadership')
    expect(submittedPatch.default_tone).toBe('conciseExecutive')
    // Scanner toggles include the flipped design chip.
    expect(submittedPatch.scanner_toggles).toBeDefined()
    const submittedScannerToggles = submittedPatch.scanner_toggles as Record<string, boolean>
    expect(submittedScannerToggles.design).toBe(true)
    // Toggles the user didn't touch still ride along -- Save is one
    // atomic patch, not per-field.
    expect(submittedScannerToggles.clarity).toBe(true)
    expect(submittedScannerToggles.email).toBe(false)
  })

  it('lets the user type a custom default via Other mode and includes it on save', async () => {
    render(<SettingsPanel />)
    // Enter Other mode on the audience picker.
    const audienceSelect = screen.getByRole('combobox', { name: /default audience/i })
    fireEvent.change(audienceSelect, { target: { value: '__other__' } })
    const customAudienceInput = screen.getByRole('textbox', {
      name: /default audience \(custom\)/i,
    })
    fireEvent.change(customAudienceInput, {
      target: { value: 'Product-council reviewers' },
    })
    fireEvent.click(screen.getByRole('button', { name: /^save$/i }))
    await waitFor(() => expect(mockedUpdateSettings).toHaveBeenCalledTimes(1))
    const submittedPatch = mockedUpdateSettings.mock.calls[0][0]
    // The typed value reaches the wire verbatim. Downstream the scanner
    // prompt uses raw string pass-through, so this string ends up in
    // the LLM prompt exactly as authored.
    expect(submittedPatch.default_audience).toBe('Product-council reviewers')
  })

  it('disables Save until every field has hydrated from settings', () => {
    // Settings still loading (``data`` is ``undefined``). Save must be
    // disabled so a partial payload cannot overwrite the persisted state.
    mockedUseWritingReview.mockReturnValueOnce(
      makeFakeContextValueForSettings({
        settingsQuery: { data: undefined },
      }),
    )
    render(<SettingsPanel />)
    const saveButton = screen.getByRole('button', { name: /^save$/i })
    expect(saveButton).toBeDisabled()
  })

  it('renders each scanner chip using the localized scanner display name, not the raw wire ID', () => {
    // Regression test for the bug caught 2026-08-29: the ``NewReviewDialog``
    // chip picker was migrated to ``resolveScannerName()`` in Session 14
    // (2026-08-29) but ``SettingsPanel``'s chip picker was missed. Users
    // saw translated labels ("Clarity" / "Klarheit" / "明晰") in the New
    // Review dialog but the raw wire IDs ("clarity") in Settings, in
    // every locale. This test pins that both surfaces resolve through
    // the same shared helper so a future locale addition covers both.
    render(<SettingsPanel />)
    // Locate the chip by its wire-ID data attribute (locale-agnostic;
    // learned correction 2026-08-29 -- name-based lookups break the
    // moment i18nT capitalises or translates the label).
    const clarityScannerChip = document.querySelector(
      '[data-scanner-name="clarity"]',
    )
    expect(clarityScannerChip).not.toBeNull()
    // The visible text must be the resolved i18n label ("Clarity"),
    // not the raw wire ID ("clarity"). Under the default en catalog
    // ``apps.writingReview.scannerNames.clarity`` resolves to "Clarity"
    // with a capital C; the raw ID has no capital.
    expect(clarityScannerChip?.textContent?.trim()).toBe('Clarity')
  })
})
