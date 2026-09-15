import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { columnLetter, detectFileType, HtmlViewer, JsonlViewer, OfficeViewer, SheetViewer } from '../components/FileRenderers'
import { consumeChatHandoff, installSoftNavigate, __resetNavSeamForTests } from '../utils/errorReport'

// `useCanOpenFile` reads both of these, and it is the gate deciding whether the
// Open button exists at all. Drive them explicitly: on the test host they would
// otherwise resolve from a live /api/branding call.
const brandingEnv = vi.hoisted(() => ({ directLocal: true }))
const platformEnv = vi.hoisted(() => ({ value: 'other' as 'other' | 'darwin' | 'windows' }))

vi.mock('../hooks/useBranding', () => ({
  useBranding: () => ({ botName: 'Test', avatar: '', directLocal: brandingEnv.directLocal }),
}))

vi.mock('../hooks/useGatewayPlatform', () => ({
  useGatewayPlatform: () => platformEnv.value,
}))

// Only `revealPath` is stubbed: the other viewers in this file talk to the
// server through `fetch`, so a partial mock keeps their paths real.
vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return { ...actual, api: { ...actual.api, revealPath: vi.fn() } }
})

// Resolves TRUE by default: the real signature is `Promise<boolean>` and a bare
// `vi.fn()` returns undefined, which would read as a FAILED clipboard write.
vi.mock('../utils/clipboard', () => ({
  copyToClipboard: vi.fn().mockResolvedValue(true),
}))

import { api } from '../api/client'
import { copyToClipboard } from '../utils/clipboard'
import { ApiError } from '../api/apiError'

describe('detectFileType', () => {
  it('returns jsonl for .jsonl files', () => {
    expect(detectFileType('data.jsonl')).toBe('jsonl')
    expect(detectFileType('/path/to/session.jsonl')).toBe('jsonl')
  })

  it('returns json for .json files (not jsonl)', () => {
    expect(detectFileType('config.json')).toBe('json')
  })

  it('returns sheet for OOXML spreadsheets (inline preview via /api/file-sheet)', () => {
    expect(detectFileType('workbook.xlsx')).toBe('sheet')
    expect(detectFileType('macros.xlsm')).toBe('sheet')
    // Case-insensitive on extension.
    expect(detectFileType('/tmp/NVDA_DCF_Model.XLSX')).toBe('sheet')
  })

  it('returns office for OOXML and legacy Office extensions', () => {
    // OOXML (ZIP-based) — the specific formats that motivated this fix.
    expect(detectFileType('report.docx')).toBe('office')
    expect(detectFileType('deck.pptx')).toBe('office')
    // Legacy OLE compound files. .xls stays here: openpyxl reads OOXML only,
    // so legacy spreadsheets keep the download card instead of a broken grid.
    expect(detectFileType('old.doc')).toBe('office')
    expect(detectFileType('old.xls')).toBe('office')
    expect(detectFileType('old.ppt')).toBe('office')
    // OpenDocument formats — including .ods, which openpyxl cannot parse.
    expect(detectFileType('doc.odt')).toBe('office')
    expect(detectFileType('sheet.ods')).toBe('office')
    expect(detectFileType('slides.odp')).toBe('office')
    // Case-insensitive on extension.
    expect(detectFileType('/tmp/quarterly-report.DOCX')).toBe('office')
  })

  it('keeps pdf routed to pdf (not office) so browser inline preview still works', () => {
    // .pdf has its own PdfViewer that iframes /api/file-raw. It must NOT be
    // reclassified as 'office' or the download-only card would replace the
    // working inline preview.
    expect(detectFileType('paper.pdf')).toBe('pdf')
  })
})

describe('JsonlViewer', () => {
  it('renders line count and initial page of lines', () => {
    const content = '{"a":1}\n{"b":2}\n{"c":3}\n'
    render(<JsonlViewer content={content} />)
    expect(screen.getByText('3 lines')).toBeInTheDocument()
  })

  it('shows remaining count when more lines exist than page size', () => {
    const lines = Array.from({ length: 150 }, (_, i) => JSON.stringify({ i }))
    render(<JsonlViewer content={lines.join('\n')} />)
    expect(screen.getByText('150 lines')).toBeInTheDocument()
    expect(screen.getByText(/50 remaining/)).toBeInTheDocument()
  })

  it('skips empty lines', () => {
    const content = '{"a":1}\n\n\n{"b":2}\n'
    render(<JsonlViewer content={content} />)
    expect(screen.getByText('2 lines')).toBeInTheDocument()
  })
})

