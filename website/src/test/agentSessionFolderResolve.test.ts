/**
 * Both app `resolveFolderId` copies (Issue Radar, Auto Improvement) go through the
 * shared `ensureChatFolder` helper over the host `api`, and each keeps the contract
 * it had: the repo folder is reused wherever it exists in the tree, created by
 * name on a miss, a rejected folder read surfaces as the hook's error UNTOUCHED
 * (no slot is created), and a create that answers without an id is an error rather
 * than a slot filed under `undefined`.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'

const { dispatch, apiMock, sendTurn, saveInvestigation, getInvestigation } = vi.hoisted(() => ({
  dispatch: vi.fn(),
  apiMock: {
    chatFolders: vi.fn(),
    createChatFolder: vi.fn(),
    chatSlotDetail: vi.fn(),
  },
  sendTurn: vi.fn(),
  saveInvestigation: vi.fn(),
  getInvestigation: vi.fn(),
}))

vi.mock('../store', () => ({ useAppDispatch: () => dispatch }))
vi.mock('../store/chatSlice', () => ({
  createSlot: (arg: unknown) => ({ type: 'createSlot', arg }),
  switchSlot: (arg: unknown) => ({ type: 'switchSlot', arg }),
  deleteSlot: (arg: unknown) => ({ type: 'deleteSlot', arg }),
}))
vi.mock('react-router-dom', () => ({ useNavigate: () => vi.fn() }))
vi.mock('../api/client', () => ({ api: apiMock }))
vi.mock('../chat-core/transport/sendTurn', () => ({ sendTurn }))
vi.mock('../apps/issue-radar/api', () => ({ issueRadarApi: { saveInvestigation, getInvestigation } }))

import { useAgentSession as useIssueRadarSession } from '../apps/issue-radar/lib/agentSession'
import { useAgentSession as useAutoImproveSession } from '../apps/auto-improvement/lib/agentSession'

const createdSlotArgs = () =>
  dispatch.mock.calls
    .map(c => c[0] as { type: string; arg?: { folder_id?: string } })
    .filter(a => a.type === 'createSlot')
    .map(a => a.arg)

function happyPath() {
  dispatch.mockImplementation((action: { type: string }) => ({
    unwrap: () => (action.type === 'createSlot' ? Promise.resolve({ key: 'slot-1' }) : Promise.resolve(undefined)),
  }))
  sendTurn.mockResolvedValue({ status: 'dispatched', body: {} })
  apiMock.createChatFolder.mockImplementation(async (name: string) => ({ id: 'made', name, parent_id: '' }))
}

const openIssueRadar = async () => {
  const { result } = renderHook(() => useIssueRadarSession())
  const record = await result.current.openSession({
    repoRef: { host: 'github.com', owner: 'acme', repo: 'demo-repo' } as never,
    number: 1,
    title: '#1 · thing',
    prompt: 'seed',
    existing: null,
  })
  return { record, result }
}

const openAutoImprove = async () => {
  const { result } = renderHook(() => useAutoImproveSession())
  const record = await result.current.openSession({ kind: 'pr', id: 1, repo: 'acme/demo-repo', title: 'PR #1', prompt: 'seed' })
  return { record, result }
}

beforeEach(() => {
  vi.resetAllMocks()
  happyPath()
  getInvestigation.mockResolvedValue({ investigation: null })
  saveInvestigation.mockResolvedValue({ investigation: { slot_key: 'slot-1' } })
  // Auto Improvement keeps its records behind a raw fetch: no record, then a saved one.
  vi.stubGlobal(
    'fetch',
    vi.fn(async (_url: string, init?: { method?: string }) =>
      init?.method === 'PUT'
        ? new Response(JSON.stringify({ session: { slot_key: 'slot-1' } }), { status: 200 })
        : new Response('', { status: 404 }),
    ),
  )
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe.each([
  ['Issue Radar', 'Issue Radar - demo-repo', openIssueRadar],
  ['Auto Improvement', 'Auto-Improve - acme/demo-repo', openAutoImprove],
] as const)('%s resolveFolderId', (_label, folderName, open) => {
  it('reuses the existing top-level repo folder', async () => {
    apiMock.chatFolders.mockResolvedValue([{ id: 'f1', name: folderName, parent_id: '' }])
    const { record } = await open()
    expect(record).not.toBeNull()
    expect(apiMock.createChatFolder).not.toHaveBeenCalled()
    expect(createdSlotArgs()[0]?.folder_id).toBe('f1')
  })

  it('keeps finding the repo folder after the reader nests it under another one', async () => {
    apiMock.chatFolders.mockResolvedValue([{ id: 'moved', name: folderName, parent_id: 'archive' }])
    const { record } = await open()
    expect(record).not.toBeNull()
    expect(apiMock.createChatFolder).not.toHaveBeenCalled()
    expect(createdSlotArgs()[0]?.folder_id).toBe('moved')
  })

  it('creates the repo folder by name on a miss and files the slot into it', async () => {
    apiMock.chatFolders.mockResolvedValue([{ id: 'other', name: 'Something else', parent_id: '' }])
    const { record } = await open()
    expect(record).not.toBeNull()
    expect(apiMock.createChatFolder).toHaveBeenCalledTimes(1)
    expect(apiMock.createChatFolder.mock.calls[0][0]).toBe(folderName)
    expect(createdSlotArgs()[0]?.folder_id).toBe('made')
  })

  it('surfaces a rejected folder read as the hook error, untouched, and creates no slot', async () => {
    const boom = new Error('HTTP 503: folders unavailable')
    apiMock.chatFolders.mockRejectedValue(boom)
    const { record, result } = await open()
    expect(record).toBeNull()
    await waitFor(() => expect(result.current.error).toBe(boom))
    expect(createdSlotArgs()).toEqual([])
  })

  it('treats a create that answers without an id as a failure rather than filing under undefined', async () => {
    apiMock.chatFolders.mockResolvedValue([])
    apiMock.createChatFolder.mockResolvedValue({})
    const { record, result } = await open()
    expect(record).toBeNull()
    await waitFor(() => expect(result.current.error).toBeInstanceOf(Error))
    expect(createdSlotArgs()).toEqual([])
  })
})
