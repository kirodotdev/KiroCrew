/**
 * Source editing for the text-backed rich file types (json, csv, html, ...).
 *
 * These open in their viewer, as before, and Edit switches the tab to the code
 * editor over the same buffer, with Save and Preview, exactly like markdown.
 * The byte-backed rich types (image, pdf, ...) have no text source and must
 * keep offering no Edit at all.
 *
 * Pierre is stubbed with a textarea so a test can type into the editor the way
 * a keystroke reaches the panel's onChange.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { forwardRef, useImperativeHandle, useState } from 'react'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { PierreEditorHandle } from '../pierre'

vi.mock('../pierre', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  PierreEditor: forwardRef<PierreEditorHandle, { file: { contents: string }; onChange?: (value: string) => void }>(
    function PierreEditorStub({ file, onChange }, ref) {
      useImperativeHandle(ref, () => ({ jumpToLine: () => {}, focus: () => {} }), [])
      return <textarea data-testid="pierre-editor" aria-label="editor" value={file.contents} onChange={e => onChange?.(e.target.value)} />
    },
  ),
  PierreCode: ({ file }: { file: { contents: string } }) => (
    <div data-testid="pierre-code" data-value={file.contents} />
  ),
  PierreFilePair: () => <div data-testid="pierre-diff" />,
}))

vi.mock('../api/client', () => ({
  api: {
    artifacts: vi.fn().mockResolvedValue({ artifacts: [] }),
    artifact: vi.fn().mockResolvedValue({}),
    fileDiff: vi.fn().mockResolvedValue({ diff: '', original: '', status: 'clean' }),
    revealPath: vi.fn(),
  },
}))

const { default: MarkdownPanel } = await import('../components/MarkdownPanel')
const { i18nT } = await import('../i18n/t')

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

beforeEach(() => {
  qc.clear()
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: true, status: 200, headers: { get: () => null }, json: async () => ({}), text: async () => '',
  })))
})

/** A host that owns the buffer the way SidePanel does, so typing in the
 *  editor flows back into `content` and Save writes what is on screen. */
function Host({ filePath, initial, onSave, partial, onRailToggle }: { filePath: string; initial: string; onSave: (p: string, c: string) => Promise<void>; partial?: boolean; onRailToggle?: () => void }) {
  const [content, setContent] = useState(initial)
  return (
    <MarkdownPanel
      embedded
      filePath={filePath}
      content={content}
      partial={partial}
      onContentChange={setContent}
      onSave={onSave}
      onClose={() => {}}
      onRailToggle={onRailToggle}
    />
  )
}

function mount(filePath: string, initial: string, onSave = vi.fn(async () => {}), partial?: boolean, onRailToggle?: () => void) {
  const view = render(
    <MemoryRouter><QueryClientProvider client={qc}><Host filePath={filePath} initial={initial} onSave={onSave} partial={partial} onRailToggle={onRailToggle} /></QueryClientProvider></MemoryRouter>,
  )
  return Object.assign(onSave, { view })
}

const editLabel = () => i18nT('components.markdownPanel.edit')
const previewLabel = () => i18nT('components.markdownPanel.preview')

