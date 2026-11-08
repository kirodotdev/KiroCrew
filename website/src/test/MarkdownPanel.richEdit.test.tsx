/**
 * Text-backed rich files (json, jsonl, csv, html, excalidraw) open in their
 * viewer and an explicit Edit toggle swaps the viewer for the text editor, with
 * the same Save as a code file. A buffer whose read was partial (capped,
 * redacted or lossily decoded) is never written back over the file.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { createTestStore } from './helpers'
import MarkdownPanel from '../components/MarkdownPanel'
import { i18nT } from '../i18n/t'
import type { PartialCause } from '../utils/fileReadQuery'

vi.mock('../pierre', () => ({
  PierreCode: () => <div data-testid="pierre-code" />,
  PierreEditor: () => <div data-testid="pierre-editor" />,
  PierreFilePair: () => <div data-testid="pierre-diff" />,
}))
vi.mock('../components/CodeEditor', () => ({
  CodeEditor: ({ content, onChange }: { content: string; onChange: (v: string) => void }) => (
    <textarea aria-label="source" data-testid="code-editor" value={content} onChange={e => onChange(e.target.value)} />
  ),
}))
vi.mock('../components/FileRenderers', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../components/FileRenderers')>()),
  JsonViewer: () => <div data-testid="rich-viewer" />,
  JsonlViewer: () => <div data-testid="rich-viewer" />,
  CsvViewer: () => <div data-testid="rich-viewer" />,
  HtmlViewer: () => <div data-testid="rich-viewer" />,
  ExcalidrawViewer: () => <div data-testid="rich-viewer" />,
}))

type PanelProps = { filePath: string; content: string; partial?: PartialCause; readOnly?: boolean }

function renderPanel(props: PanelProps) {
  const store = createTestStore()
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const onSave = vi.fn(() => Promise.resolve())
  const panel = (p: PanelProps) => (
    <Provider store={store}>
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <MarkdownPanel
            embedded
            filePath={p.filePath}
            content={p.content}
            partial={p.partial}
            readOnly={p.readOnly}
            onContentChange={vi.fn()}
            onSave={onSave}
            onClose={vi.fn()}
          />
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>
  )
  const view = render(panel(props))
  return { ...view, onSave, rerenderPanel: (next: PanelProps) => view.rerender(panel(next)) }
}

const editButton = () => screen.queryAllByRole('button', { name: 'Edit' })[0] ?? null
const previewButton = () => screen.queryAllByRole('button', { name: 'Preview' })[0] ?? null
const saveButton = () => screen.queryAllByRole('button', { name: 'Save' })[0] ?? null

describe('MarkdownPanel: editing text-backed rich files', () => {
  beforeEach(() => {
    globalThis.fetch = vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        headers: { get: () => null },
        text: () => Promise.resolve(''),
        json: () => Promise.resolve({}),
      } as unknown as Response),
    ) as unknown as typeof fetch
  })

  it.each(['data.json', 'rows.jsonl', 'table.csv', 'page.html', 'board.excalidraw'])(
    '%s opens in its viewer with an Edit toggle and no Save',
    (name) => {
      renderPanel({ filePath: `/home/user/${name}`, content: '{}' })
      expect(screen.getByTestId('rich-viewer')).toBeInTheDocument()
      expect(screen.queryByTestId('code-editor')).toBeNull()
      expect(editButton()).not.toBeNull()
      expect(saveButton()).toBeNull()
    },
  )

  it('switches a json file to the editor, saves it, and switches back', async () => {
    const { onSave } = renderPanel({ filePath: '/home/user/sources.json', content: '{"a":1}' })
    fireEvent.click(editButton()!)
    expect(screen.getByTestId('code-editor')).toBeInTheDocument()
    expect(screen.queryByTestId('rich-viewer')).toBeNull()
    // The embedded panel's Save sits in the unsaved-changes bar, shown once dirty.
    expect(saveButton()).toBeNull()

    fireEvent.change(screen.getByTestId('code-editor'), { target: { value: '{"a":2}' } })
    expect(saveButton()).toBeEnabled()
    await act(async () => { fireEvent.click(saveButton()!) })
    expect(onSave).toHaveBeenCalledWith('/home/user/sources.json', '{"a":1}')
    await vi.waitFor(() => expect(saveButton()).toBeNull())

    fireEvent.click(previewButton()!)
    expect(screen.getByTestId('rich-viewer')).toBeInTheDocument()
    expect(screen.queryByTestId('code-editor')).toBeNull()
  })

  it('keeps wrap and line-number toggles off the header in rich source mode', () => {
    renderPanel({ filePath: '/home/user/sources.json', content: '{}' })
    fireEvent.click(editButton()!)
    expect(screen.queryByRole('button', { name: /toggle word wrap/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /toggle line numbers/i })).toBeNull()
  })

  it('keeps the json header to Edit and ⋯, with Refresh and add-to-library in the menu', () => {
    renderPanel({ filePath: '/home/user/sources.json', content: '{}' })
    expect(screen.queryByRole('button', { name: /add to artifact library/i })).toBeNull()
    fireEvent.click(screen.getAllByTestId('markdown-panel-more-options')[0])
    expect(screen.getByRole('menuitem', { name: /refresh/i })).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: /add to artifacts/i })).toBeInTheDocument()
  })

  it('leaves a code file header as it was', () => {
    renderPanel({ filePath: '/home/user/app/settings.py', content: 'x = 1' })
    expect(screen.getByRole('button', { name: /add to artifact library/i })).toBeInTheDocument()
  })

  it('offers no Edit toggle for a byte-backed viewer or a read-only tab', () => {
    const { unmount } = renderPanel({ filePath: '/home/user/shot.png', content: '' })
    expect(editButton()).toBeNull()
    unmount()
    renderPanel({ filePath: '/home/user/data.json', content: '{}', readOnly: true })
    expect(editButton()).toBeNull()
  })
})

describe('MarkdownPanel: a partial buffer is never saved', () => {
  beforeEach(() => {
    globalThis.fetch = vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        headers: { get: () => null },
        text: () => Promise.resolve(''),
        json: () => Promise.resolve({}),
      } as unknown as Response),
    ) as unknown as typeof fetch
  })

  it('keeps Save disabled and says why for a partial code file', () => {
    const { onSave } = renderPanel({ filePath: '/home/user/app/.env.py', content: 'KEY=***', partial: 'redacted' })
    expect(screen.getByTestId('code-editor')).toBeInTheDocument()
    const notice = screen.getByTestId('markdown-panel-partial-notice').textContent
    expect(notice).toBe(i18nT('components.markdownPanel.partial_copy_cannot_save', { reason: i18nT('components.markdownPanel.partial_reason_redacted') }))
    expect(notice).toMatch(/credentials/)
    expect(notice).not.toMatch(/cut short/)
    fireEvent.change(screen.getByTestId('code-editor'), { target: { value: 'KEY=***x' } })
    expect(saveButton()).toBeDisabled()
    fireEvent.click(saveButton()!)
    expect(onSave).not.toHaveBeenCalled()
  })

  it('refuses Cmd+S on a partial buffer and reports it', async () => {
    const { onSave } = renderPanel({ filePath: '/home/user/app/settings.py', content: 'x = 1', partial: 'truncated' })
    fireEvent.change(screen.getByTestId('code-editor'), { target: { value: 'x = 2' } })
    fireEvent.keyDown(document, { key: 's', metaKey: true })
    expect((await screen.findByTestId('markdown-panel-save-error')).textContent).toMatch(/cut short at the gateway's size limit/)
    expect(onSave).not.toHaveBeenCalled()
  })

  it('saves the same code file when the read was whole', async () => {
    const { onSave } = renderPanel({ filePath: '/home/user/app/settings.py', content: 'x = 1' })
    expect(screen.queryByTestId('markdown-panel-partial-notice')).toBeNull()
    fireEvent.change(screen.getByTestId('code-editor'), { target: { value: 'x = 2' } })
    await act(async () => { fireEvent.click(saveButton()!) })
    expect(onSave).toHaveBeenCalledTimes(1)
  })

  it.each(['/home/user/sources.json', '/home/user/notes.md'])('offers no Edit toggle for a partial %s', (filePath) => {
    renderPanel({ filePath, content: 'cut short', partial: 'truncated' })
    expect(editButton()).toBeNull()
  })

  it('still lets an editor whose read turned partial go back to its viewer', () => {
    const { rerenderPanel } = renderPanel({ filePath: '/home/user/sources.json', content: '{}' })
    fireEvent.click(editButton()!)
    fireEvent.change(screen.getByTestId('code-editor'), { target: { value: '{"b":1}' } })
    rerenderPanel({ filePath: '/home/user/sources.json', content: '{}', partial: 'truncated' })
    expect(saveButton()).toBeDisabled()
    fireEvent.click(previewButton()!)
    expect(screen.getByTestId('rich-viewer')).toBeInTheDocument()
    expect(editButton()).toBeNull()
  })
})
