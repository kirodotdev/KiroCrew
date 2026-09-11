// Issue Radar's Investigate / Review sessions are titled at CREATION, not renamed
// afterwards. The server pins a title supplied on the create request and the
// create broadcast already carries it; a follow-up rename paints a generated title
// first, costs a round-trip, and — being best-effort — can drop the name silently
// and leave the auto-titler free to invent its own.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook } from '@testing-library/react'

const { dispatch, apiMock, saveInvestigation, getInvestigation } = vi.hoisted(() => ({
  dispatch: vi.fn(),
  apiMock: {
    chatFolders: vi.fn(),
    createChatFolder: vi.fn(),
    renameSlot: vi.fn(),
    sendChat: vi.fn(),
  },
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
vi.mock('../apps/issue-radar/api', () => ({ issueRadarApi: { saveInvestigation, getInvestigation } }))

import { useAgentSession } from '../apps/issue-radar/lib/agentSession'

const TITLE = '#4237 · session/new times out'

/** The options object the SUT handed `createSlot`. */
function createArg(): { folder_id?: string; title?: string; project?: string | null } | undefined {
  const call = dispatch.mock.calls
    .map((c) => c[0] as { type: string; arg?: { folder_id?: string; title?: string; project?: string | null } })
    .find((a) => a.type === 'createSlot')
  return call?.arg
}

async function open(workspacePath?: string) {
  const { result } = renderHook(() => useAgentSession())
  return result.current.openSession({
    repoRef: { host: 'github.com', owner: 'acme', repo: 'demo-repo' } as never,
    number: 4237,
    title: TITLE,
    prompt: 'seed',
    existing: null,
    workspacePath,
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
    getInvestigation.mockResolvedValue({ investigation: null })
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

  // The Investigate action forwards the repo's configured workspace_path as the
  // new slot's `project`, so the chat session opens in the repo's real working
  // copy instead of the gateway's default cwd -- the whole point of the setting.
  it('opens the session in the configured workspace path', async () => {
    await open('/Users/me/code/acme/demo-repo')
    expect(createArg()?.project).toBe('/Users/me/code/acme/demo-repo')
  })

  // No configured path must not pin a cwd: it passes `null` so `createSlot` skips
  // the chatSlotProject call and the slot keeps the gateway default (the
  // pre-workspace behavior). An empty string would be a real, wrong path.
  it('passes null when no workspace path is configured', async () => {
    await open()
    expect(createArg()?.project).toBeNull()
    await open('')
    expect(createArg()?.project).toBeNull()
  })

  // A stored workspace_path is never filesystem-validated, so the gateway can
  // reject it (missing dir on the gateway host, sensitive path) — createSlot then
  // deletes the slot and throws. That must degrade to a session on the default
  // cwd, not a dead Investigate button: retry once without the project.
  it('falls back to the default cwd when the configured path is rejected', async () => {
    const createArgs: Array<{ project?: string | null }> = []
    dispatch.mockImplementation((action: { type: string; arg?: { project?: string | null } }) => ({
      unwrap: () => {
        if (action.type !== 'createSlot') return Promise.resolve(undefined)
        createArgs.push(action.arg ?? {})
        // First attempt carries the (bad) project and is rejected; the retry
        // carries null and succeeds.
        return action.arg?.project
          ? Promise.reject(new Error('Not a directory'))
          : Promise.resolve({ key: 'slot-1' })
      },
    }))

    const rec = await open('/does/not/exist')
    // Two create attempts: the rejected one with the path, then the retry with null.
    expect(createArgs.map((a) => a.project)).toEqual(['/does/not/exist', null])
    // The session still opened (record linked), rather than the whole action failing.
    expect(rec).not.toBeNull()
  })
})
