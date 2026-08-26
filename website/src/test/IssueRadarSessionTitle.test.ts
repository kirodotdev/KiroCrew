// Issue Radar's Investigate / Review sessions are titled at CREATION, not renamed
// afterwards. The server pins a title supplied on the create request and the
// create broadcast already carries it; a follow-up rename paints a generated title
// first, costs a round-trip, and — being best-effort — can drop the name silently
// and leave the auto-titler free to invent its own.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook } from '@testing-library/react'

const { dispatch, apiMock, saveInvestigation } = vi.hoisted(() => ({
  dispatch: vi.fn(),
  apiMock: {
    chatFolders: vi.fn(),
    createChatFolder: vi.fn(),
    renameSlot: vi.fn(),
    sendChat: vi.fn(),
  },
  saveInvestigation: vi.fn(),
}))

vi.mock('../store', () => ({ useAppDispatch: () => dispatch }))
vi.mock('../store/chatSlice', () => ({
  createSlot: (arg: unknown) => ({ type: 'createSlot', arg }),
  switchSlot: (arg: unknown) => ({ type: 'switchSlot', arg }),
  deleteSlot: (arg: unknown) => ({ type: 'deleteSlot', arg }),
}))
vi.mock('react-router-dom', () => ({ useNavigate: () => vi.fn() }))
vi.mock('../api/client', () => ({ api: apiMock }))
vi.mock('../apps/issue-radar/api', () => ({ issueRadarApi: { saveInvestigation } }))

import { useAgentSession } from '../apps/issue-radar/lib/agentSession'

const TITLE = '#4237 · session/new times out'

/** The options object the SUT handed `createSlot`. */
function createArg(): { folder_id?: string; title?: string } | undefined {
  const call = dispatch.mock.calls
    .map((c) => c[0] as { type: string; arg?: { folder_id?: string; title?: string } })
    .find((a) => a.type === 'createSlot')
  return call?.arg
}

async function open() {
  const { result } = renderHook(() => useAgentSession())
  return result.current.openSession({
    repoRef: { host: 'github.com', owner: 'acme', repo: 'demo-repo' } as never,
    number: 4237,
    title: TITLE,
    prompt: 'seed',
    existing: null,
  })
}

describe('Issue Radar names the session it opens up front', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    dispatch.mockImplementation((action: { type: string }) => ({
      unwrap: () =>
        action.type === 'createSlot'
          ? Promise.resolve({ key: 'slot-1' })
          : Promise.resolve(undefined),
    }))
    apiMock.chatFolders.mockResolvedValue([{ id: 'repo-1', name: 'Issue Radar - demo-repo' }])
    apiMock.sendChat.mockResolvedValue({ ok: true })
    saveInvestigation.mockResolvedValue({ investigation: { slot_key: 'slot-1' } })
  })

  it('sends the title on the create request', async () => {
    await open()
    expect(createArg()).toMatchObject({ folder_id: 'repo-1', title: TITLE })
  })

  it('never follows the create with a rename', async () => {
    await open()
    expect(apiMock.renameSlot).not.toHaveBeenCalled()
  })

  it('still files the session into the one per-repo folder', async () => {
    await open()
    expect(apiMock.createChatFolder).not.toHaveBeenCalled()
    expect(createArg()?.folder_id).toBe('repo-1')
  })
})
