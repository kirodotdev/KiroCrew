import { configureStore } from '@reduxjs/toolkit'

/* `dashboard.new_chat_crew_mode`: a create that opts in (`crewModeDefault:
 * true`, the person's own New chat entry points) turns conductor mode on the way the
 * composer chip does, by the ordinary agent switch to the conductor after the
 * create, and only for a chat the chip would be offered on. The create request
 * itself is unchanged. */

const api = vi.hoisted(() => ({
  createChatSlot: vi.fn(),
  chatSlotAgent: vi.fn(),
  agentCatalog: vi.fn(),
  chatSlotProject: vi.fn(() => Promise.resolve({ ok: true })),
  deleteChatSlot: vi.fn(() => Promise.resolve({ ok: true })),
  setSlotColor: vi.fn(() => Promise.resolve({ ok: true })),
  dashboardConfig: vi.fn(),
}))

vi.mock('../lib/scrollQuiet', () => ({ whenScrollQuiet: () => Promise.resolve() }))
vi.mock('../api/client', () => ({ api }))

import dashboardReducer, { addSlotOptimistic } from './dashboardSlice'
import chatReducer, { createSlot } from './chatSlice'
import { resetCrewModeCatalog, setCrewModeCatalog } from '../lib/crewMode'
import { turnOffCrewModeAsLts } from '../test/crewModeOff'
import { FEATURE_PREVIEW_LOCK_WAIT_MS, __setFeaturePreviewLockKnownForTests } from '../utils/editionLocks'

const CONDUCTOR = 'kirocrew-conductor'
const CATALOG = { agents: [], default_agent: 'kirocrew', crew_mode_agent: CONDUCTOR, conductor_agents: [CONDUCTOR] }

const makeStore = () => configureStore({
  reducer: { chat: chatReducer, dashboard: dashboardReducer },
  middleware: g => g({ serializableCheck: false, immutableCheck: false }),
})

/** The slot the create resolves with, as the thunk publishes it. */
const created = async (opts: Parameters<typeof createSlot>[0], row: Record<string, unknown> = {}) => {
  api.createChatSlot.mockResolvedValue({ key: 'NEW', title: 'New', agent: 'kirocrew', agent_kind: 'template', memory_mode: 'persistent', ...row })
  return makeStore().dispatch(createSlot(opts)).unwrap()
}

