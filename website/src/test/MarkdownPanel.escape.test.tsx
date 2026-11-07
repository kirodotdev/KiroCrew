/**
 * Escape typed in the side panel's code editor leaves the file open (#23975).
 *
 * The panel answers Escape anywhere on the page by closing its file, so a press
 * meant for the editor (to drop a selection, say) closed the file mid-edit. A
 * field elsewhere in the panel, the Files rail's filter, keeps its Escape too.
 * Escape from a control that is not a field, or from outside the panel (the chat
 * composer beside it included: it yields the key to an open file), still closes it.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { forwardRef, useImperativeHandle } from 'react'
import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { PierreEditorHandle } from '../pierre'

// Pierre's editable surface lives in a shadow root happy-dom cannot paint; a
// textarea is the editable field inside the panel's body this needs.
vi.mock('../pierre', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  PierreEditor: forwardRef<PierreEditorHandle, { file: { contents: string } }>(
    function PierreEditorStub({ file }, ref) {
      useImperativeHandle(ref, () => ({ jumpToLine: () => {}, focus: () => {} }), [])
      return <textarea data-testid="pierre-editor" aria-label="Code editor" defaultValue={file.contents} />
    },
  ),
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

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('MarkdownPanel — Escape in the code editor (#23975)', () => {
  it('keeps the file open from a field in the panel and closes it from anywhere else', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: true, status: 200, headers: { get: () => null }, json: async () => ({}), text: async () => '',
    })))
    const onClose = vi.fn()
    render(
      <MemoryRouter><QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <MarkdownPanel embedded filePath="/proj/app.py" content={'print(1)\n'} onSave={async () => {}} onClose={onClose}
          railOpen browserRail={<div><input aria-label="Filter files" /><button type="button">Collapse</button></div>} />
        <textarea aria-label="Message" />
      </QueryClientProvider></MemoryRouter>,
    )
    // A clean, unwatched buffer closes without an await, so a close either of
    // these presses started would already have landed here.
    fireEvent.keyDown(await screen.findByTestId('pierre-editor'), { key: 'Escape' })
    fireEvent.keyDown(screen.getByLabelText('Filter files'), { key: 'Escape' })
    expect(onClose).not.toHaveBeenCalled()
    fireEvent.keyDown(screen.getByRole('button', { name: 'Collapse' }), { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
    fireEvent.keyDown(screen.getByLabelText('Message'), { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(2)
  })
})
