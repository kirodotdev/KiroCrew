/**
 * `useChatSession` files an app's slot into a folder named after the app, through
 * the shared `ensureChatFolder` helper over the app's permission-scoped client.
 * Pinned here: the folder is reused wherever it exists in the tree, asked for by the
 * name the server will store, and created at the top level with the same `{ name }`
 * body the hook has always sent — the scoped client never learns a parent field.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import React from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const mockGet = vi.fn()
const mockPost = vi.fn()
const mockPatch = vi.fn()

// Stable reference: the hook's effect depends on `api`.
const stableApi = { get: mockGet, post: mockPost, patch: mockPatch }

vi.mock('../app-sdk/index', () => ({
  useAppApi: () => stableApi,
  useNavigate: () => vi.fn(),
}))

import { useChatSession, hashStr } from '../app-sdk/useChatSession'
import { storedName } from '../utils/ensureChatFolder'

const WS = '/ws/test'
const slotOf = (appName: string) => appName + '-' + hashStr(WS)
const folderPatchOf = (appName: string) => '/api/chat/slots/' + encodeURIComponent(slotOf(appName)) + '/folder'
const LONG_APP = 'w' + 'x'.repeat(110)
/** The name asked for on `LONG_APP`'s behalf: capitalised the way the hook names
 *  folders, then cut to the server's limit with the fingerprint tail. */
const LONG_STORED = storedName('W' + 'x'.repeat(110))

let queryClient: QueryClient
const wrapper = ({ children }: { children: React.ReactNode }) =>
  React.createElement(QueryClientProvider, { client: queryClient }, children)

/** One discovered slot for `appName` plus the given folder listing. */
function serve(folders: unknown, appName = 'workbench') {
  mockGet.mockImplementation((path: string) => {
    if (path === '/api/chat/slots') {
      return Promise.resolve([{ key: slotOf(appName), title: 'T', messages: 0, running: false }])
    }
    if (path === '/api/chat/folders') return Promise.resolve(folders)
    return Promise.resolve({})
  })
  mockPost.mockResolvedValue({ id: 'made', name: 'Workbench' })
  mockPatch.mockResolvedValue({})
}

const render = (appName = 'workbench') =>
  renderHook(() => useChatSession({ workspacePath: WS, label: 'L', appName }), { wrapper })

beforeEach(() => {
  mockGet.mockReset()
  mockPost.mockReset()
  mockPatch.mockReset()
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
})

describe('useChatSession folder assignment', () => {
  it('reuses an existing top-level folder without creating one', async () => {
    serve([{ id: 'f1', name: 'Workbench', parent_id: '' }])
    render()
    await waitFor(() => expect(mockPatch).toHaveBeenCalledWith(folderPatchOf('workbench'), { folder_id: 'f1' }))
    expect(mockPost).not.toHaveBeenCalled()
  })

  it('keeps finding the folder after the reader nests it under another one', async () => {
    // The base behaviour: matched by name anywhere, so a moved folder is not duplicated.
    serve([{ id: 'deep', name: 'Workbench', parent_id: 'somewhere' }])
    render()
    await waitFor(() => expect(mockPatch).toHaveBeenCalledWith(folderPatchOf('workbench'), { folder_id: 'deep' }))
    expect(mockPost).not.toHaveBeenCalled()
  })

  it('asks for the name the server will store when the app name is over-long', async () => {
    // The server keeps the first 100 characters; matching and creating with the
    // full name would make a fresh folder on every discovery.
    serve([], LONG_APP)
    render(LONG_APP)
    expect(LONG_STORED).toMatch(/^Wx{64} \([0-9a-f]{32}\)$/)
    await waitFor(() => expect(mockPost).toHaveBeenCalledWith('/api/chat/folders', { name: LONG_STORED }))
  })

  it('finds the clamped folder an earlier run left behind', async () => {
    serve([{ id: 'kept', name: LONG_STORED }], LONG_APP)
    render(LONG_APP)
    await waitFor(() => expect(mockPatch).toHaveBeenCalledWith(folderPatchOf(LONG_APP), { folder_id: 'kept' }))
    expect(mockPost).not.toHaveBeenCalled()
  })

  it('skips the assignment when the create answers without an id', async () => {
    serve([])
    mockPost.mockResolvedValue({})
    const { result } = render()
    await waitFor(() => expect(result.current.status).toBe('ready'))
    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1))
    expect(mockPatch).not.toHaveBeenCalled()
  })
})
