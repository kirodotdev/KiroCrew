import { describe, it, expect, vi, afterEach } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { columnLetter, detectFileType, JsonlViewer, OfficeViewer, SheetViewer } from '../components/FileRenderers'

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
  function stubPreview(body: { text?: string; truncated?: boolean; error?: string } | null, ok = true, status = 200) {
    globalThis.fetch = vi.fn(() =>
      Promise.resolve({
        ok,
        status,
        json: () => Promise.resolve(body ?? {}),
      } as unknown as Response),
    ) as unknown as typeof fetch
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
