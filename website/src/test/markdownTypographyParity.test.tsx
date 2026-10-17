import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import MarkdownRenderer from '../components/MarkdownRenderer'
import { OfficeViewer } from '../components/FileRenderers'
import {
  MD_EM_CLS,
  MD_HEADING_CLS,
  MD_LIST_ITEM_CLS,
  MD_OL_CLS,
  MD_STRONG_CLS,
  MD_TABLE_CELL_CLS,
  MD_TABLE_HEAD_CELL_CLS,
  MD_UL_CLS,
} from '../components/markdown/typography'
import { __resetPathKindCache } from '../hooks/usePathKind'

/**
 * A heading, list, table cell or inline run in a `.docx`'s structured preview
 * renders with the same typography as the same element in markdown. Both
 * surfaces are rendered for real and their resolved `class` attributes are
 * compared byte for byte, so restyling one without the other fails here.
 */

vi.mock('../hooks/useBranding', () => ({
  useBranding: () => ({ botName: 'Test', avatar: '', directLocal: true }),
}))
vi.mock('../hooks/useGatewayPlatform', () => ({ useGatewayPlatform: () => 'other' }))
vi.mock('../utils/clipboard', () => ({ copyToClipboard: vi.fn().mockResolvedValue(true), copyCode: vi.fn() }))
vi.mock('mermaid', () => ({
  default: { initialize: vi.fn(), render: vi.fn(() => new Promise(() => {})) },
}))

const MARKDOWN = [
  '# H1', '## H2', '### H3', '#### H4', '##### H5', '###### H6',
  '',
  'plain **bold** and *slanted*',
  '',
  '- dot',
  '',
  '1. first',
  '',
  '| Head |',
  '| --- |',
  '| cell |',
].join('\n')

const BLOCKS = [
  { type: 'heading', level: 1, text: 'H1' },
  { type: 'heading', level: 2, text: 'H2' },
  { type: 'heading', level: 3, text: 'H3' },
  { type: 'heading', level: 4, text: 'H4' },
  { type: 'heading', level: 5, text: 'H5' },
  { type: 'heading', level: 6, text: 'H6' },
  { type: 'paragraph', runs: [{ text: 'plain ' }, { text: 'bold', bold: true }, { text: ' and ' }, { text: 'slanted', italic: true }] },
  { type: 'list', ordered: false, items: ['dot'] },
  { type: 'list', ordered: true, items: ['first'] },
  { type: 'table', rows: [['Head'], ['cell']] },
]

const realFetch = globalThis.fetch

beforeEach(() => {
  __resetPathKindCache()
  globalThis.fetch = vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('file-office-preview')) {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ blocks: BLOCKS, truncated: false }),
      } as unknown as Response)
    }
    return Promise.resolve({ ok: false, status: 404, headers: new Headers() } as Response)
  }) as unknown as typeof fetch
})

afterEach(() => {
  globalThis.fetch = realFetch
  vi.restoreAllMocks()
})

function renderWithQuery(ui: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>)
}

/** The class attribute of the first `selector` in `root`, failing loudly when absent. */
function cls(root: Element, selector: string): string {
  const el = root.querySelector(selector)
  expect(el, selector).not.toBeNull()
  return el!.getAttribute('class') ?? ''
}

async function renderBoth() {
  const md = render(<MarkdownRenderer content={MARKDOWN} />).container
  const office = renderWithQuery(<OfficeViewer filePath="/home/user/docs/parity.docx" />).container
  await waitFor(() => expect(office.querySelector('td')).not.toBeNull())
  await waitFor(() => expect(md.querySelector('td')).not.toBeNull())
  expect(screen.getAllByText('H1')).toHaveLength(2)
  return { md, office }
}

describe('markdown and Office preview typography parity', () => {
  it('resolves the same class for every heading level', async () => {
    const { md, office } = await renderBoth()
    for (let level = 1; level <= 6; level++) {
      const tag = `h${level}`
      expect(cls(office, tag)).toBe(cls(md, tag))
      expect(cls(md, tag)).toBe(MD_HEADING_CLS[level - 1])
    }
  })

  it('resolves the same class for both list kinds', async () => {
    const { md, office } = await renderBoth()
    expect(cls(office, 'ul')).toBe(cls(md, 'ul'))
    expect(cls(office, 'ol')).toBe(cls(md, 'ol'))
    expect(cls(md, 'ul')).toBe(MD_UL_CLS)
    expect(cls(md, 'ol')).toBe(MD_OL_CLS)
  })

  it('resolves the same class for bold and italic runs', async () => {
    const { md, office } = await renderBoth()
    expect(cls(office, 'strong')).toBe(cls(md, 'strong'))
    expect(cls(office, 'em')).toBe(cls(md, 'em'))
    expect(cls(md, 'strong')).toBe(MD_STRONG_CLS)
    expect(cls(md, 'em')).toBe(MD_EM_CLS)
  })

  it('shares the list-item and table-cell base, each surface adding only its wrapping rule', async () => {
    const { md, office } = await renderBoth()
    expect(cls(md, 'li')).toBe(MD_LIST_ITEM_CLS)
    expect(cls(office, 'li')).toBe(`${MD_LIST_ITEM_CLS} break-words`)
    expect(cls(md, 'th')).toBe(`${MD_TABLE_HEAD_CELL_CLS} whitespace-nowrap`)
    expect(cls(office, 'th')).toBe(`${MD_TABLE_HEAD_CELL_CLS} whitespace-pre-line align-top`)
    expect(cls(md, 'td')).toBe(MD_TABLE_CELL_CLS)
    expect(cls(office, 'td')).toBe(`${MD_TABLE_CELL_CLS} whitespace-pre-line align-top`)
  })

  it('keeps the class strings the two surfaces rendered before they shared an owner', () => {
    // Literal values, so a change to the shared owner is a visible restyle of
    // both surfaces in review rather than a silent one.
    expect(MD_HEADING_CLS).toEqual([
      'text-xl font-bold mt-4 mb-2 text-text-strong',
      'text-lg font-bold mt-3 mb-2 text-text-strong',
      'text-base font-semibold mt-3 mb-1.5 text-text-strong',
      'text-sm font-semibold mt-2 mb-1 text-text-strong',
      'text-sm font-medium mt-2 mb-1 text-text-strong',
      'text-[13px] font-medium mt-2 mb-1 text-muted',
    ])
    expect(MD_UL_CLS).toBe('list-disc pl-8 my-2 space-y-1 marker:text-muted')
    expect(MD_OL_CLS).toBe('list-decimal pl-8 my-2 space-y-1 marker:text-muted')
    expect(MD_LIST_ITEM_CLS).toBe('text-sm leading-relaxed')
    expect(MD_TABLE_HEAD_CELL_CLS).toBe('text-left text-muted text-[13px] font-medium px-3 py-2 border-b border-border bg-bg-elevated')
    expect(MD_TABLE_CELL_CLS).toBe('px-3 py-2 border-b border-border text-sm')
    expect(MD_STRONG_CLS).toBe('font-semibold text-text-strong')
    expect(MD_EM_CLS).toBe('italic')
  })
})
