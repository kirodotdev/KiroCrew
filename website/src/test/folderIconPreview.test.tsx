/**
 * Folder settings: Auto-generate shows the icon before Save.
 *
 * Driven through the sidebar's `renderFolderModal`, so the wiring to
 * `api.previewChatFolderIcon` is what is tested, not only the component.
 * Without a preview callback the modal keeps arming `regenerate_icon` for
 * Save; FolderConfigModal.test.tsx covers that path.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { act, render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const mocks = vi.hoisted(() => ({
  previewChatFolderIcon: vi.fn(),
  listInstances: vi.fn().mockResolvedValue({ instances: [] }),
}))
vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy(mocks as Record<string, unknown>, {
    get: (t, p: string) => (p in t ? t[p] : vi.fn().mockResolvedValue({ dirs: [] })),
  }),
}))

import { renderFolderModal } from '../pages/chat-sidebar/folderModal'
import type { SidebarScope } from '../pages/chat-sidebar/scope'
import type { ChatFolder } from '../types'

const WORK = { id: 'f1', name: 'Work', order: 0, icon: '📁' } as ChatFolder

function deferred<T>() {
  let resolve!: (v: T) => void
  let reject!: (e: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

function openSettings() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const update = vi.fn().mockResolvedValue(WORK)
  const setFolderModal = vi.fn()
  const shell = {
    createFolderMutation: { mutateAsync: vi.fn() }, updateFolderMutation: { mutateAsync: update },
    defaultAgent: '', flatView: false, folders: [WORK], installedAgents: [], refetchTags: vi.fn(),
    setFlatView: vi.fn(), setFolderModal, tagsData: [], tagsQueryFailed: false,
  } as unknown as SidebarScope
  render(
    <QueryClientProvider client={qc}>
      {renderFolderModal(shell, { mode: 'edit', folderId: 'f1' })}
    </QueryClientProvider>,
  )
  return { update, setFolderModal }
}

const iconInput = () => screen.getByTestId('folder-config-icon') as HTMLInputElement
const autoGenerate = () => screen.getByTestId('folder-config-icon-regenerate')

describe('Folder settings: Auto-generate previews the icon', () => {
  beforeEach(() => { mocks.previewChatFolderIcon.mockReset() })

  it('puts the generated emoji in the field before Save, and Save stores it', async () => {
    mocks.previewChatFolderIcon.mockResolvedValueOnce({ icon: '💼' })
    const { update } = openSettings()
    fireEvent.click(autoGenerate())
    await waitFor(() => expect(iconInput().value).toBe('💼'))
    expect(mocks.previewChatFolderIcon).toHaveBeenCalledWith('f1', 'Work', ['📁'])
    expect(screen.getByText(/Not saved yet/)).toBeInTheDocument()
    expect(screen.queryByText(/after you save/)).toBeNull()
    expect(update).not.toHaveBeenCalled()
    fireEvent.click(screen.getByTestId('folder-config-submit'))
    await waitFor(() => expect(update).toHaveBeenCalled())
    expect(update).toHaveBeenCalledWith({ id: 'f1', body: { icon: '💼' } })
  })

  it('a second click asks for a different emoji', async () => {
    mocks.previewChatFolderIcon
      .mockResolvedValueOnce({ icon: '💼' })
      .mockResolvedValueOnce({ icon: '🗂️' })
    openSettings()
    fireEvent.click(autoGenerate())
    await waitFor(() => expect(iconInput().value).toBe('💼'))
    fireEvent.click(autoGenerate())
    await waitFor(() => expect(iconInput().value).toBe('🗂️'))
    expect(mocks.previewChatFolderIcon).toHaveBeenLastCalledWith('f1', 'Work', ['📁', '💼'])
  })

  it('shows a spinner on the button while it runs', async () => {
    const pending = deferred<{ icon: string }>()
    mocks.previewChatFolderIcon.mockReturnValueOnce(pending.promise)
    openSettings()
    fireEvent.click(autoGenerate())
    expect(await screen.findByTestId('folder-config-icon-generating')).toBeInTheDocument()
    expect(autoGenerate()).toBeDisabled()
    expect(autoGenerate()).toHaveAttribute('aria-busy', 'true')
    pending.resolve({ icon: '💼' })
    await waitFor(() => expect(screen.queryByTestId('folder-config-icon-generating')).toBeNull())
    expect(autoGenerate()).not.toBeDisabled()
  })

  it('says so in the modal when it fails, and keeps the old icon', async () => {
    mocks.previewChatFolderIcon.mockRejectedValueOnce(new Error('502'))
    openSettings()
    fireEvent.click(autoGenerate())
    expect(await screen.findByTestId('folder-config-icon-generate-error')).toBeInTheDocument()
    expect(iconInput().value).toBe('📁')
  })

  it('Cancel keeps the old icon: nothing is saved', async () => {
    mocks.previewChatFolderIcon.mockResolvedValueOnce({ icon: '💼' })
    const { update, setFolderModal } = openSettings()
    fireEvent.click(autoGenerate())
    await waitFor(() => expect(iconInput().value).toBe('💼'))
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(setFolderModal).toHaveBeenCalledWith(null)
    expect(update).not.toHaveBeenCalled()
  })

  it('holds Save while the pick runs, so Save never closes before it lands', async () => {
    const pending = deferred<{ icon: string }>()
    mocks.previewChatFolderIcon.mockReturnValueOnce(pending.promise)
    openSettings()
    fireEvent.click(autoGenerate())
    expect(screen.getByTestId('folder-config-submit')).toBeDisabled()
    await act(async () => { pending.resolve({ icon: '💼' }); await pending.promise })
    expect(screen.getByTestId('folder-config-submit')).not.toBeDisabled()
    expect(iconInput().value).toBe('💼')
  })

  it('a rename while it runs drops the pick made for the old name', async () => {
    const pending = deferred<{ icon: string }>()
    mocks.previewChatFolderIcon.mockReturnValueOnce(pending.promise)
    openSettings()
    fireEvent.click(autoGenerate())
    fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'Holidays' } })
    // Save is free again, and the button waits for the request to settle.
    expect(screen.getByTestId('folder-config-submit')).not.toBeDisabled()
    expect(autoGenerate()).toBeDisabled()
    await act(async () => { pending.resolve({ icon: '💼' }); await pending.promise })
    expect(iconInput().value).toBe('📁')
    expect(autoGenerate()).not.toBeDisabled()
  })

  it('an emoji typed while it runs wins over the late result', async () => {
    const pending = deferred<{ icon: string }>()
    mocks.previewChatFolderIcon.mockReturnValueOnce(pending.promise)
    openSettings()
    fireEvent.click(autoGenerate())
    fireEvent.change(iconInput(), { target: { value: '🧪' } })
    await act(async () => { pending.resolve({ icon: '💼' }); await pending.promise })
    expect(mocks.previewChatFolderIcon).toHaveBeenCalledTimes(1)
    expect(autoGenerate()).not.toBeDisabled()
    expect(iconInput().value).toBe('🧪')
  })
})
