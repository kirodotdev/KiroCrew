import { describe, it, expect } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
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

describe('OfficeViewer', () => {
  it('renders filename, extension badge, and a Download link pointing at /api/file-download', () => {
    render(<OfficeViewer filePath="/home/user/docs/quarterly-report.docx" />)
    // Filename shown to the user (basename, not full path).
    expect(screen.getByText('quarterly-report.docx')).toBeInTheDocument()
    // Extension badge — uppercase, drives the visual "this is a DOCX" cue.
    expect(screen.getByText('DOCX')).toBeInTheDocument()
    // Accessible download control routed through /api/file-download so the
    // browser sees attachment disposition + nosniff and downloads raw bytes
    // instead of trying to render UTF-8-decoded ZIP garbage.
    const link = screen.getByRole('link', { name: /quarterly-report\.docx/i })
    expect(link).toHaveAttribute('href', expect.stringContaining('/api/file-download?path='))
    expect(link).toHaveAttribute('href', expect.stringContaining('quarterly-report.docx'))
    expect(link).toHaveAttribute('download', 'quarterly-report.docx')
  })

  it('extracts the basename from a Windows path with backslash separators', () => {
    // Kiro Crew ships native on Windows where filePath arrives as
    // C:\Users\...\report.docx. A `/`-only split would surface the whole
    // path — split on BOTH separators to match MarkdownRenderer/VectorMemoryCard.
    render(<OfficeViewer filePath="C:\\Users\\harpreet\\Documents\\report.docx" />)
    expect(screen.getByText('report.docx')).toBeInTheDocument()
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
    render(<SheetViewer filePath="/ws/outbox/model.xlsx" />)
    expect(await screen.findByText('model.xlsx')).toBeInTheDocument()
    expect(screen.getByText(/Preview failed/)).toBeInTheDocument()
    const link = screen.getByRole('link', { name: /model\.xlsx/i })
    expect(link).toHaveAttribute('href', expect.stringContaining('/api/file-download?path='))
  })

  it('degrades to the download card when fetch itself rejects', async () => {
    stubFetch(async () => { throw new Error('network down') })
    render(<SheetViewer filePath="/ws/outbox/model.xlsx" />)
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
