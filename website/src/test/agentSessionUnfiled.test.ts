/**
 * A create can answer 200 with its session opened but NOT filed into the
 * requested folder: the answered `folder_id` is empty. Both app session hooks
 * (Issue Radar, Auto Improvement) then record the folder the session is actually
 * in ("" when unfiled) instead of the one they asked for, and still open the session.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, renderHook } from '@testing-library/react'

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
  let record: unknown = null
  await act(async () => {
    record = await result.current.openSession({
      repoRef: { host: 'github.com', owner: 'acme', repo: 'demo-repo' } as never,
      number: 1,
      title: '#1 · thing',
      prompt: 'seed',
      existing: null,
    })
  })
  return { record, result }
}

const openAutoImprove = async () => {
  const { result } = renderHook(() => useAutoImproveSession())
  let record: unknown = null
  await act(async () => {
    record = await result.current.openSession({ kind: 'pr', id: 1, repo: 'acme/demo-repo', title: 'PR #1', prompt: 'seed' })
  })
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

const recordedFolder = async (label: string): Promise<unknown> => {
  if (label === 'Issue Radar') return (saveInvestigation.mock.calls[0]?.[2] as { folder_id?: unknown })?.folder_id
  const put = (vi.mocked(fetch).mock.calls as [string, RequestInit | undefined][]).find(([, init]) => init?.method === 'PUT')
  return (JSON.parse(String(put?.[1]?.body ?? '{}')) as { folder_id?: unknown }).folder_id
}

function createAnswers(slot: Record<string, unknown>) {
  dispatch.mockImplementation((action: { type: string }) => ({
    unwrap: () => (action.type === 'createSlot' ? Promise.resolve(slot) : Promise.resolve(undefined)),
  }))
}

describe.each([
  ['Issue Radar', 'Issue Radar - demo-repo', openIssueRadar],
  ['Auto Improvement', 'Auto-Improve - acme/demo-repo', openAutoImprove],
] as const)('%s with a refused filing', (label, folderName, open) => {
  beforeEach(() => {
    apiMock.chatFolders.mockResolvedValue([{ id: 'f1', name: folderName, parent_id: '' }])
  })

  it('records the session outside the folder when the create could not file it', async () => {
    createAnswers({ key: 'slot-1', folder_id: '' })
    const { record } = await open()
    expect(record).not.toBeNull()
    expect(createdSlotArgs()[0]?.folder_id).toBe('f1')
    expect(await recordedFolder(label)).toBe('')
  })

  it('records the folder the session is in when it is not the requested one', async () => {
    createAnswers({ key: 'slot-1', folder_id: 'f-other' })
    const { record } = await open()
    expect(record).not.toBeNull()
    expect(await recordedFolder(label)).toBe('f-other')
  })

  it('records the requested folder when the create filed the session', async () => {
    createAnswers({ key: 'slot-1', folder_id: 'f1' })
    const { record } = await open()
    expect(record).not.toBeNull()
    expect(await recordedFolder(label)).toBe('f1')
  })
})
