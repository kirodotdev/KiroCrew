/**
 * The ⋯ menu's "Print or save as PDF" prints the rendered markdown preview
 * alone. The print stylesheet keys on two marks (the preview root and <html>),
 * so what this pins is that both are in place when the dialog opens, that the
 * saved PDF is named after the file, and that all of it is undone afterwards.
 */
import { describe, it, expect, vi, beforeEach, afterEach, onTestFinished } from 'vitest'
import { act, render, screen, fireEvent, waitFor, within } from '@testing-library/react'
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
const { printPreview } = await import('../components/markdownPanelPrint')

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
const printLabel = () => i18nT('components.markdownPanel.print_or_save_as_pdf')

/** What the page looked like at the moment the print dialog opened. */
let atPrint: { printing: boolean; root: Element | null; title: string; imageLoading: string[] } | undefined

beforeEach(() => {
  qc.clear()
  localStorage.clear()
  atPrint = undefined
  document.title = 'Kiro Crew'
  vi.stubGlobal('fetch', vi.fn(async (input: unknown) => {
    const url = String(input)
    if (url.startsWith('/api/knowledge/config')) return { ok: true, json: async () => ({ enabled: false, supported_formats: [] }) }
    if (url.startsWith('/api/knowledge/sources')) return { ok: true, json: async () => [] }
    return { ok: true, status: 200, headers: { get: () => null }, text: async () => '# Title\n' }
  }))
  // happy-dom has no print dialog; record the page state it would print instead.
  Object.defineProperty(window, 'print', {
    configurable: true,
    writable: true,
    value: vi.fn(() => {
      atPrint = {
        printing: document.documentElement.hasAttribute('data-printing-preview'),
        root: document.querySelector('[data-print-root]'),
        title: document.title,
        imageLoading: Array.from(document.querySelectorAll('img'), img => img.loading),
      }
    }),
  })
})

afterEach(() => {
  vi.unstubAllGlobals()
  Reflect.deleteProperty(window, 'print')
  document.body.style.overflow = ''
})

function mount({ partial = false } = {}) {
  render(
    <MemoryRouter>
      <QueryClientProvider client={qc}>
        <MarkdownPanel embedded filePath="/tmp/release-notes.md" content={'# Title\n\n![chart](chart.png)\n'} onContentChange={vi.fn()} onSave={vi.fn(async () => {})} onClose={vi.fn()} partial={partial} />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

describe('MarkdownPanel ⋯ Print or save as PDF', () => {
  it('prints the rendered preview alone under the file name, then restores the page', async () => {
    mount()
    const heading = await screen.findByRole('heading', { name: 'Title' })
    fireEvent.click(screen.getAllByTestId('markdown-panel-more-options')[0])
    fireEvent.click(screen.getByText(printLabel()))

    await waitFor(() => expect(window.print).toHaveBeenCalledTimes(1))
    expect(atPrint?.printing).toBe(true)
    expect(atPrint?.root).toContainElement(heading)
    expect(atPrint?.title).toBe('release-notes')
    // A lazy image would print blank had the reader not scrolled to it.
    expect(atPrint?.imageLoading).toEqual(['eager'])

    act(() => { window.dispatchEvent(new Event('afterprint')) })
    expect(document.querySelector('[data-print-root]')).toBeNull()
    expect(document.documentElement).not.toHaveAttribute('data-printing-preview')
    expect(document.title).toBe('Kiro Crew')
  })

  it('names a partial read like its download, never as the whole file', async () => {
    mount({ partial: true })
    await screen.findByRole('heading', { name: 'Title' })
    fireEvent.click(screen.getAllByTestId('markdown-panel-more-options')[0])
    fireEvent.click(screen.getByText(printLabel()))
    await waitFor(() => expect(window.print).toHaveBeenCalledTimes(1))
    expect(atPrint?.title).toBe('release-notes.partial')
  })

  it('prints the full-screen preview from the full-screen menu', async () => {
    mount()
    await screen.findByRole('heading', { name: 'Title' })
    fireEvent.click(screen.getAllByTestId('markdown-panel-more-options')[0])
    fireEvent.click(screen.getByText(i18nT('components.markdownPanel.full_screen')))
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(screen.getByTestId('markdown-panel-fullscreen-header')).getByTestId('markdown-panel-more-options'))
    fireEvent.click(screen.getByText(printLabel()))
    await waitFor(() => expect(window.print).toHaveBeenCalledTimes(1))
    expect(atPrint?.root).toContainElement(within(dialog).getByRole('heading', { name: 'Title' }))
  })

  it('opens the dialog even while an image never finishes loading', async () => {
    mount()
    await screen.findByRole('heading', { name: 'Title' })
    const decode = vi.spyOn(HTMLImageElement.prototype, 'decode').mockReturnValue(new Promise<void>(() => {}))
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    onTestFinished(() => { vi.useRealTimers(); decode.mockRestore() })
    fireEvent.click(screen.getAllByTestId('markdown-panel-more-options')[0])
    fireEvent.click(screen.getByText(printLabel()))
    await act(async () => { await vi.advanceTimersByTimeAsync(3000) })
    vi.useRealTimers()
    await waitFor(() => expect(window.print).toHaveBeenCalledTimes(1))
    expect(decode).toHaveBeenCalled()
  })

  it.each([
    ['/tmp/README', 'README.partial'],
    ['/tmp/.md', '.md.partial'],
    ['/tmp/notes.md', 'notes.partial'],
  ])('keeps the partial mark on %s', async (filePath, title) => {
    const root = document.createElement('div')
    document.body.appendChild(root)
    onTestFinished(() => { root.remove(); window.dispatchEvent(new Event('afterprint')) })
    await printPreview(root, filePath, true)
    expect(atPrint?.title).toBe(title)
  })

  it('keeps only the printed preview marked when other content carries the mark', async () => {
    // An SVG artifact in a hidden tab, sanitized elsewhere, that minted the mark.
    const forged = document.createElementNS('http://www.w3.org/2000/svg', 'svg')
    forged.setAttribute('data-print-root', '')
    forged.append('other file')
    const root = document.createElement('div')
    document.body.append(forged, root)
    onTestFinished(() => { forged.remove(); root.remove(); window.dispatchEvent(new Event('afterprint')) })
    await printPreview(root, '/tmp/notes.md', false)
    expect(window.print).toHaveBeenCalledTimes(1)
    expect(Array.from(document.querySelectorAll('[data-print-root]'))).toEqual([root])
  })

  it('prints nothing when full screen swaps the preview out while images load', async () => {
    // Full screen unmounts the side preview during the image wait, so the
    // root the print started from is no longer in the page when it resumes.
    const root = document.createElement('div')
    root.appendChild(document.createElement('img'))
    const decode = vi.spyOn(HTMLImageElement.prototype, 'decode').mockResolvedValue(undefined)
    onTestFinished(() => { decode.mockRestore() })
    await printPreview(root, '/tmp/release-notes.md', false)
    expect(window.print).not.toHaveBeenCalled()
    expect(root).not.toHaveAttribute('data-print-root')
    expect(document.title).toBe('Kiro Crew')
  })

  it('offers no print row while the file is open in the editor', async () => {
    mount()
    await screen.findByRole('heading', { name: 'Title' })
    fireEvent.click(screen.getByRole('button', { name: i18nT('components.markdownPanel.edit') }))
    await screen.findByTestId('pierre-editor')
    fireEvent.click(screen.getAllByTestId('markdown-panel-more-options')[0])
    expect(screen.getByText(i18nT('components.markdownPanel.download'))).toBeInTheDocument()
    expect(screen.queryByText(printLabel())).toBeNull()
  })
})
