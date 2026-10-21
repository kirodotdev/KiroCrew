import { configureStore } from '@reduxjs/toolkit'

/* Only a create that opts in (`defaultFolder: true`, the plain New entry
 * points) asks the server for the person's default sidebar folder, and an
 * explicit folder or carried project on the same call still wins. */

const api = vi.hoisted(() => ({
  createChatSlot: vi.fn(),
  chatSlotProject: vi.fn(() => Promise.resolve({ ok: true })),
  deleteChatSlot: vi.fn(() => Promise.resolve({ ok: true })),
  setSlotColor: vi.fn(() => Promise.resolve({ ok: true })),
  dashboardConfig: vi.fn(() => Promise.resolve({ default_memory_mode: 'persistent' })),
}))

vi.mock('../lib/scrollQuiet', () => ({ whenScrollQuiet: () => Promise.resolve() }))
vi.mock('../api/client', () => ({ api }))

import dashboardReducer from './dashboardSlice'
import chatReducer, { createSlot } from './chatSlice'

// api.createChatSlot(name, agent, model, mode, memory_mode, title, artifact,
//   folder_id, agent_kind, no_session_folder, app_agent_owner, default_folder)
const ARG_DEFAULT_FOLDER = 11

const makeStore = () => configureStore({
  reducer: { chat: chatReducer, dashboard: dashboardReducer },
  middleware: g => g({ serializableCheck: false, immutableCheck: false }),
})

describe('createSlot default folder', () => {
  beforeEach(() => {
    api.createChatSlot.mockReset()
    api.createChatSlot.mockResolvedValue({ key: 'NEW', title: 'New' })
  })

  it.each([
    ['the opt-in alone', { defaultFolder: true }],
    ['the opt-in with a mode and memory mode', { defaultFolder: true, mode: 'plan', memory_mode: 'incognito' }],
  ])('asks for it with %s', async (_label, opts) => {
    await makeStore().dispatch(createSlot(opts))
    expect(api.createChatSlot.mock.calls[0][ARG_DEFAULT_FOLDER]).toBe(true)
  })

  // Opt-in: a create path that has not chosen the default (an app launch,
  // a hand-off, a recreated slot) stays unfiled rather than moving into the
  // person's folder and running in its project.
  it.each([
    ['no options', undefined],
    ['an agent name', 'crew-member'],
    ['options without the opt-in', { mode: 'plan', agent: 'app-agent' }],
    ['an explicit folder', { defaultFolder: true, folder_id: 'work' }],
    ['an explicit unfiled recreate', { defaultFolder: true, folder_id: null }],
    ['a carried project', { defaultFolder: true, project: '/repo/worktree' }],
  ])('does not ask for it with %s', async (_label, opts) => {
    await makeStore().dispatch(createSlot(opts as Parameters<typeof createSlot>[0]))
    expect(api.createChatSlot.mock.calls[0][ARG_DEFAULT_FOLDER]).toBe(false)
  })
})
