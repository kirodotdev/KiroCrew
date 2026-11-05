/**
 * Alt+Shift+N files the new chat in the active chat's folder (#16041).
 *
 *   - Active chat in a folder: the create names that folder, the folder's
 *     pinned agent, and its project directory, like the folder's own "+".
 *   - Active chat unfiled: the create asks for the default folder, as before.
 *   - Folder no longer listed: the plain default-folder create.
 *   - Folder a mate manages (its home or inside it): the default-folder create.
 *   - The folder's project directory is gone: the shell notice names the path.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { renderHook, fireEvent, act, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import type { ChatFolder, ChatSlot } from '../types'

vi.mock('react-router-dom', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  useNavigate: () => vi.fn(),
}))

const mocks = vi.hoisted(() => ({ createChatSlot: vi.fn(), chatSlotProject: vi.fn() }))
vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy(mocks as Record<string, unknown>, {
    get: (target, prop: string) => (prop in target ? target[prop] : vi.fn().mockResolvedValue([])),
  }),
}))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: '' }) }))
vi.mock('../hooks/useInstanceShortcuts', () => ({ useInstanceShortcuts: () => {} }))
vi.mock('../hooks/useAutoConnectInstances', () => ({ useAutoConnectInstances: () => {} }))
vi.mock('../hooks/useCommandPalette', () => ({ useCommandPalette: () => ({ open: false, setOpen: () => {} }) }))
vi.mock('../providers/context', () => ({ useProvider: () => ({}) }))
vi.mock('../pages/chat/composerFocus', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  queryComposerOrExpand: vi.fn(),
}))

import { useShellKeyboard } from '../shell/shortcuts/shellKeyboard'
import { ApiError } from '../api/apiError'

const FOLDERS: ChatFolder[] = [
  { id: 'f-parent', name: 'Parent', order: 0, project_dir: '/repo/app', default_agent: 'reviewer' },
  { id: 'f-child', name: 'Child', order: 0, parent_id: 'f-parent' },
]

function harness(activeFolder: string | undefined, folders: ChatFolder[] | undefined = FOLDERS) {
  const defaults = createTestStore().getState()
  const slots = [{ key: 'chat-a', title: 'A', messages: 1, running: false, folder_id: activeFolder }] as ChatSlot[]
  const store = createTestStore({
    dashboard: { ...defaults.dashboard, slots, slotsLoaded: true },
    chat: { ...defaults.chat, activeSlot: 'chat-a' },
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  if (folders) qc.setQueryData(['chat-folders'], folders)
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}><Provider store={store}><MemoryRouter>{children}</MemoryRouter></Provider></QueryClientProvider>
  )
  renderHook(() => useShellKeyboard({
    toggleFocusMode: () => {}, toggleNav: () => {}, terminalEnabled: false, isPopout: false,
    isEmbed: false, terminalPoppedOut: false, activeSlotProject: undefined,
  }), { wrapper })
  return store
}
const press = () => fireEvent.keyDown(document, { code: 'KeyN', altKey: true, shiftKey: true })
// createChatSlot(_, agent, model, mode, memory, title, _, folderId, kind, skipSessionFolder, owner, useDefaultFolder)
const call = () => mocks.createChatSlot.mock.calls[0]

beforeEach(() => {
  localStorage.clear()
  window.history.pushState(null, '', '/chat?sid=chat-a')
  mocks.createChatSlot.mockReset().mockResolvedValue({ key: 'chat-new', folder_id: 'f-child' })
  mocks.chatSlotProject.mockReset().mockResolvedValue({})
})

describe('Alt+Shift+N in a folder', () => {
  it('creates the chat in the active chat folder with its agent and project', async () => {
    harness('f-child')
    press()
    await waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalledTimes(1))
    expect(call()[1]).toBe('reviewer')
    expect(call()[7]).toBe('f-child')
    expect(call()[11]).toBe(false)
    await waitFor(() => expect(mocks.chatSlotProject).toHaveBeenCalledWith('chat-new', '/repo/app'))
  })

  it('keeps the default-folder create for an unfiled active chat', async () => {
    harness(undefined)
    press()
    await waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalledTimes(1))
    expect(call()[7]).toBeUndefined()
    expect(call()[11]).toBe(true)
  })

  it('keeps the default-folder create when the folder is no longer listed', async () => {
    harness('f-gone')
    press()
    await waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalledTimes(1))
    expect(call()[7]).toBeUndefined()
    expect(call()[11]).toBe(true)
  })

  it.each(['f-home', 'f-mate-sub'])('keeps the default-folder create in a mate-managed folder (%s)', async (folder) => {
    const mateFolders: ChatFolder[] = [
      ...FOLDERS,
      { id: 'f-home', name: 'Radar', order: 0, mate_home: true },
      { id: 'f-mate-sub', name: 'PR review', order: 0, parent_id: 'f-home' },
    ]
    harness(folder, mateFolders)
    press()
    await waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalledTimes(1))
    expect(call()[7]).toBeUndefined()
    expect(call()[11]).toBe(true)
  })

  it('names the missing project directory in the shell notice', async () => {
    mocks.createChatSlot.mockResolvedValue({ key: 'chat-new', folder_id: 'f-child' })
    mocks.chatSlotProject.mockRejectedValue(new ApiError(400, 'Not a directory'))
    const store = harness('f-child')
    press()
    await waitFor(() => expect(store.getState().chat.agentSwitchNotice?.message).toContain('/repo/app'))
    await act(async () => {})
  })

  it('names the missing project directory when the create endpoint refuses the folder', async () => {
    // POST /api/chat/slots answers folder_project_invalid before the slot exists.
    mocks.createChatSlot.mockRejectedValue(new ApiError(400, 'invalid folder project: Project directory must be an existing directory'))
    const store = harness('f-child')
    press()
    await waitFor(() => expect(store.getState().chat.agentSwitchNotice?.message).toContain('/repo/app'))
    expect(mocks.chatSlotProject).not.toHaveBeenCalled()
  })
})