describe('HtmlViewer', () => {
  it('gives the preview frame its own compositing layer so a skipped first paint cannot blank it', () => {
    const { container } = render(<HtmlViewer content="<p>preview</p>" />)
    const iframe = container.querySelector('iframe') as HTMLIFrameElement
    expect(iframe).not.toBeNull()
    expect(iframe.style.transform).toBe('translateZ(0)')
    // The isolation contract must survive the style change: an empty sandbox
    // is what keeps the srcDoc document inert.
    expect(iframe.getAttribute('sandbox')).toBe('')
  })
})

/** OfficeViewer (and SheetViewer's fallback card, which renders it) fetch via
 *  React Query, so renders need a QueryClientProvider. Fresh client per render
 *  keeps the per-filePath query cache from leaking between tests; retry
 *  disabled so error paths settle in one pass. */
function renderWithQuery(ui: ReactNode) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return render(<QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>)
}

describe('OfficeViewer', () => {
  const realFetch = globalThis.fetch


  /** Stub /api/file-office-preview with a Response-shaped object. Mirrors the
   *  pattern used in MarkdownRenderer.test.tsx for the file-read HEAD probe. */
  function stubPreview(body: { text?: string; truncated?: boolean; error?: string; slides?: { index: number; text: string }[] } | null, ok = true, status = 200) {
    globalThis.fetch = vi.fn(() =>
      Promise.resolve({
        ok,
        status,
        json: () => Promise.resolve(body ?? {}),
      } as unknown as Response),
    ) as unknown as typeof fetch
  }

  /** Route by endpoint: the slide renderer asks /api/file-office-slides first
   *  and the text viewer /api/file-office-preview; a deck exercises both. */
  function stubByUrl(routes: Record<string, { body?: unknown; ok?: boolean; status?: number }>) {
    globalThis.fetch = vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      const key = Object.keys(routes).find(k => url.includes(k))
      const route = key ? routes[key] : { ok: false, status: 404 }
      return Promise.resolve({
        ok: route.ok ?? true,
        status: route.status ?? (route.ok === false ? 500 : 200),
        json: () => Promise.resolve(route.body ?? {}),
      } as unknown as Response)
    }) as unknown as typeof fetch
  }

  afterEach(() => {
    globalThis.fetch = realFetch
    vi.restoreAllMocks()
  })

  it('renders the plaintext preview when /api/file-office-preview returns text', async () => {
    // The component's decision about which UI state to render is driven
    // entirely by (ok, body.text) — matches the backend contract
    // in `api_file_office_preview`.
    stubPreview({
      text: 'Introduction\n\nThis is the first paragraph of the document.',
      truncated: false,
    })
    renderWithQuery(<OfficeViewer filePath="/home/user/docs/quarterly-report.docx" />)
    await waitFor(() => {
      expect(screen.getByText(/Introduction/)).toBeInTheDocument()
    })
    expect(screen.getByText(/first paragraph/)).toBeInTheDocument()
    // Compact "Download original" affordance is present beneath the preview,
    // not the full-size button — this is the preview state.
    expect(screen.getByRole('link', { name: /quarterly-report\.docx/i })).toBeInTheDocument()
    expect(screen.getByText('Download original')).toBeInTheDocument()
  })

  it('renders a .pptx as a slide outline when the host has no LibreOffice, naming the install command', async () => {
    // Without LibreOffice on the machine the gateway runs on, the slides endpoint answers
    // `unavailable`; the deck then renders as its text outline UNDER a row that
    // says why and shows the gateway's own install hint. A deck shown as text
    // with no explanation reads as "the panel failed to render my slides".
    stubByUrl({
      '/api/file-office-slides': { body: { status: 'unavailable', reason: 'soffice_unavailable', hint: 'sudo apt install libreoffice' } },
      '/api/file-office-preview': {
        body: {
          text: '--- Slide 1 ---\nRoadmap\nQ3 goals\n\n--- Slide 3 ---\nRisks',
          truncated: false,
          slides: [
            { index: 1, text: 'Roadmap\nQ3 goals' },
            { index: 3, text: 'Risks' },
          ],
        },
      },
    })
    renderWithQuery(<OfficeViewer filePath="/home/user/decks/roadmap.pptx" />)
    await waitFor(() => {
      expect(screen.getByTestId('office-slide-outline')).toBeInTheDocument()
    })
    const degrade = screen.getByTestId('office-slides-degrade')
    expect(degrade).toHaveTextContent(/need LibreOffice on the machine Kiro Crew runs on/)
    expect(degrade).toHaveTextContent('sudo apt install libreoffice')
    expect(screen.queryByTestId('office-slides')).toBeNull()
    // The remedy is a hand-off, not only a command to copy: the button stages a
    // prompt naming the gateway's own install command and opens the chat. The
    // user reads and sends it; nothing runs on the click itself.
    const navigated: string[] = []
    installSoftNavigate(to => { navigated.push(to) })
    try {
      fireEvent.click(screen.getByRole('button', { name: /ask the agent to install it/i }))
      const prompt = consumeChatHandoff()
      expect(prompt).toContain('LibreOffice')
      expect(prompt).toContain('sudo apt install libreoffice')
      expect(navigated).toEqual(['/chat'])
    } finally {
      __resetNavSeamForTests()
    }
    expect(screen.getByText(/Text outline only/)).toBeInTheDocument()
    // Deck numbering, not a renumbering: slide 2 carried no text.
    expect(screen.getByRole('region', { name: 'Slide 1' })).toHaveTextContent('Roadmap')
    expect(screen.getByRole('region', { name: 'Slide 3' })).toHaveTextContent('Risks')
    expect(screen.queryByRole('region', { name: 'Slide 2' })).toBeNull()
    // The flat separator string is NOT what renders — it would duplicate the headings.
    expect(screen.queryByText(/--- Slide 1 ---/)).toBeNull()
    expect(screen.getByText('Download original')).toBeInTheDocument()
  })

  it('says why a deck the render refused on content grounds shows only its redacted outline', async () => {
    // A slide is a picture of the deck's text and cannot be redacted after the
    // fact, so a credential-bearing deck answers `unavailable` with
    // `content_redacted`. The row names that reason -- not a missing LibreOffice
    // -- and offers no install hand-off, because installing anything would not
    // change the answer.
    stubByUrl({
      '/api/file-office-slides': { body: { status: 'unavailable', reason: 'content_redacted' } },
      '/api/file-office-preview': {
        body: { text: '--- Slide 1 ---\nKey: [REDACTED: credential]', slides: [{ index: 1, text: 'Key: [REDACTED: credential]' }] },
      },
    })
    renderWithQuery(<OfficeViewer filePath="/home/user/decks/secrets.pptx" />)
    await waitFor(() => {
      expect(screen.getByTestId('office-slide-outline')).toBeInTheDocument()
    })
    const degrade = screen.getByTestId('office-slides-degrade')
    expect(degrade).toHaveTextContent(/looks like a credential/)
    expect(degrade).not.toHaveTextContent(/LibreOffice/)
    expect(screen.queryByTestId('office-slides-ask-install')).toBeNull()
    expect(screen.queryByTestId('office-slides')).toBeNull()
  })

  it('renders a .pptx as pictures with a pager when the host rendered it', async () => {
    stubByUrl({
      '/api/file-office-slides': {
        body: {
          status: 'ready', digest: 'a'.repeat(64), count: 3, truncated: false,
          slides: [
            { n: 1, width: 1280, height: 720 },
            { n: 2, width: 1280, height: 720 },
            { n: 3, width: 1280, height: 720 },
          ],
        },
      },
    })
    renderWithQuery(<OfficeViewer filePath="/home/user/decks/roadmap.pptx" />)
    await waitFor(() => {
      expect(screen.getByTestId('office-slides')).toBeInTheDocument()
    })
    // Once the slides are ready the outline that stood in while they rendered
    // is gone, and so is the "Rendering slides" row: the deck is shown as slides.
    expect(screen.queryByTestId('office-slide-outline')).toBeNull()
    expect(screen.queryByTestId('office-slides-degrade')).toBeNull()
    // Stage shows slide 1 from the slide endpoint, with the deck's numbering.
    const stage = screen.getByRole('img', { name: 'Slide 1 of 3' })
    expect(stage).toHaveAttribute('src', expect.stringContaining('/api/file-office-slide?path='))
    expect(stage).toHaveAttribute('src', expect.stringContaining('&n=1'))
    // Pinned to the manifest's digest, so an edited deck never gets a browser-cached slide.
    expect(stage).toHaveAttribute('src', expect.stringContaining('&digest=' + 'a'.repeat(64)))
    expect(screen.getByRole('button', { name: 'Previous slide' })).toBeDisabled()
    // Next pages forward; the thumbstrip tracks the selection.
    fireEvent.click(screen.getByRole('button', { name: 'Next slide' }))
    expect(screen.getByRole('img', { name: 'Slide 2 of 3' })).toHaveAttribute('src', expect.stringContaining('&n=2'))
    expect(screen.getByRole('option', { name: 'Slide 2 of 3' })).toHaveAttribute('aria-selected', 'true')
    // A thumbnail jumps straight to its slide, and the last slide disables Next.
    fireEvent.click(screen.getByRole('option', { name: 'Slide 3 of 3' }))
    expect(screen.getByRole('img', { name: 'Slide 3 of 3' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Next slide' })).toBeDisabled()
    // Keyboard paging works from inside the pager.
    fireEvent.keyDown(screen.getByTestId('office-slides'), { key: 'Home' })
    expect(screen.getByRole('img', { name: 'Slide 1 of 3' })).toBeInTheDocument()
    fireEvent.keyDown(screen.getByTestId('office-slides'), { key: 'ArrowRight' })
    expect(screen.getByRole('img', { name: 'Slide 2 of 3' })).toBeInTheDocument()
    // The card's actions stay reachable beneath the pager.
    expect(screen.getByText('Download original')).toBeInTheDocument()
  })

  it('keeps a failed Open on the compact card in its own row above the two file actions', async () => {
    // Inside the pager footer the card is compact. A failed Open renders the
    // shared notice (its own Ask the agent and dismiss controls) as a row of
    // its own, so the row with Open and Download never carries more than two.
    stubByUrl({
      '/api/file-office-slides': {
        body: { status: 'ready', digest: 'c'.repeat(64), count: 1, truncated: false, slides: [{ n: 1, width: 1280, height: 720 }] },
      },
    })
    vi.mocked(api.revealPath).mockRejectedValue(new ApiError(500, 'boom'))
    renderWithQuery(<OfficeViewer filePath="/home/user/decks/roadmap.pptx" />)
    await waitFor(() => {
      expect(screen.getByTestId('office-slides')).toBeInTheDocument()
    })
    const open = screen.getByRole('button', { name: /open with default app/i })
    fireEvent.click(open)
    const notice = await screen.findByTestId('office-card-open-error')
    expect(notice.parentElement).not.toBe(open.parentElement)
    expect(open.parentElement?.querySelectorAll('button, a')).toHaveLength(2)
  })

  it('replaces a slide that fails to load with an ErrorNotice and a way to render again', async () => {
    // The slide endpoint answers 409 after an edit and 404 after eviction; the
    // <img> only knows it failed. A broken image is a dead end, so the stage
    // shows the shared notice (agent hand-off included) and a button that asks
    // for the manifest again -- a changed deck gets its new digest, an evicted
    // one is rendered again.
    stubByUrl({
      '/api/file-office-slides': {
        body: { status: 'ready', digest: 'b'.repeat(64), count: 2, truncated: false, slides: [{ n: 1, width: 1280, height: 720 }, { n: 2, width: 1280, height: 720 }] },
      },
    })
    renderWithQuery(<OfficeViewer filePath="/home/user/decks/roadmap.pptx" />)
    await waitFor(() => {
      expect(screen.getByTestId('office-slides')).toBeInTheDocument()
    })
    const before = (globalThis.fetch as unknown as ReturnType<typeof vi.fn>).mock.calls.filter(c => String(c[0]).includes('/api/file-office-slides')).length
    fireEvent.error(screen.getByRole('img', { name: 'Slide 1 of 2' }))
    expect(screen.getByTestId('office-slide-load-error')).toHaveTextContent(/Slide 1 did not load/)
    expect(screen.getByRole('button', { name: /ask the agent/i })).toBeInTheDocument()
    // Paging away still works: slide 2 is untouched by slide 1's failure.
    fireEvent.click(screen.getByRole('button', { name: 'Next slide' }))
    expect(screen.getByRole('img', { name: 'Slide 2 of 2' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Previous slide' }))
    fireEvent.click(screen.getByRole('button', { name: 'Render slides again' }))
    await waitFor(() => {
      const after = (globalThis.fetch as unknown as ReturnType<typeof vi.fn>).mock.calls.filter(c => String(c[0]).includes('/api/file-office-slides')).length
      expect(after).toBeGreaterThan(before)
    })
  })

  it('falls back to the text outline with an ErrorNotice carrying the server reason when rendering errors', async () => {
    stubByUrl({
      '/api/file-office-slides': { ok: false, status: 502, body: { error: 'could not render slides', code: 'convert_failed', detail: 'soffice exited 77' } },
      '/api/file-office-preview': { body: { text: '--- Slide 1 ---\nOnly text', slides: [{ index: 1, text: 'Only text' }] } },
    })
    renderWithQuery(<OfficeViewer filePath="/home/user/decks/broken.pptx" />)
    await waitFor(() => {
      expect(screen.getByTestId('office-slide-outline')).toBeInTheDocument()
    })
    // A failed render is an error: the shared notice, with the server's reason
    // and code preserved, and the agent hand-off beside it.
    const notice = screen.getByTestId('office-slides-render-error')
    expect(notice).toHaveTextContent(/could not be rendered/)
    expect(notice).toHaveTextContent('could not render slides: soffice exited 77 (convert_failed)')
  })

  it('shows the text outline under a rendering row while the first render runs', async () => {
    // The manifest fetch never resolves here: the panel must not sit blank on
    // a spinner for the ~30 s a cold conversion takes when the outline is instant.
    globalThis.fetch = vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/api/file-office-slides')) return new Promise<Response>(() => {})
      return Promise.resolve({
        ok: true, status: 200,
        json: () => Promise.resolve({ text: '--- Slide 1 ---\nInstant text', slides: [{ index: 1, text: 'Instant text' }] }),
      } as unknown as Response)
    }) as unknown as typeof fetch
    renderWithQuery(<OfficeViewer filePath="/home/user/decks/slow.pptx" />)
    await waitFor(() => {
      expect(screen.getByTestId('office-slide-outline')).toBeInTheDocument()
    })
    expect(screen.getByTestId('office-slides-degrade')).toHaveTextContent(/Rendering slides/)
    expect(screen.getByText('Instant text')).toBeInTheDocument()
  })

  it('shows the download card under the degrade row for a legacy .ppt without LibreOffice', async () => {
    // .ppt has no XML for the text outline (415 from the preview endpoint is
    // short-circuited client-side), so without soffice the card is all there
    // is -- but the row still says what would make slides appear.
    stubByUrl({
      '/api/file-office-slides': { body: { status: 'unavailable', reason: 'soffice_unavailable', hint: 'brew install --cask libreoffice' } },
    })
    renderWithQuery(<OfficeViewer filePath="/home/user/decks/legacy.ppt" />)
    await waitFor(() => {
      expect(screen.getByTestId('office-slides-degrade')).toBeInTheDocument()
    })
    expect(screen.getByText('legacy.ppt')).toBeInTheDocument()
    expect(screen.getByText('Download')).toBeInTheDocument()
  })

  it('does not ask the slides endpoint for a .docx', async () => {
    stubByUrl({ '/api/file-office-preview': { body: { text: 'Introduction', truncated: false } } })
    renderWithQuery(<OfficeViewer filePath="/home/user/docs/report.docx" />)
    await waitFor(() => {
      expect(screen.getByText('Introduction')).toBeInTheDocument()
    })
    const urls = (globalThis.fetch as unknown as ReturnType<typeof vi.fn>).mock.calls.map(c => String(c[0]))
    expect(urls.some(u => u.includes('/api/file-office-slides'))).toBe(false)
    expect(screen.queryByTestId('office-slides-degrade')).toBeNull()
  })

  it('keeps the flat text preview when the response carries no slides', async () => {
    // The .docx path, and any backend that predates `slides`: same body,
    // same <pre>, no notice about slides.
    stubPreview({ text: 'Introduction\nBody', truncated: false })
    renderWithQuery(<OfficeViewer filePath="/home/user/docs/report.docx" />)
    await waitFor(() => {
      expect(screen.getByText(/Introduction/)).toBeInTheDocument()
    })
    expect(screen.queryByTestId('office-slide-outline')).toBeNull()
    expect(screen.queryByText(/Text outline only/)).toBeNull()
  })

  it('makes the preview scroll container keyboard-focusable', async () => {
    // Long documents must stay readable past the fold without a pointer —
    // the scroll region carries tabIndex=0 and an accessible name.
    stubPreview({ text: 'Some document text', truncated: false })
    renderWithQuery(<OfficeViewer filePath="/home/user/docs/quarterly-report.docx" />)
    await waitFor(() => {
      expect(screen.getByText('Some document text')).toBeInTheDocument()
    })
    const region = screen.getByRole('region', { name: 'quarterly-report.docx' })
    expect(region).toHaveAttribute('tabindex', '0')
  })

  it('falls back to the download card when /api/file-office-preview returns 415', async () => {
    // Server-side safety net: if the backend rejects a nominally previewable
    // extension (list drift, direct API), the component MUST render the
    // full-size download card — never block the user from getting the file.
    stubPreview({ error: 'unsupported format for inline preview' }, false, 415)
    renderWithQuery(<OfficeViewer filePath="/home/user/reports/report.docx" />)
    await waitFor(() => {
      expect(screen.getByText('report.docx')).toBeInTheDocument()
    })
    expect(screen.getByText('Download')).toBeInTheDocument()
    expect(screen.getByText(/Preview isn't available for this file/i)).toBeInTheDocument()
  })

  it('renders the download card for never-previewable extensions without fetching', async () => {
    // Known-unsupported formats (.xls/.doc/.odt…) short-circuit client-side:
    // no fetch, no "Loading preview…" flash for a guaranteed 415.
    const fetchSpy = vi.fn()
    globalThis.fetch = fetchSpy as unknown as typeof fetch
    renderWithQuery(<OfficeViewer filePath="/home/user/reports/legacy.xls" />)
    await waitFor(() => {
      expect(screen.getByText('legacy.xls')).toBeInTheDocument()
    })
    expect(screen.getByText('Download')).toBeInTheDocument()
    expect(fetchSpy).not.toHaveBeenCalled()
  })

  it('falls back to the download card when the fetch itself throws', async () => {
    globalThis.fetch = vi.fn(() => Promise.reject(new TypeError('Failed to fetch'))) as unknown as typeof fetch
    renderWithQuery(<OfficeViewer filePath="/home/user/docs/quarterly-report.docx" />)
    await waitFor(() => {
      expect(screen.getByText('quarterly-report.docx')).toBeInTheDocument()
    })
    expect(screen.getByText('Download')).toBeInTheDocument()
  })

  it('falls back to the download card when extraction returns empty text', async () => {
    // doc_parser returns "" for both a blank document and a parse failure —
    // the frontend treats empty text as "no preview" and shows the card.
    stubPreview({ text: '', truncated: false })
    renderWithQuery(<OfficeViewer filePath="/home/user/docs/blank.docx" />)
    await waitFor(() => {
      expect(screen.getByText('blank.docx')).toBeInTheDocument()
    })
    expect(screen.getByText('Download')).toBeInTheDocument()
  })

  it('renders the truncation notice in the pinned footer when the backend flags truncation', async () => {
    stubPreview({
      text: 'A very long document that would keep going...',
      truncated: true,
    })
    renderWithQuery(<OfficeViewer filePath="/home/user/docs/huge-report.docx" />)
    await waitFor(() => {
      expect(screen.getByText(/Preview shows only the beginning/i)).toBeInTheDocument()
    })
  })

  it('extracts the basename from a Windows path with backslash separators', async () => {
    // Kiro Crew ships native on Windows where filePath arrives as
    // C:\Users\...\report.docx. A `/`-only split would surface the whole
    // path — split on BOTH separators to match MarkdownRenderer/VectorMemoryCard.
    stubPreview({}, false, 415)  // force fallback so the download card is visible
    renderWithQuery(<OfficeViewer filePath="C:\\Users\\harpreet\\Documents\\report.docx" />)
    await waitFor(() => {
      expect(screen.getByText('report.docx')).toBeInTheDocument()
    })
    expect(screen.queryByText(/C:\\Users/)).not.toBeInTheDocument()
  })

  describe('open with default app', () => {
    const revealPath = vi.mocked(api.revealPath)
    const copy = vi.mocked(copyToClipboard)
    let alertSpy: ReturnType<typeof vi.spyOn>

    beforeEach(() => {
      revealPath.mockReset()
      copy.mockReset()
      copy.mockResolvedValue(true)
      brandingEnv.directLocal = true
      platformEnv.value = 'other'
      alertSpy = vi.spyOn(window, 'alert').mockImplementation(() => {})
    })

    it('leads the download card with Open, and keeps Download beside it', async () => {
      // Card state: the format has no preview, so the file can only be reached
      // by handing it to its application or by downloading a copy.
      stubPreview({}, false, 415)
      revealPath.mockResolvedValue({ ok: true })
      renderWithQuery(<OfficeViewer filePath="/home/user/docs/legacy.doc" />)
      const open = await screen.findByRole('button', { name: /open with default app/i })
      fireEvent.click(open)
      // The EXISTING file, not a second copy in ~/Downloads.
      await waitFor(() => expect(revealPath).toHaveBeenCalledWith('/home/user/docs/legacy.doc', 'open'))
      // Download stays available for the user who wants their own copy anyway.
      expect(screen.getByRole('link', { name: /legacy\.doc/i })).toBeInTheDocument()
    })

    it('offers the same action under the plaintext preview', async () => {
      // Preview state: the extracted text is not the document — its formatting,
      // images and layout are only in the real file.
      stubPreview({ text: 'Introduction\n\nBody.', truncated: false })
      revealPath.mockResolvedValue({ ok: true })
      renderWithQuery(<OfficeViewer filePath="/home/user/docs/quarterly-report.docx" />)
      fireEvent.click(await screen.findByRole('button', { name: /open with default app/i }))
      await waitFor(() => expect(revealPath).toHaveBeenCalledWith('/home/user/docs/quarterly-report.docx', 'open'))
      expect(screen.getByText(/Download original/i)).toBeInTheDocument()
    })

    it('actually writes the clipboard when the gateway degrades to a copy', async () => {
      // The regression this locks: announcing "path copied" while the clipboard
      // is untouched makes the user paste whatever was there before. Only
      // `revealOrOpen` performs the write, so the button must route through it
      // rather than calling `api.revealPath` (side-effect-free) on its own.
      stubPreview({}, false, 415)
      revealPath.mockResolvedValue({ ok: true, copy: '/srv/agent/report.doc' })
      renderWithQuery(<OfficeViewer filePath="/srv/agent/report.doc" />)
      fireEvent.click(await screen.findByRole('button', { name: /open with default app/i }))
      await waitFor(() => expect(copy).toHaveBeenCalledWith('/srv/agent/report.doc'))
    })

    it('acknowledges the degrade inline so the button is not a dead click', async () => {
      // Same host as above: the open silently became a clipboard copy. Without
      // the swap the primary control looks like it did nothing at all.
      stubPreview({}, false, 415)
      revealPath.mockResolvedValue({ ok: true, copy: '/srv/agent/report.doc' })
      renderWithQuery(<OfficeViewer filePath="/srv/agent/report.doc" />)
      fireEvent.click(await screen.findByRole('button', { name: /open with default app/i }))
      // The shared useCopyAck swap, the same wording the file-path menu shows.
      await screen.findByRole('button', { name: /path copied/i })
    })

    it('leads the hint with Open when Open is the action', async () => {
      // The hint is the card's only instruction: pointing a local user at
      // Download points them at the duplicate-file divergence this card exists
      // to avoid.
      stubPreview({}, false, 415)
      renderWithQuery(<OfficeViewer filePath="/home/user/docs/legacy.doc" />)
      expect(await screen.findByText(/Open it in its app/i)).toBeInTheDocument()
      expect(screen.queryByText(/Download to open in Word/i)).not.toBeInTheDocument()
    })

    it('keeps the Download wording on a remote session, where Download is the action', async () => {
      brandingEnv.directLocal = false
      stubPreview({}, false, 415)
      renderWithQuery(<OfficeViewer filePath="/srv/agent/report.doc" />)
      expect(await screen.findByText(/Download to open in Word/i)).toBeInTheDocument()
      expect(screen.queryByText(/Open it in its app/i)).not.toBeInTheDocument()
    })

    it('translates a policy refusal instead of leaking the server string', async () => {
      // A sensitive path (SEL guard) answers 403. Raw backend prose is English
      // on a 13-locale UI, so the shared funnel maps it to a catalog key.
      stubPreview({}, false, 415)
      revealPath.mockRejectedValue(new ApiError(403, 'access denied to /home/user/private'))
      renderWithQuery(<OfficeViewer filePath="/home/user/private/notes.doc" />)
      fireEvent.click(await screen.findByRole('button', { name: /open with default app/i }))
      // Rendered in place on the card through the shared ErrorNotice (the
      // card has a render context, so no blocking alert()); the raw server
      // prose never reaches it.
      const notice = await screen.findByTestId('office-card-open-error')
      expect(notice).toHaveAttribute('role', 'alert')
      expect(notice).toHaveTextContent(/protected/i)
      expect(notice).not.toHaveTextContent('access denied')
      expect(alertSpy).not.toHaveBeenCalled()
    })

    it('hides Open on a remote session and promotes Download instead', async () => {
      // No desktop to open on: the document would open on the gateway machine,
      // which nobody is looking at. Download is the only action that works, so
      // it takes the primary styling back rather than sitting beside a dead
      // button.
      brandingEnv.directLocal = false
      stubPreview({}, false, 415)
      renderWithQuery(<OfficeViewer filePath="/srv/agent/report.doc" />)
      const download = await screen.findByRole('link', { name: /report\.doc/i })
      expect(download.className).toContain('bg-accent')
      expect(screen.queryByRole('button', { name: /open with default app/i })).not.toBeInTheDocument()
    })

    it('hides Open when the gateway runs Windows', async () => {
      // files.py degrades an `open` to a clipboard copy there, so every other
      // Open surface suppresses the row; this card must not be the exception.
      platformEnv.value = 'windows'
      stubPreview({}, false, 415)
      renderWithQuery(<OfficeViewer filePath="C:\\Users\\dev\\report.doc" />)
      await screen.findByRole('link', { name: /report\.doc/i })
      expect(screen.queryByRole('button', { name: /open with default app/i })).not.toBeInTheDocument()
    })
  })
})

describe('SheetViewer', () => {
  const payload = {
    sheets: [
      {
        name: 'DCF',
        rows: [['Revenue', 1000, '=B1*1.1'], ['Margin', 0.42, null]],
        truncated_rows: false,
        truncated_cols: false,
      },
      {
        name: 'Assumptions',
        rows: [['WACC', 0.09]],
        truncated_rows: true,
        truncated_cols: false,
      },
    ],
    total_sheets: 2,
    truncated_sheets: false,
  }

  afterEach(() => { vi.unstubAllGlobals() })

  const stubFetch = (impl: () => Promise<unknown>) => {
    vi.stubGlobal('fetch', vi.fn(impl))
  }

  it('renders the first sheet as a grid with column letters and row numbers', async () => {
    stubFetch(async () => ({ ok: true, json: async () => payload }))
    render(<SheetViewer filePath="/ws/outbox/model.xlsx" />)
    expect(await screen.findByText('Revenue')).toBeInTheDocument()
    expect(screen.getByText('1000')).toBeInTheDocument()
    // Column-letter header and row-number gutter make it read as a spreadsheet.
    expect(screen.getByRole('columnheader', { name: 'A' })).toBeInTheDocument()
    expect(screen.getByRole('columnheader', { name: 'C' })).toBeInTheDocument()
    expect(screen.getByRole('rowheader', { name: '2' })).toBeInTheDocument()
    // Formula cell with no cached value shows the formula source.
    expect(screen.getByText('=B1*1.1')).toBeInTheDocument()
  })

  it('switches sheets via the sheet buttons and shows the truncation notice', async () => {
    stubFetch(async () => ({ ok: true, json: async () => payload }))
    render(<SheetViewer filePath="/ws/outbox/model.xlsx" />)
    const tab = await screen.findByRole('button', { name: 'Assumptions' })
    fireEvent.click(tab)
    expect(await screen.findByText('WACC')).toBeInTheDocument()
    expect(screen.getByText(/Showing first 1 rows/)).toBeInTheDocument()
  })

  it('explains formula cells in the footer when the sheet contains any', async () => {
    stubFetch(async () => ({ ok: true, json: async () => payload }))
    render(<SheetViewer filePath="/ws/outbox/model.xlsx" />)
    await screen.findByText('Revenue')
    expect(screen.getByText(/Computed values are not stored in this file/)).toBeInTheDocument()
  })

  it('requests /api/file-sheet with the encoded file path', async () => {
    stubFetch(async () => ({ ok: true, json: async () => payload }))
    render(<SheetViewer filePath="/ws/out box/model.xlsx" />)
    await screen.findByText('Revenue')
    expect(vi.mocked(fetch)).toHaveBeenCalledWith(
      '/api/file-sheet?path=' + encodeURIComponent('/ws/out box/model.xlsx'),
      expect.anything(),
    )
  })

  it('degrades to the download card with a sheet-specific failure banner when the endpoint fails', async () => {
    // 422 = parse failure; the viewer must never be worse than the card it replaced,
    // and the banner must not claim xlsx can never preview inline.
    stubFetch(async () => ({ ok: false, status: 422, json: async () => ({ error: 'cannot parse workbook' }) }))
    // Fallback card renders OfficeViewer, which calls useQuery — needs the provider.
    renderWithQuery(<SheetViewer filePath="/ws/outbox/model.xlsx" />)
    expect(await screen.findByText('model.xlsx')).toBeInTheDocument()
    expect(screen.getByText(/Preview failed/)).toBeInTheDocument()
    const link = screen.getByRole('link', { name: /model\.xlsx/i })
    expect(link).toHaveAttribute('href', expect.stringContaining('/api/file-download?path='))
  })

  it('degrades to the download card when fetch itself rejects', async () => {
    stubFetch(async () => { throw new Error('network down') })
    renderWithQuery(<SheetViewer filePath="/ws/outbox/model.xlsx" />)
    expect(await screen.findByRole('link', { name: /model\.xlsx/i })).toBeInTheDocument()
  })

  it('shows the empty-sheet notice for a workbook with no populated cells', async () => {
    stubFetch(async () => ({
      ok: true,
      json: async () => ({
        sheets: [{ name: 'Sheet1', rows: [], truncated_rows: false, total_rows: null, truncated_cols: false }],
        total_sheets: 1,
        truncated_sheets: false,
      }),
    }))
    render(<SheetViewer filePath="/ws/outbox/empty.xlsx" />)
    expect(await screen.findByText('Empty sheet')).toBeInTheDocument()
  })
})

describe('columnLetter', () => {
  it('maps 0-based indices to spreadsheet letters across the AA boundary', () => {
    expect(columnLetter(0)).toBe('A')
    expect(columnLetter(25)).toBe('Z')
    expect(columnLetter(26)).toBe('AA')
    expect(columnLetter(51)).toBe('AZ')
    expect(columnLetter(52)).toBe('BA')
    expect(columnLetter(701)).toBe('ZZ')
    expect(columnLetter(702)).toBe('AAA')
  })
})