describe('createSlot new chat conductor mode default', () => {
  beforeEach(() => {
    api.createChatSlot.mockReset()
    api.chatSlotAgent.mockReset()
    api.chatSlotAgent.mockResolvedValue({ ok: true, agent: CONDUCTOR, agent_kind: 'template' })
    api.agentCatalog.mockReset()
    api.agentCatalog.mockResolvedValue(CATALOG)
    api.dashboardConfig.mockReset()
    api.dashboardConfig.mockResolvedValue({ default_memory_mode: 'persistent', new_chat_crew_mode: true })
    setCrewModeCatalog(CONDUCTOR, [CONDUCTOR], 'kirocrew')
  })
  afterEach(() => {
    resetCrewModeCatalog()
  })

  it('switches an opted-in New chat to the conductor and opens it switched', async () => {
    const slot = await created({ crewModeDefault: true, defaultFolder: true })
    expect(api.createChatSlot.mock.calls[0][1]).toBeUndefined()
    expect(api.chatSlotAgent).toHaveBeenCalledWith('NEW', CONDUCTOR, 'template')
    expect(slot.agent).toBe(CONDUCTOR)
  })

  it('reads a catalog that has not landed yet before deciding (the page\'s first chat)', async () => {
    resetCrewModeCatalog()
    await created({ crewModeDefault: true, automatic: true })
    expect(api.agentCatalog).toHaveBeenCalledWith('NEW')
    expect(api.chatSlotAgent).toHaveBeenCalledWith('NEW', CONDUCTOR, 'template')
  })

  it('reads the setting when an explicit memory mode skipped the config read', async () => {
    await created({ crewModeDefault: true, memory_mode: 'persistent' })
    expect(api.dashboardConfig).toHaveBeenCalledTimes(1)
    expect(api.chatSlotAgent).toHaveBeenCalled()
  })

  it.each([
    ['no opt-in', { defaultFolder: true }, {}],
    ['a folder pin the server applied', { crewModeDefault: true, defaultFolder: true }, { agent: 'reviewer' }],
    ['a crewmate named like the default', { crewModeDefault: true }, { agent_kind: 'member' }],
    ['an Incognito chat', { crewModeDefault: true, memory_mode: 'incognito' }, { memory_mode: 'incognito' }],
    ['a chat mode', { crewModeDefault: true, mode: 'plan' }, { mode: 'plan' }],
  ])('leaves %s plain', async (_label, opts, row) => {
    await created(opts as Parameters<typeof createSlot>[0], row)
    expect(api.chatSlotAgent).not.toHaveBeenCalled()
  })

  it('leaves the chat plain while the setting is off', async () => {
    api.dashboardConfig.mockResolvedValue({ default_memory_mode: 'persistent', new_chat_crew_mode: false })
    await created({ crewModeDefault: true })
    expect(api.chatSlotAgent).not.toHaveBeenCalled()
  })

  it('waits for the edition lock before deciding, then switches once a standard build answers', async () => {
    vi.useFakeTimers()
    onTestFinished(() => { vi.useRealTimers() })
    __setFeaturePreviewLockKnownForTests(false)
    const pending = created({ crewModeDefault: true })
    // Everything but the lock has answered; the decision is still waiting on it.
    await vi.advanceTimersByTimeAsync(FEATURE_PREVIEW_LOCK_WAIT_MS - 1)
    expect(api.createChatSlot).toHaveBeenCalled()
    expect(api.chatSlotAgent).not.toHaveBeenCalled()
    __setFeaturePreviewLockKnownForTests(true)
    await pending
    expect(api.chatSlotAgent).toHaveBeenCalledWith('NEW', CONDUCTOR, 'template')
  })

  it('leaves the chat plain when the edition lock never arrives within the wait', async () => {
    vi.useFakeTimers()
    onTestFinished(() => { vi.useRealTimers() })
    __setFeaturePreviewLockKnownForTests(false)
    const pending = created({ crewModeDefault: true })
    await vi.advanceTimersByTimeAsync(FEATURE_PREVIEW_LOCK_WAIT_MS)
    await pending
    expect(api.chatSlotAgent).not.toHaveBeenCalled()
  })

  it('leaves the chat plain in a build without conductor mode (LTS)', async () => {
    turnOffCrewModeAsLts()
    await created({ crewModeDefault: true })
    expect(api.chatSlotAgent).not.toHaveBeenCalled()
  })

  it('leaves the chat plain on a gateway without Crew Mode', async () => {
    setCrewModeCatalog(undefined, undefined, 'kirocrew')
    await created({ crewModeDefault: true })
    expect(api.chatSlotAgent).not.toHaveBeenCalled()
  })

  it('leaves the chat plain when the default agent is itself a conductor', async () => {
    setCrewModeCatalog(CONDUCTOR, [CONDUCTOR, 'team-lead'], 'team-lead')
    await created({ crewModeDefault: true }, { agent: 'team-lead' })
    expect(api.chatSlotAgent).not.toHaveBeenCalled()
  })

  it('puts the switch on a row the create broadcast already listed plain', async () => {
    const store = makeStore()
    const plain = { key: 'NEW', title: 'New', agent: 'kirocrew', agent_kind: 'template' as const, memory_mode: 'persistent', messages: 0, running: false }
    // The slots frame lists the plain row before the create answers; no switch
    // broadcast follows (the socket dropped), so only the thunk can fix the row.
    api.createChatSlot.mockImplementation(async () => {
      store.dispatch(addSlotOptimistic(plain))
      return { ...plain }
    })
    await store.dispatch(createSlot({ crewModeDefault: true })).unwrap()
    const row = store.getState().dashboard.slots.find(s => s.key === 'NEW')
    expect(row?.agent).toBe(CONDUCTOR)
    expect(row?.agent_kind).toBe('template')
  })

  it('keeps the created chat when the switch is refused', async () => {
    api.chatSlotAgent.mockRejectedValue(new Error('409'))
    const slot = await created({ crewModeDefault: true })
    expect(slot.key).toBe('NEW')
    expect(slot.agent).toBe('kirocrew')
  })
})
