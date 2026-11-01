/**
 * #13598 — FolderNameInput (rename) and FolderRow: closing the inline editor
 * never strands focus on document.body.
 *
 * Enter and Escape return focus to the folder row (tabIndex={-1} makes it
 * programmatically focusable). A pointer blur moves focus nowhere.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { createConnectedTestStore } from './helpers'

vi.mock('../api/client', () => ({
  api: new Proxy({} as Record<string, unknown>, {
    get: () => vi.fn().mockResolvedValue({ ok: true }),
  }),
}))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  })),
})

import { DndContext } from '@dnd-kit/core'
import { FolderNameInput, FolderRow } from '../components/library/LibraryTable'
import type { ArtifactFolder, FolderActions } from '../types'

const FOLDER: ArtifactFolder = {
  id: 'f1', name: 'Alpha', parent_id: '', color: '', icon: '', collapsed: false,
}
const FOLDER_ACTIONS: FolderActions = {
  onOpen: vi.fn(),
  onRename: vi.fn(),
  onMove: vi.fn(),
  onDelete: vi.fn(),
  onSetColor: vi.fn(),
  renamingId: 'f1',
  onRenameSubmit: vi.fn(),
  onRenameCancel: vi.fn(),
}

const nextFrame = () => act(async () => { await new Promise(requestAnimationFrame) })

// ── FolderNameInput standalone ───────────────────────────────────────────────
// FolderNameInput uses useConnected() which reads from the Redux store.
function renderFolderNameInput(props: Parameters<typeof FolderNameInput>[0]) {
  // connected:true required: the offline guard in onBlur returns early when
  // connected=false, skipping the onClose callback and stranding focus.
  const store = createConnectedTestStore()
  return render(<Provider store={store}><FolderNameInput {...props} /></Provider>)
}

describe('FolderNameInput close-reason contract (#13598)', () => {
  it('onClose receives "enter" when Enter commits', () => {
    const onClose = vi.fn()
    const onCommit = vi.fn()
    renderFolderNameInput({ initial: "Alpha", onCommit, onCancel: vi.fn(), onClose })
    const input = screen.getByRole('textbox') as HTMLInputElement
    act(() => { fireEvent.change(input, { target: { value: 'Beta' } }) })
    act(() => { fireEvent.keyDown(input, { key: 'Enter' }) })
    act(() => { fireEvent.blur(input) })
    expect(onCommit).toHaveBeenCalledWith('Beta')
    expect(onClose).toHaveBeenCalledWith('enter')
  })

  it('onClose receives "escape" when Escape cancels', () => {
    const onClose = vi.fn()
    const onCancel = vi.fn()
    renderFolderNameInput({ initial: "Alpha", onCommit: vi.fn(), onCancel, onClose })
    const input = screen.getByRole('textbox') as HTMLInputElement
    act(() => { fireEvent.keyDown(input, { key: 'Escape' }) })
    expect(onCancel).toHaveBeenCalled()
    expect(onClose).toHaveBeenCalledWith('escape')
  })

  it('onClose receives "blur" on a pointer blur', () => {
    const onClose = vi.fn()
    renderFolderNameInput({ initial: "Alpha", onCommit: vi.fn(), onCancel: vi.fn(), onClose })
    const input = screen.getByRole('textbox') as HTMLInputElement
    act(() => { fireEvent.blur(input) })
    expect(onClose).toHaveBeenCalledWith('blur')
  })

  it('without onClose prop the component still works (create use-case)', () => {
    const onCommit = vi.fn()
    renderFolderNameInput({ initial: "", placeholder: "New folder", onCommit, onCancel: vi.fn() })
    const input = screen.getByRole('textbox') as HTMLInputElement
    act(() => { fireEvent.change(input, { target: { value: 'New' } }) })
    act(() => { fireEvent.keyDown(input, { key: 'Enter' }) })
    act(() => { fireEvent.blur(input) })
    expect(onCommit).toHaveBeenCalledWith('New')
  })
})

// ── FolderRow: focus restoration ────────────────────────────────────────────
function renderFolderRow() {
  // connected:true required: same offline guard as FolderNameInput (see above).
  const store = createConnectedTestStore()
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  // DnD context required by FolderRow; stub with a plain wrapper.
  // Redux Provider required because FolderNameInput uses useConnected().
  const view = render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <DndContext>
          <table>
            <tbody>
              <FolderRow
                folder={FOLDER}
                folders={[FOLDER]}
                depth={0}
                expanded={false}
                onToggle={vi.fn()}
                actions={FOLDER_ACTIONS}
              />
            </tbody>
          </table>
          <button type="button" data-testid="elsewhere">Elsewhere</button>
        </DndContext>
      </QueryClientProvider>
    </Provider>,
  )
  return view
}

describe('FolderRow rename focus contract (#13598)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    // actions.renamingId = FOLDER.id causes FolderNameInput to render
    FOLDER_ACTIONS.renamingId = 'f1'
  })

  it('Enter returns focus to the folder row', async () => {
    renderFolderRow()
    const input = screen.getByRole('textbox') as HTMLInputElement
    act(() => { fireEvent.change(input, { target: { value: 'Renamed' } }) })
    act(() => { fireEvent.keyDown(input, { key: 'Enter' }) })
    act(() => { fireEvent.blur(input) })
    await nextFrame()
    await nextFrame()
    const row = document.querySelector('tr') as HTMLElement
    expect(document.activeElement).toBe(row)
    expect(document.activeElement).not.toBe(document.body)
  })

  it('Escape returns focus to the folder row', async () => {
    renderFolderRow()
    const input = screen.getByRole('textbox') as HTMLInputElement
    act(() => { fireEvent.keyDown(input, { key: 'Escape' }) })
    await nextFrame()
    await nextFrame()
    const row = document.querySelector('tr') as HTMLElement
    expect(document.activeElement).toBe(row)
    expect(document.activeElement).not.toBe(document.body)
  })

  it('pointer blur does not redirect focus', async () => {
    renderFolderRow()
    const input = screen.getByRole('textbox') as HTMLInputElement
    const elsewhere = screen.getByTestId('elsewhere')
    act(() => { elsewhere.focus() })
    act(() => { fireEvent.blur(input) })
    await nextFrame()
    await nextFrame()
    expect(document.activeElement).toBe(elsewhere)
  })
})
