import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'

import PortabilityTab, { isMemoryOnlyManifest, memoryShareItems } from './PortabilityTab'

function stubDownload(fetchMock: ReturnType<typeof vi.fn>) {
  vi.stubGlobal('fetch', fetchMock)
  vi.stubGlobal('URL', class extends URL {
    static createObjectURL = () => 'blob:x'
    static revokeObjectURL = () => {}
  })
}

const zipResponse = () => new Response(new Blob(['PK']), { status: 200 })

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('isMemoryOnlyManifest', () => {
  it('is true only for a manifest declaring exactly the memory component', () => {
    const base = { version: 2, created_at: 't', hostname: 'h', user: 'u', contents: {} }
    expect(isMemoryOnlyManifest({ ...base, components: ['memory'] })).toBe(true)
    expect(isMemoryOnlyManifest(base)).toBe(false)
    expect(isMemoryOnlyManifest({ ...base, components: ['memory', 'config'] })).toBe(false)
    expect(isMemoryOnlyManifest(null)).toBe(false)
  })
})

describe('PortabilityTab memory-only export', () => {
  it('asks first, listing what the file holds, and downloads only after yes', async () => {
    const fetchMock = vi.fn(async () => zipResponse())
    stubDownload(fetchMock)
    render(<PortabilityTab />)

    fireEvent.click(screen.getByRole('checkbox', { name: /export memory only/i }))
    // The card says what the button will do once memory only is ticked.
    expect(screen.queryByText(/Download all settings/)).toBeNull()
    expect(screen.getByText('Export memory', { selector: 'h3' })).toBeTruthy()
    expect(screen.getByText(/Download only memory as a zip file/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Download memory export (.zip)' }))

    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText('Export this memory?')).toBeTruthy()
    const items = within(dialog).getAllByTestId('portability-share-memory-item').map(n => n.textContent)
    expect(items).toEqual(memoryShareItems().map(i => `• ${i}`))
    expect(items.join(' ')).toContain('lessons')
    expect(within(dialog).getByTestId('portability-share-memory-body').textContent).toContain('Nothing is redacted')
    expect(fetchMock).not.toHaveBeenCalled()

    const yes = within(dialog).getByRole('button', { name: 'Export memory' })
    expect(yes.className).toContain('bg-accent')
    fireEvent.click(yes)
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/api/portability/export?components=memory'))
    expect(await screen.findByText('Download started.')).toBeTruthy()
  })

  it('downloads nothing when the user cancels', async () => {
    const fetchMock = vi.fn(async () => zipResponse())
    stubDownload(fetchMock)
    render(<PortabilityTab />)

    fireEvent.click(screen.getByRole('checkbox', { name: /export memory only/i }))
    fireEvent.click(screen.getByRole('button', { name: /download memory export/i }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: /cancel/i }))

    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('keeps the whole-install export unasked, and turns the chat box off while memory only is on', async () => {
    const fetchMock = vi.fn(async () => zipResponse())
    stubDownload(fetchMock)
    render(<PortabilityTab />)

    const chats = () => screen.getByRole('checkbox', { name: /include chat history/i }) as HTMLInputElement
    fireEvent.click(chats())
    fireEvent.click(screen.getByRole('checkbox', { name: /export memory only/i }))
    // A memory export never carries chats: the box stays, off and locked, and says why.
    expect(chats().checked).toBe(false)
    expect(chats().disabled).toBe(true)
    expect(screen.getByText(/Chat transcripts never go in a memory export/)).toBeTruthy()
    expect(screen.queryByText(/Adds every chat/)).toBeNull()
    fireEvent.click(screen.getByRole('checkbox', { name: /export memory only/i }))
    // It does not come back ticked on its own.
    expect(chats().checked).toBe(false)
    fireEvent.click(chats())

    fireEvent.click(screen.getByRole('button', { name: /download export/i }))
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/api/portability/export?include_sessions=true'))
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('shows a refused export as an error notice', async () => {
    stubDownload(vi.fn(async () => new Response(JSON.stringify({ error: 'Export failed', code: 'export_failed' }), { status: 500 })))
    render(<PortabilityTab />)

    fireEvent.click(screen.getByRole('button', { name: /download export/i }))

    const notice = await screen.findByTestId('portability-export-error')
    expect(notice.textContent).toContain('Export failed')
  })
})

describe('PortabilityTab memory-only import', () => {
  function stubImport(manifest: Record<string, unknown>) {
    const calls: string[] = []
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      calls.push(url)
      return new Response(JSON.stringify(
        url.includes('preview')
          ? { ok: true, manifest: { version: 2, created_at: 't', hostname: 'h', user: 'u', contents: {}, ...manifest } }
          : { ok: true, summary: { items: ['memory (merged)'], components: ['memory'] } },
      ), { status: 200 })
    }))
    return calls
  }

  async function chooseArchive() {
    const input = screen.getByLabelText(/choose import file/i) as HTMLInputElement
    fireEvent.change(input, { target: { files: [new File(['PK'], 'e.zip')] } })
    const importButton = screen.getByRole('button', { name: /^import$/i }) as HTMLButtonElement
    await waitFor(() => expect(importButton.disabled).toBe(false))
    return importButton
  }

  it('sends components=memory when the user picks memory only', async () => {
    const calls = stubImport({ contents: { 'config.json': 1024, 'memory.db': 2048, skill_count: 2, workspace_files: 5 } })
    render(<PortabilityTab />)
    const importButton = await chooseArchive()
    expect(screen.queryByTestId('portability-preview-skipped')).toBeNull()

    fireEvent.click(screen.getByRole('checkbox', { name: /import memory only/i }))
    // The preview now says which rows will not load.
    expect(screen.getAllByTestId('portability-preview-skipped').length).toBeGreaterThan(0)
    expect(screen.getByText('Workspace files: 5 (only memory and knowledge load)')).toBeTruthy()
    fireEvent.click(importButton)

    await screen.findByText(/Import complete/)
    expect(calls).toContain('/api/portability/import?mode=merge&components=memory')
  })

  it('imports a memory bundle with Merge and says why', async () => {
    const calls = stubImport({ components: ['memory'] })
    render(<PortabilityTab />)
    const importButton = await chooseArchive()

    expect(await screen.findByText('This archive holds memory only, so it imports with Merge.')).toBeTruthy()
    const box = screen.getByRole('checkbox', { name: /import memory only/i }) as HTMLInputElement
    expect(box.checked).toBe(true)
    expect(box.disabled).toBe(true)
    expect(screen.getByText('Import memory', { selector: 'h3' })).toBeTruthy()
    expect(screen.getByText(/Adds the memory in the file to this instance/)).toBeTruthy()
    expect(screen.queryByText(/Chats in the archive are added/)).toBeNull()
    // The option is read before the button that commits the import.
    expect(box.compareDocumentPosition(importButton) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    fireEvent.click(importButton)

    await screen.findByText(/Import complete/)
    expect(calls).toContain('/api/portability/import?mode=merge&components=memory')
  })

  it('imports the whole archive by default', async () => {
    const calls = stubImport({})
    render(<PortabilityTab />)
    fireEvent.click(await chooseArchive())

    await screen.findByText(/Import complete/)
    expect(calls).toContain('/api/portability/import?mode=merge')
  })
})