describe('MarkdownPanel — source editing for text-backed rich files', () => {
  it.each([false, true])('opens json with Edit in the artifact button slot (file-browser toggle: %s)', async withBrowserToggle => {
    mount('/x/mcp.json', '{"mcpServers":{}}', undefined, undefined, withBrowserToggle ? () => {} : undefined)
    const edit = await screen.findByRole('button', { name: editLabel() })
    const header = edit.parentElement!
    expect(screen.queryByTestId('pierre-editor')).toBeNull()
    expect(screen.getByText('"mcpServers"')).toBeInTheDocument()
    expect(within(header).queryByRole('button', { name: i18nT('components.markdownPanel.add_to_artifact_library') })).toBeNull()
    expect(within(header).getAllByRole('button')).toHaveLength(withBrowserToggle ? 3 : 2)

    const more = within(header).getByRole('button', { name: i18nT('components.markdownPanel.more_options') })
    fireEvent.click(more)
    const menu = await screen.findByRole('menu')
    expect(within(menu).getByRole('menuitem', { name: i18nT('components.markdownPanel.add_to_artifacts') })).toBeEnabled()
    fireEvent.click(more)
    fireEvent.click(edit)
    expect(await screen.findByRole('button', { name: previewLabel() })).toBeInTheDocument()
    expect(within(header).getAllByRole('button')).toHaveLength(withBrowserToggle ? 3 : 2)
  })

  it('edits a json file as source and saves the edited buffer', async () => {
    const onSave = mount('/x/mcp.json', '{"a":1}')
    fireEvent.click(await screen.findByRole('button', { name: editLabel() }))
    const editor = await screen.findByTestId('pierre-editor')
    expect(editor).toHaveValue('{"a":1}')
    fireEvent.change(editor, { target: { value: '{"a":2}' } })
    // The unsaved-changes banner carries the Save for the embedded panel.
    const save = await screen.findByRole('button', { name: i18nT('components.markdownPanel.save') })
    fireEvent.click(save)
    await waitFor(() => expect(onSave).toHaveBeenCalledWith('/x/mcp.json', '{"a":2}'))
  })

  it('returns to the viewer with Preview, showing the edited buffer', async () => {
    mount('/x/data.json', '{"before":1}')
    fireEvent.click(await screen.findByRole('button', { name: editLabel() }))
    fireEvent.change(await screen.findByTestId('pierre-editor'), { target: { value: '{"after":1}' } })
    fireEvent.click(screen.getByRole('button', { name: previewLabel() }))
    await waitFor(() => expect(screen.queryByTestId('pierre-editor')).toBeNull())
    expect(screen.getByText('"after"')).toBeInTheDocument()
  })

  it.each([
    ['/x/rows.csv', 'a,b\n1,2\n'],
    ['/x/log.jsonl', '{"a":1}\n'],
    ['/x/page.html', '<p>hi</p>'],
    ['/x/scene.excalidraw', '{"type":"excalidraw","elements":[]}'],
  ])('offers Edit for %s and mounts the editor over its text', async (filePath, text) => {
    mount(filePath, text)
    fireEvent.click(await screen.findByRole('button', { name: editLabel() }))
    expect(await screen.findByTestId('pierre-editor')).toHaveValue(text)
  })

  it('offers no Edit for a partial (truncated or redacted) json buffer, and says why', async () => {
    // A save writes the buffer over the whole file: from a redacted copy it
    // would replace the real secrets with placeholders.
    mount('/x/mcp.json', '{"token":"[REDACTED]"}', undefined, true)
    expect(await screen.findByTestId('markdown-panel-partial-read-only')).toHaveAttribute(
      'aria-label', i18nT('components.markdownPanel.partial_copy_read_only'),
    )
    expect(await screen.findByRole('button', { name: i18nT('components.markdownPanel.add_to_artifact_library') })).toBeEnabled()
    expect(screen.queryByRole('button', { name: editLabel() })).toBeNull()
    expect(screen.queryByTestId('pierre-editor')).toBeNull()
  })

  it('closes the source view and refuses the save when a partial verdict lands after an edit', async () => {
    const onSave = mount('/x/mcp.json', '{"a":1}')
    fireEvent.click(await screen.findByRole('button', { name: editLabel() }))
    fireEvent.change(await screen.findByTestId('pierre-editor'), { target: { value: '{"a":2}' } })
    onSave.view.rerender(
      <MemoryRouter><QueryClientProvider client={qc}><Host filePath="/x/mcp.json" initial={'{"a":1}'} onSave={onSave} partial /></QueryClientProvider></MemoryRouter>,
    )
    await waitFor(() => expect(screen.queryByTestId('pierre-editor')).toBeNull())
    // The draft is kept (the unsaved row stays), but its Save cannot write it.
    const save = screen.getByRole('button', { name: i18nT('components.markdownPanel.save') })
    expect(save).toBeDisabled()
    fireEvent.click(save)
    expect(onSave).not.toHaveBeenCalled()
  })

  it('adds no header buttons when a rich file in source mode goes full screen', async () => {
    // The full-screen header is already past two controls and must not grow:
    // a rich file's Save and Cancel ride the unsaved-changes row instead.
    mount('/x/mcp.json', '{"a":1}')
    const dialogButtons = () => {
      const dialog = screen.getByRole('dialog', { name: i18nT('components.markdownPanel.full_screen_file_preview') })
      const header = dialog.firstElementChild as HTMLElement
      return header.querySelectorAll('button').length
    }
    fireEvent.click(await screen.findByRole('button', { name: i18nT('components.markdownPanel.more_options') }))
    fireEvent.click(await screen.findByText(i18nT('components.markdownPanel.full_screen')))
    const before = dialogButtons()
    // Leave full screen, switch to source, come back: same header count.
    fireEvent.click(screen.getByRole('button', { name: i18nT('components.markdownPanel.exit_full_screen') }))
    fireEvent.click(await screen.findByRole('button', { name: editLabel() }))
    fireEvent.change(await screen.findByTestId('pierre-editor'), { target: { value: '{"a":2}' } })
    fireEvent.click(await screen.findByRole('button', { name: i18nT('components.markdownPanel.more_options') }))
    fireEvent.click(await screen.findByText(i18nT('components.markdownPanel.full_screen')))
    expect(dialogButtons()).toBe(before)
    // The buffer's Save is still reachable, on its own row.
    const dialog = screen.getByRole('dialog', { name: i18nT('components.markdownPanel.full_screen_file_preview') })
    expect(within(dialog).getAllByRole('button', { name: i18nT('components.markdownPanel.save') }).length).toBeGreaterThan(0)
  })

  it('offers no Edit for a byte-backed rich file', async () => {
    mount('/x/diagram.png', 'iVBORw0KGgo=')
    await waitFor(() => expect(document.querySelector('img')).not.toBeNull())
    expect(screen.queryByRole('button', { name: editLabel() })).toBeNull()
    expect(screen.queryByTestId('pierre-editor')).toBeNull()
  })
})
