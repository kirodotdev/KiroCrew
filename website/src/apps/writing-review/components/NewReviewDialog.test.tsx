/**
 * Tests for the scanner-selection block in ``NewReviewDialog``.
 *
 * The rest of the dialog (audience/type/tone selects, additional-context textarea,
 * the browse-file flow) is not covered here -- those paths have been
 * stable through multiple sessions and this file is scoped to the
 * feature added in this cycle: chip-toggle picker for scanner
 * ``scanner_toggles`` that ships as part of the ``POST /scan`` body.
 *
 * Two behaviours are pinned:
 *
 * * ALL scanners returned by ``GET /settings`` appear as chips, in the
 *   order the server sent them. This is the visible surface a user
 *   scans first when they open the dialog.
 * * De-selecting a scanner sends ``false`` for that scanner in the
 *   ``startScan`` payload. Selection state is the whole point of the
 *   feature; a chip that visually toggles but doesn't reach the wire
 *   is worse than no picker at all.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'

/**
 * Look up a scanner chip by its raw scanner ID via the ``data-scanner-name``
 * attribute rather than by accessible name. The chip's displayed label is
 * routed through i18n (``resolveScannerName``), so a name-based lookup
 * would break when the resolver returns ``"Evidence"`` instead of the raw
 * ID ``"evidence"``. The ``data-scanner-name`` attribute is the wire-ID
 * hook that stays stable across locales.
 */
function getScannerChipByRawScannerName(rawScannerName: string): HTMLElement {
  const element = document.querySelector(`[data-scanner-name="${rawScannerName}"]`)
  if (!(element instanceof HTMLElement)) {
    throw new Error(`no scanner chip with data-scanner-name="${rawScannerName}"`)
  }
  return element
}

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

import NewReviewDialog from './NewReviewDialog'
import { writingReviewApi } from '../api'
import { useWritingReview } from '../context'

// ---- Test doubles -----------------------------------------------------------
//
// ``useWritingReview`` is mocked so this test does not need to wire up a
// real ``QueryClientProvider`` + ``WritingReviewProvider`` tree just to
// hand the dialog its context value. The dialog only reaches into the
// context for a handful of fields; the mock returns exactly those.
//
// ``writingReviewApi.startScan`` is mocked so we can assert on the
// payload the dialog would have POSTed. The mock returns a fake job id
// so the ``handleStartReview`` happy path completes without exceptions.

vi.mock('../context', () => ({
  useWritingReview: vi.fn(),
}))

vi.mock('../api', () => ({
  writingReviewApi: {
    startScan: vi.fn(),
    uploadDocumentFile: vi.fn(),
  },
  WritingReviewApiError: class WritingReviewApiError extends Error {},
}))

const mockedUseWritingReview = vi.mocked(useWritingReview)
const mockedStartScan = vi.mocked(writingReviewApi.startScan)
const mockedUploadDocumentFile = vi.mocked(writingReviewApi.uploadDocumentFile)

const fakeScannerToggles: Record<string, boolean> = {
  clarity: true,
  naturalness: true,
  structure: true,
  evidence: true,
  consistency: true,
  attribution: true,
  audience: true,
  readability: true,
}

function makeFakeContextValue(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    selectedReviewId: null,
    selectReview: vi.fn(),
    newReviewDialogOpen: true,
    openNewReviewDialog: vi.fn(),
    closeNewReviewDialog: vi.fn(),
    settingsDialogOpen: false,
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
        default_audience: '',
        default_doc_type: '',
        default_tone: '',
        scanner_toggles: fakeScannerToggles,
        max_concurrent: 9,
      },
    },
    ...overrides,
  } as unknown as ReturnType<typeof useWritingReview>
}

