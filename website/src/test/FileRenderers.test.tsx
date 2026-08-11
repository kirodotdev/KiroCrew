import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { detectFileType, JsonlViewer, OfficeViewer } from '../components/FileRenderers'

describe('detectFileType', () => {
  it('returns jsonl for .jsonl files', () => {
    expect(detectFileType('data.jsonl')).toBe('jsonl')
    expect(detectFileType('/path/to/session.jsonl')).toBe('jsonl')
  })

  it('returns json for .json files (not jsonl)', () => {
    expect(detectFileType('config.json')).toBe('json')
  })

  it('returns office for OOXML and legacy Office extensions', () => {
    // OOXML (ZIP-based) — the specific formats that motivated this fix.
    expect(detectFileType('report.docx')).toBe('office')
    expect(detectFileType('workbook.xlsx')).toBe('office')
    expect(detectFileType('deck.pptx')).toBe('office')
    // Legacy OLE compound files.
    expect(detectFileType('old.doc')).toBe('office')
    expect(detectFileType('old.xls')).toBe('office')
    expect(detectFileType('old.ppt')).toBe('office')
    // OpenDocument formats.
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
