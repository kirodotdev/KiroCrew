/**
 * Settings ▸ Chat ▸ Default Sidebar Folder for New Chats.
 *
 * Load-bearing assertions:
 *   (1) the list is None plus the person's own folders by path; an app's
 *       folder is not offered;
 *   (2) picking a folder saves its ID, and picking None clears it;
 *   (3) a saved folder that was since deleted stays visible as its own option
 *       with a hint that new chats are unfiled, so the stored value is honest.
 *
 * The touch path renders a native <select>, which jsdom can drive directly.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ChatFolder } from '../types'

vi.mock('../hooks/useIsTouchDevice', () => ({ useIsTouchDevice: () => true }))

const server = vi.hoisted(() => ({ folder: '', folders: [] as ChatFolder[], folderSort: 'custom' }))
const mocks = vi.hoisted(() => ({
  updateDashboardConfig: vi.fn(),
  chatFoldersImpl: null as null | (() => Promise<unknown>),
}))
vi.mock('../api/client', () => ({
  api: {
    dashboardConfig: () => Promise.resolve({ default_session_folder: server.folder }),
    chatFolders: () => (mocks.chatFoldersImpl ? mocks.chatFoldersImpl() : Promise.resolve(server.folders)),
    updateDashboardConfig: mocks.updateDashboardConfig,
    kirocrewConfig: () => Promise.resolve({ dashboard: { folder_sort: server.folderSort } }),
  },
}))

import { DefaultSessionFolderSetting } from '../pages/settings/DefaultSessionFolderSetting'

const FOLDERS: ChatFolder[] = [
  { id: 'work', name: 'Work', order: 0 },
  { id: 'inbox', name: 'Inbox', order: 0, parent_id: 'work' },
  { id: 'app', name: 'App folder', order: 1, owner_app: 'some-app' },
]

function renderSetting() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={qc}><DefaultSessionFolderSetting /></QueryClientProvider>)
}

async function select(): Promise<HTMLSelectElement> {
  const el = await screen.findByRole('combobox', { name: 'Default Sidebar Folder for New Chats' }) as HTMLSelectElement
  await waitFor(() => expect(el).not.toBeDisabled())
  return el
}

const optionTexts = (el: HTMLSelectElement) =>
  Array.from(el.options).filter(o => !o.hidden).map(o => o.textContent)

beforeEach(() => {
  server.folder = ''
  server.folders = FOLDERS
  server.folderSort = 'custom'
  mocks.chatFoldersImpl = null
  mocks.updateDashboardConfig.mockReset()
  mocks.updateDashboardConfig.mockImplementation((patch: { default_session_folder: string }) => {
    server.folder = patch.default_session_folder
    return Promise.resolve({ ok: true })
  })
})

describe('DefaultSessionFolderSetting', () => {
  it("lists None and the person's folders by path", async () => {
    renderSetting()
    const el = await select()
    await waitFor(() => expect(optionTexts(el)).toEqual(['None (unfiled)', 'Work', 'Work › Inbox']))
    expect(el.value).not.toBe('app')
  })

  it('saves a pick and clears with None', async () => {
    renderSetting()
    const el = await select()
    await waitFor(() => expect(optionTexts(el)).toContain('Work › Inbox'))
    fireEvent.change(el, { target: { value: 'inbox' } })
    await waitFor(() => expect(mocks.updateDashboardConfig).toHaveBeenCalledWith({ default_session_folder: 'inbox' }))
    await waitFor(() => expect(el).not.toBeDisabled())
    expect(el.value).toBe('inbox')
    const none = Array.from(el.options).find(o => o.textContent === 'None (unfiled)')!
    fireEvent.change(el, { target: { value: none.value } })
    await waitFor(() => expect(mocks.updateDashboardConfig).toHaveBeenLastCalledWith({ default_session_folder: '' }))
  })

  it('reports a folder-list failure and retries it', async () => {
    let calls = 0
    server.folders = FOLDERS
    const original = server.folders
    mocks.chatFoldersImpl = () => {
      calls += 1
      return calls === 1 ? Promise.reject(new Error('down')) : Promise.resolve(original)
    }
    renderSetting()
    expect(await screen.findByText('Your sidebar folders could not be loaded, so they are not listed here.')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    const el = await select()
    await waitFor(() => expect(optionTexts(el)).toContain('Work › Inbox'))
    expect(screen.queryByText(/could not be loaded/)).toBeNull()
  })

  it('numbers folders that share a path so the options differ', async () => {
    server.folders = [
      { id: 'a', name: 'Work', order: 0 },
      { id: 'b', name: 'Work', order: 1 },
    ]
    renderSetting()
    const el = await select()
    await waitFor(() => expect(optionTexts(el)).toEqual(['None (unfiled)', 'Work', 'Work (2)']))
  })

  it("orders folders the way the sidebar's folder sort does", async () => {
    // Stored order puts Zeta first; By name must put Alpha first, as the
    // sidebar and the other folder pickers do.
    server.folders = [
      { id: 'z', name: 'Zeta', order: 0 },
      { id: 'a', name: 'Alpha', order: 1 },
    ]
    server.folderSort = 'name'
    renderSetting()
    const el = await select()
    await waitFor(() => expect(optionTexts(el)).toEqual(['None (unfiled)', 'Alpha', 'Zeta']))
  })

  it('shows a deleted default as its own option with a hint', async () => {
    server.folder = 'gone'
    renderSetting()
    const el = await select()
    await waitFor(() => expect(optionTexts(el)).toContain('Deleted folder'))
    expect(el.value).toBe('gone')
    expect(screen.getByText(/was deleted, so new chats are unfiled/)).toBeTruthy()
  })
})