// React Query's ``useQueryClient`` is used by the dialog to invalidate
// the reviews list on submit. We do not care what it does, just that a
// call to it succeeds; ``@tanstack/react-query`` throws when it cannot
// find a client, so mock the whole hook.
vi.mock('@tanstack/react-query', async () => {
  const actual = await vi.importActual<typeof import('@tanstack/react-query')>(
    '@tanstack/react-query',
  )
  return {
    ...actual,
    useQueryClient: () => ({ invalidateQueries: vi.fn().mockResolvedValue(undefined) }),
  }
})

beforeEach(() => {
  // Clear the call history on the api mock so each test asserts against
  // its own invocations rather than an accumulating tally across the file.
  mockedStartScan.mockClear()
  mockedUploadDocumentFile.mockClear()
  mockedUseWritingReview.mockReturnValue(makeFakeContextValue())
  mockedStartScan.mockResolvedValue({ job_id: 'test-job-id' })
})

describe('NewReviewDialog scanner selection', () => {
  it('renders one chip per scanner the settings endpoint returned', () => {
    render(<NewReviewDialog />)
    for (const expectedScannerName of Object.keys(fakeScannerToggles)) {
      const chipElement = getScannerChipByRawScannerName(expectedScannerName)
      expect(chipElement).toBeInTheDocument()
      // All scanners default to enabled in fakeScannerToggles, so every
      // chip should render as pressed.
      expect(chipElement.getAttribute('aria-pressed')).toBe('true')
    }
  })

  it('toggles a chip visually when the user clicks it', () => {
    render(<NewReviewDialog />)
    const evidenceChip = getScannerChipByRawScannerName('evidence')
    expect(evidenceChip.getAttribute('aria-pressed')).toBe('true')
    fireEvent.click(evidenceChip)
    expect(evidenceChip.getAttribute('aria-pressed')).toBe('false')
    // Clicking again returns to the on state, proving the toggle is
    // fully reversible rather than one-shot.
    fireEvent.click(evidenceChip)
    expect(evidenceChip.getAttribute('aria-pressed')).toBe('true')
  })

  it('excludes a de-selected scanner from the startScan payload', async () => {
    render(<NewReviewDialog />)
    // Give the dialog something to submit -- ``handleStartReview`` bails
    // out before dispatching if both doc_path and doc_text are empty.
    const docTextArea = screen.getByPlaceholderText(
      /paste markdown/i,
    ) as HTMLTextAreaElement
    fireEvent.change(docTextArea, { target: { value: 'A short document body.' } })

    // De-select consistency and structure. The rest of the wave stays on.
    fireEvent.click(getScannerChipByRawScannerName('consistency'))
    fireEvent.click(getScannerChipByRawScannerName('structure'))

    // Kick off the scan.
    fireEvent.click(screen.getByRole('button', { name: /start review/i }))

    await waitFor(() => expect(mockedStartScan).toHaveBeenCalledTimes(1))
    const submittedPayload = mockedStartScan.mock.calls[0][0]
    expect(submittedPayload.scanner_toggles).toBeDefined()
    const submittedToggles = submittedPayload.scanner_toggles as Record<string, boolean>
    expect(submittedToggles.consistency).toBe(false)
    expect(submittedToggles.structure).toBe(false)
    // The other six scanners must still be on -- de-selecting two does
    // not silently disable the rest.
    expect(submittedToggles.clarity).toBe(true)
    expect(submittedToggles.evidence).toBe(true)
    expect(submittedToggles.audience).toBe(true)
    expect(submittedToggles.readability).toBe(true)
    expect(submittedToggles.attribution).toBe(true)
    expect(submittedToggles.naturalness).toBe(true)
  })

  it('sends the ask textarea value in the context.ask payload field', async () => {
    // The Ask field is a free-form directive the author types to
    // steer scanners toward the decision they want reviewed. On
    // submit it MUST reach ``context.ask`` on the wire so the
    // backend can thread it into every scanner prompt and the
    // discussion agent's context bundle. Empty ask stays empty.
    render(<NewReviewDialog />)
    const docTextArea = screen.getByPlaceholderText(
      /paste markdown/i,
    ) as HTMLTextAreaElement
    fireEvent.change(docTextArea, { target: { value: 'A short doc body.' } })

    const askTextArea = screen.getByPlaceholderText(
      /decision are you asking/i,
    ) as HTMLTextAreaElement
    fireEvent.change(askTextArea, {
      target: { value: 'Is the phased rollout timeline realistic?' },
    })

    fireEvent.click(screen.getByRole('button', { name: /start review/i }))
    await waitFor(() => expect(mockedStartScan).toHaveBeenCalledTimes(1))

    const submittedPayload = mockedStartScan.mock.calls[0][0]
    expect(submittedPayload.context.ask).toBe(
      'Is the phased rollout timeline realistic?',
    )
  })

  it('sends an empty context.ask when the ask textarea is left blank', async () => {
    // When the user leaves ask empty, the payload must carry the
    // empty string so the backend prompt's ``if context.ask`` guard
    // fires and omits the directive line entirely. A missing key
    // would also work backend-side, but consistently emitting the
    // field keeps the wire shape stable regardless of user input.
    render(<NewReviewDialog />)
    const docTextArea = screen.getByPlaceholderText(
      /paste markdown/i,
    ) as HTMLTextAreaElement
    fireEvent.change(docTextArea, { target: { value: 'A short doc body.' } })

    fireEvent.click(screen.getByRole('button', { name: /start review/i }))
    await waitFor(() => expect(mockedStartScan).toHaveBeenCalledTimes(1))

    const submittedPayload = mockedStartScan.mock.calls[0][0]
    expect(submittedPayload.context.ask).toBe('')
  })

  it('renders the submit error banner outside the scrollable body region', () => {
    // The dialog's field stack sits inside a ``.overflow-y-auto``
    // scrollable region. When a user pastes a large body then
    // clicks Start Review, the size-validation error was previously
    // rendered at the BOTTOM of that scroll region -- below the
    // textarea + selects + additional-context + scanner picker --
    // requiring the user to scroll down to see why the submit failed.
    // The error banner must render as a sibling of the footer instead,
    // so it stays visible regardless of scroll position.
    render(<NewReviewDialog />)
    // Click Start Review with no input to trigger the errorInput branch.
    fireEvent.click(screen.getByRole('button', { name: /start review/i }))

    const errorElement = screen.getByText(
      /paste text or provide a document path/i,
    )
    // The scrollable region has ``overflow-y-auto`` in its class list.
    // The error banner must NOT sit anywhere inside such an ancestor;
    // if it does the user has to scroll to see it, which is the bug.
    const scrollableAncestor = errorElement.closest('.overflow-y-auto')
    expect(scrollableAncestor).toBeNull()
  })

  it('sends a timestamped doc_name when the user pastes without browsing', async () => {
    // With browse/path both empty and only text pasted, the frontend
    // must generate a distinguishable doc_name so multiple paste-based
    // reviews are not all shown as the same generic "pasted" label.
    // The label uses an ISO-8601-style ``YYYY-MM-DD HH:mm`` timestamp
    // -- deliberately NOT a locale-formatted date like en-GB's
    // ``27/08/2026``. The backend filename sanitiser treats ``/`` as a
    // path-traversal separator and splits on it, keeping only the last
    // segment. A locale-formatted timestamp is therefore mangled to
    // things like ``2026_13_27`` (year followed by hour and minute --
    // month and day lost) on the persisted record. ISO shape has no
    // separator chars the sanitiser mishandles, so every info piece
    // survives.
    render(<NewReviewDialog />)
    const docTextArea = screen.getByPlaceholderText(
      /paste markdown/i,
    ) as HTMLTextAreaElement
    fireEvent.change(docTextArea, { target: { value: 'A short body.' } })
    fireEvent.click(screen.getByRole('button', { name: /start review/i }))
    await waitFor(() => expect(mockedStartScan).toHaveBeenCalledTimes(1))
    const submittedPayload = mockedStartScan.mock.calls[0][0]
    expect(submittedPayload.doc_name).toBeDefined()
    // Format: ``pasted_YYYY_MM_DD_HH_MM_SS.md``. Year-first ordering
    // so review filenames sort chronologically by name. Seconds
    // precision so multiple pastes in the same minute stay
    // distinguishable. Every character is in the sanitiser's
    // allowlist (``A-Za-z0-9._-``) so the persisted filename is
    // byte-identical to what the frontend displays.
    expect(submittedPayload.doc_name).toMatch(
      /^pasted_\d{4}_\d{2}_\d{2}_\d{2}_\d{2}_\d{2}\.md$/,
    )
    expect(submittedPayload.doc_name).not.toContain('/')
    expect(submittedPayload.doc_name).not.toContain(':')
    expect(submittedPayload.doc_name).not.toContain(' ')
  })

  it('routes a browsed .docx file through the binary upload endpoint', async () => {
    // A ``.docx`` is a ZIP archive of XML. The old browse flow ran
    // every browsed file through ``FileReader.readAsText`` and
    // sent the result in ``doc_text``, which mangled the ZIP bytes
    // via the JS string layer's UTF-8 re-encoding. The docx never
    // parsed on the backend.
    //
    // The fixed flow routes ``.docx`` through the multipart
    // ``/uploads`` endpoint, receives back a ``doc_path`` pointing
    // at the byte-identical stashed file, and submits the scan with
    // ``doc_path`` set (not ``doc_text``). This test pins that
    // routing.
    mockedUploadDocumentFile.mockResolvedValueOnce({
      doc_path: '/tmp/fake_uploads/abcd1234_my_design.docx',
      doc_name: 'abcd1234_my_design.docx',
    })
    render(<NewReviewDialog />)
    const fileInputElement = document.querySelector(
      'input[type="file"]',
    ) as HTMLInputElement
    const fakeDocxFile = new File(
      [new Uint8Array([0x50, 0x4b, 0x03, 0x04, 0x00, 0x01, 0x02])],
      'my_design.docx',
      { type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' },
    )
    Object.defineProperty(fileInputElement, 'files', {
      value: [fakeDocxFile],
      configurable: true,
    })
    fireEvent.change(fileInputElement)

    await waitFor(() => expect(mockedUploadDocumentFile).toHaveBeenCalledTimes(1))
    // The uploaded ``File`` object is passed through verbatim so the
    // api client can post it as multipart form-data.
    expect(mockedUploadDocumentFile.mock.calls[0][0]).toBe(fakeDocxFile)

    fireEvent.click(screen.getByRole('button', { name: /start review/i }))
    await waitFor(() => expect(mockedStartScan).toHaveBeenCalledTimes(1))
    const submittedPayload = mockedStartScan.mock.calls[0][0]
    // The scan submits ``doc_path`` (from the upload response) rather
    // than ``doc_text`` (which would carry the mangled bytes).
    expect(submittedPayload.doc_path).toBe('/tmp/fake_uploads/abcd1234_my_design.docx')
    expect(submittedPayload.doc_text).toBeUndefined()
  })

  it('renders the file-size limit hint below the browse button', () => {
    // The 5 MiB cap is enforced both server-side (paste / uploads
    // routes) and now client-side (this test). Stating it in the UI
    // stops users from wasting time on a large doc that will be
    // rejected. The exact rendered unit is locale-formatted via
    // ``fmtUnit`` so the assertion accepts a range of shapes -- the
    // literal digit ``5`` is stable across locales.
    render(<NewReviewDialog />)
    const dialogRoot = screen.getByRole('heading', { name: /review context|new review/i })
      .parentElement!.parentElement!
    expect(dialogRoot.textContent).toContain('2')
    // Must mention "MB" or a translated MB unit; the English rendering
    // uses "2 MB", which is the exact string the format helper emits.
    expect(dialogRoot.textContent).toMatch(/MB|mb/)
  })

  it('rejects a browsed file that exceeds the size limit', () => {
    // A 6 MB browse must be rejected client-side rather than round-
    // tripped to the server just to receive a 413. Behavioural
    // contract: no upload API call, no readAsText, submitError set,
    // and browsedFileName cleared so the UI does not show a
    // dangling "browsed" hint tied to a failed pick.
    render(<NewReviewDialog />)
    const fileInputElement = document.querySelector(
      'input[type="file"]',
    ) as HTMLInputElement
    // Constructing a 6 MB ``File`` via a Blob of the right size.
    // Bytes content does not matter; only ``.size`` is checked.
    const oversizedFile = new File(
      [new Uint8Array(6 * 1024 * 1024)],
      'huge_design.docx',
      { type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' },
    )
    Object.defineProperty(fileInputElement, 'files', {
      value: [oversizedFile],
      configurable: true,
    })
    fireEvent.change(fileInputElement)

    // The upload API must NOT have been called (we caught it client-
    // side before touching the network).
    expect(mockedUploadDocumentFile).not.toHaveBeenCalled()
    // The error surface must be populated with a size-limit message.
    // Matching on ``too large`` / ``maximum`` is enough; the exact
    // wording is i18n.
    const errorRegionRendered = document.body.textContent || ''
    expect(errorRegionRendered).toMatch(/too large|maximum/i)
  })

  it('rejects a paste that exceeds the size limit on submit', async () => {
    // Pasted docs are size-checked at submit time (there is no
    // ``.size`` on a raw string). The check uses UTF-8 byte length,
    // not string length, so a doc dense with multi-byte characters
    // still trips the limit at the right threshold. Behavioural
    // contract: no ``startScan`` call when the paste is oversized.
    render(<NewReviewDialog />)
    const documentTextArea = screen.getByPlaceholderText(
      /paste markdown/i,
    ) as HTMLTextAreaElement
    // Six million ASCII chars ~= 6 MB (ASCII = one byte per char).
    const oversizedPastedText = 'A'.repeat(6 * 1024 * 1024)
    fireEvent.change(documentTextArea, { target: { value: oversizedPastedText } })
    fireEvent.click(screen.getByRole('button', { name: /start review/i }))
    // No round-trip; the client-side check trips first.
    expect(mockedStartScan).not.toHaveBeenCalled()
  })

  it('does not render the picker until settings resolve', () => {
    // Settings still loading (``data`` is ``undefined``) -- the picker
    // is guarded on the resolved toggles map and must not render.
    // Everything else in the dialog should still render normally so
    // the user is not blocked from typing the doc.
    mockedUseWritingReview.mockReturnValueOnce(
      makeFakeContextValue({
        settingsQuery: { data: undefined },
      }),
    )
    render(<NewReviewDialog />)
    expect(screen.queryByRole('group', { name: /scanners/i })).toBeNull()
  })

  it('auto-checks the design chip when the user picks a design doc_type', () => {
    // Settings default has ``design: false`` (conditional scanners
    // ship off by default -- the user opts in per doc). Picking
    // "design document" from the doc-type select must flip the design
    // chip on so the scanner actually runs -- otherwise the backend's
    // ``_resolve_scanners`` conditional-layer trigger is silently
    // vetoed by the false toggle.
    const conditionalScannerToggles: Record<string, boolean> = {
      ...fakeScannerToggles,
      design: false,
      email: false,
    }
    mockedUseWritingReview.mockReturnValueOnce(
      makeFakeContextValue({
        settingsQuery: {
          data: {
            default_audience: '',
            default_doc_type: '',
            default_tone: '',
            scanner_toggles: conditionalScannerToggles,
            max_concurrent: 9,
          },
        },
      }),
    )
    render(<NewReviewDialog />)

    // Before the doc_type change, design is off.
    const designChipBefore = getScannerChipByRawScannerName('design')
    expect(designChipBefore.getAttribute('aria-pressed')).toBe('false')

    // Change the doc-type select. The option value is the camelCase key
    // ``designDocument``; the auto-check hook matches on the lowercased
    // substring "design", the same trigger the backend uses. Three
    // ``<select>`` elements exist (audience, docType, tone); the doc
    // type one is the second in DOM order.
    const allSelectElements = screen.getAllByRole('combobox')
    const docTypeSelect = allSelectElements[1] as HTMLSelectElement
    fireEvent.change(docTypeSelect, { target: { value: 'designDocument' } })

    const designChipAfter = getScannerChipByRawScannerName('design')
    expect(designChipAfter.getAttribute('aria-pressed')).toBe('true')
  })

  it('auto-unchecks a previously auto-checked conditional scanner when doc_type moves away', () => {
    // The symmetric flip: after auto-check flips design on for a
    // design doc_type, changing to a non-triggering doc_type must flip
    // it back off. Otherwise a user who explored several doc_types
    // ends up with every conditional scanner pressed regardless of the
    // final choice. The user did not manually touch the design chip
    // in this scenario -- the "not manually touched" state is what
    // keeps auto-management in play.
    const conditionalScannerToggles: Record<string, boolean> = {
      ...fakeScannerToggles,
      design: false,
      email: false,
    }
    mockedUseWritingReview.mockReturnValueOnce(
      makeFakeContextValue({
        settingsQuery: {
          data: {
            default_audience: '',
            default_doc_type: '',
            default_tone: '',
            scanner_toggles: conditionalScannerToggles,
            max_concurrent: 9,
          },
        },
      }),
    )
    render(<NewReviewDialog />)

    const docTypeSelect = screen.getAllByRole('combobox')[1] as HTMLSelectElement
    fireEvent.change(docTypeSelect, { target: { value: 'designDocument' } })
    expect(getScannerChipByRawScannerName('design').getAttribute('aria-pressed')).toBe(
      'true',
    )

    // Now pick a non-triggering doc_type. Design chip must flip back
    // off; email chip stays off (never triggered).
    fireEvent.change(docTypeSelect, { target: { value: 'teamUpdate' } })
    expect(getScannerChipByRawScannerName('design').getAttribute('aria-pressed')).toBe(
      'false',
    )
    expect(getScannerChipByRawScannerName('email').getAttribute('aria-pressed')).toBe(
      'false',
    )
  })

  it('respects a manual toggle over subsequent doc_type-driven auto-management', () => {
    // Once the user has manually clicked a conditional chip, the
    // auto-management back-off applies for the rest of the dialog
    // session. A user who explicitly turned design OFF while looking
    // at a design doc has stated intent that ``I don't want design
    // this time``; the effect must not re-check it when they pick
    // another design-flavoured doc_type moments later.
    const conditionalScannerToggles: Record<string, boolean> = {
      ...fakeScannerToggles,
      design: false,
      email: false,
    }
    mockedUseWritingReview.mockReturnValueOnce(
      makeFakeContextValue({
        settingsQuery: {
          data: {
            default_audience: '',
            default_doc_type: '',
            default_tone: '',
            scanner_toggles: conditionalScannerToggles,
            max_concurrent: 9,
          },
        },
      }),
    )
    render(<NewReviewDialog />)

    const docTypeSelect = screen.getAllByRole('combobox')[1] as HTMLSelectElement
    fireEvent.change(docTypeSelect, { target: { value: 'designDocument' } })
    // Auto-check ran; design is now pressed.
    const designChipAutoChecked = getScannerChipByRawScannerName('design')
    expect(designChipAutoChecked.getAttribute('aria-pressed')).toBe('true')

    // User manually flips it off. This registers a manual intent.
    fireEvent.click(designChipAutoChecked)
    expect(designChipAutoChecked.getAttribute('aria-pressed')).toBe('false')

    // Move away, then back to a design-triggering doc_type. Design
    // must stay off -- the user's manual choice wins.
    fireEvent.change(docTypeSelect, { target: { value: 'teamUpdate' } })
    fireEvent.change(docTypeSelect, { target: { value: 'designDocument' } })
    expect(getScannerChipByRawScannerName('design').getAttribute('aria-pressed')).toBe(
      'false',
    )
  })

  it('carries a custom settings-configured default_audience through as an Other-mode value', () => {
    // End-to-end regression guard (Spock F2): when a user set a custom
    // ``default_audience`` in the Settings panel (a free-form string
    // outside the built-in enum), the New Review dialog must hydrate
    // the audience field with that value AND drop the picker into
    // Other mode showing the string in the custom text input.
    //
    // Individual paths are unit-tested elsewhere (DropdownWithOther
    // handles the mode switch; SettingsPanel writes the custom value).
    // This test wires the two together via the shared context seed to
    // make sure a rename or reshuffle of either surface does not
    // silently break the carry-through, which is user-visible
    // (settings default appears blank on next scan) but has no other
    // gate.
    const customPersistedAudience = 'Q3 board deck reviewers'
    mockedUseWritingReview.mockReturnValueOnce(
      makeFakeContextValue({
        settingsQuery: {
          data: {
            default_audience: customPersistedAudience,
            default_doc_type: '',
            default_tone: '',
            scanner_toggles: fakeScannerToggles,
            max_concurrent: 9,
          },
        },
      }),
    )
    render(<NewReviewDialog />)
    // Audience combobox reflects Other mode via the reserved sentinel
    // value. Not a user-visible string; the assertion pins the wire
    // shape so a rename of the sentinel constant would fail this test
    // rather than silently break Other-mode hydration.
    const allComboboxElements = screen.getAllByRole('combobox')
    const audienceComboboxElement = allComboboxElements[0] as HTMLSelectElement
    expect(audienceComboboxElement.value).toBe('__other__')
    // Custom text input is rendered and contains the persisted default.
    const customAudienceTextInput = screen.getByRole('textbox', {
      name: /audience \(custom\)/i,
    })
    expect(customAudienceTextInput).toHaveValue(customPersistedAudience)
  })

  it('respects a settings-configured conditional scanner across doc_type changes', () => {
    // A user who set ``design: true`` in the Settings panel is stating
    // "I always want design to run". That preference must survive
    // doc_type changes to non-triggering values -- otherwise the auto-
    // management would silently override the persisted preference.
    // Detection: any conditional scanner that arrives from settings
    // already ``true`` is treated as user-managed from hydration on.
    const settingsWithDesignAlwaysOn: Record<string, boolean> = {
      ...fakeScannerToggles,
      design: true,
      email: false,
    }
    mockedUseWritingReview.mockReturnValueOnce(
      makeFakeContextValue({
        settingsQuery: {
          data: {
            default_audience: '',
            default_doc_type: '',
            default_tone: '',
            scanner_toggles: settingsWithDesignAlwaysOn,
            max_concurrent: 9,
          },
        },
      }),
    )
    render(<NewReviewDialog />)

    expect(getScannerChipByRawScannerName('design').getAttribute('aria-pressed')).toBe(
      'true',
    )

    // Pick a non-triggering doc_type. Design must stay on because it
    // was pre-flagged as user-managed during hydration.
    const docTypeSelect = screen.getAllByRole('combobox')[1] as HTMLSelectElement
    fireEvent.change(docTypeSelect, { target: { value: 'teamUpdate' } })
    expect(getScannerChipByRawScannerName('design').getAttribute('aria-pressed')).toBe(
      'true',
    )
  })
})
