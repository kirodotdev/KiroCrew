/**
 * The full-screen file view's toolbar toggles say whether they are on.
 *
 * Diff view, word wrap and line numbers are on/off buttons with a fixed name.
 * Their only "on" cue was an accent border, which a screen reader never hears,
 * so they carry `aria-pressed` like the panel's inline header toggles.
 *
 * Edit/Preview is different: its name flips to the mode it switches TO. With
 * `aria-pressed` it announced "Preview, pressed" while the editor was open, so
 * it carries no pressed state in either header; the name alone says the action.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { i18nT } from '../i18n/t'

vi.mock('../pierre', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  PierreCode: () => <div data-testid="pierre-code" />,
  PierreEditor: () => <div data-testid="pierre-editor" />,
  PierreFilePair: () => <div data-testid="pierre-diff" />,
}))
vi.mock('../components/CodeEditor', () => ({
  CodeEditor: () => <div data-testid="pierre-editor" />,
}))

vi.mock('../api/client', () => ({
  api: {
    artifacts: vi.fn().mockResolvedValue({ artifacts: [] }),
    artifact: vi.fn().mockResolvedValue({ live_dirty: false, pinned: false }),
    fileDiff: vi.fn().mockResolvedValue({ diff: '', original: '', status: 'clean' }),
    revealPath: vi.fn(),
  },
}))

const { default: MarkdownPanel } = await import('../components/MarkdownPanel')

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

beforeEach(() => {
  qc.clear()
  localStorage.clear()
  vi.stubGlobal('fetch', vi.fn(async (input: unknown) => {
    const url = String(input)
    if (url.startsWith('/api/knowledge/config')) return { ok: true, json: async () => ({ enabled: false, supported_formats: [] }) }
    if (url.startsWith('/api/knowledge/sources')) return { ok: true, json: async () => [] }
    return { ok: true, status: 200, headers: { get: () => null }, text: async () => '# Title\n' }
  }))
})

afterEach(() => {
  vi.unstubAllGlobals()
  document.body.style.overflow = ''
})

async function fullscreenHeader(): Promise<HTMLElement> {
  render(
    <MemoryRouter>
      <QueryClientProvider client={qc}>
        <MarkdownPanel embedded filePath="/tmp/notes.md" content={'# Title\n'} onContentChange={vi.fn()} onSave={vi.fn(async () => {})} onClose={vi.fn()} />
      </QueryClientProvider>
    </MemoryRouter>,
  )
  fireEvent.click(screen.getAllByTestId('markdown-panel-more-options')[0])
  fireEvent.click(screen.getByText('Full screen'))
  await screen.findByRole('dialog')
  return screen.getByTestId('markdown-panel-fullscreen-header')
}

describe('MarkdownPanel full-screen toolbar toggles expose aria-pressed', () => {
  it('reports the diff toggle state', async () => {
    const header = within(await fullscreenHeader())
    const diffName = { name: i18nT('components.markdownPanel.toggle_diff_view') }
    expect(header.getByRole('button', diffName)).toHaveAttribute('aria-pressed', 'false')
    fireEvent.click(header.getByRole('button', diffName))
    expect(await header.findByRole('button', { ...diffName, pressed: true })).toBeInTheDocument()
    fireEvent.click(header.getByRole('button', diffName))
    expect(await header.findByRole('button', { ...diffName, pressed: false })).toBeInTheDocument()

  })

  it('keeps Edit/Preview an action button with no pressed state', async () => {
    const header = within(await fullscreenHeader())
    const edit = header.getByRole('button', { name: i18nT('components.markdownPanel.edit') })
    expect(edit).not.toHaveAttribute('aria-pressed')
    fireEvent.click(edit)
    expect(await header.findByRole('button', { name: i18nT('components.markdownPanel.preview') }))
      .not.toHaveAttribute('aria-pressed')
  })

  it('keeps the inline header Edit/Preview button free of a pressed state', async () => {
    await fullscreenHeader()
    // The inline header's copy sits outside the full-screen dialog.
    const inline = screen.getAllByRole('button', { name: i18nT('components.markdownPanel.edit'), hidden: true })
      .filter(b => !screen.getByTestId('markdown-panel-fullscreen-header').contains(b))
    expect(inline.length).toBeGreaterThan(0)
    for (const b of inline) expect(b).not.toHaveAttribute('aria-pressed')
  })

  it('reports word wrap and line numbers while editing', async () => {
    const header = within(await fullscreenHeader())
    fireEvent.click(header.getByRole('button', { name: i18nT('components.markdownPanel.edit') }))

    const wrap = await header.findByRole('button', { name: i18nT('components.markdownPanel.toggle_word_wrap') })
    const nums = header.getByRole('button', { name: i18nT('components.markdownPanel.toggle_line_numbers') })
    // Both persist as on by default.
    expect(wrap).toHaveAttribute('aria-pressed', 'true')
    expect(nums).toHaveAttribute('aria-pressed', 'true')
    fireEvent.click(wrap)
    fireEvent.click(nums)
    expect(wrap).toHaveAttribute('aria-pressed', 'false')
    expect(nums).toHaveAttribute('aria-pressed', 'false')
  })
})
