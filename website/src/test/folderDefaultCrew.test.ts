import { describe, it, expect } from 'vitest'
import { QueryClient } from '@tanstack/react-query'
import { routeFolderCrew, startChatOnCrew } from '../hooks/folderCrew'
import type { RootState } from '../store'
import type { ChatFolder } from '../types'

const api = vi.hoisted(() => ({ instancesCapabilities: vi.fn(), crewPeerPost: vi.fn() }))
vi.mock('../api/client', () => ({ api }))

const folder = (extra: Partial<ChatFolder> = {}): ChatFolder =>
  ({ id: 'f1', name: 'Billing', order: 0, ...extra }) as ChatFolder

const INSTANCES = [{ id: 'i-nobita', name: 'nobita' }]
const ctx = (over: Partial<Parameters<typeof routeFolderCrew>[1]> = {}) => ({
  previewOn: true,
  warm: { 'i-nobita': { port: 1, token: 't' } } as Record<string, unknown>,
  instances: INSTANCES,
  ...over,
})

describe('routeFolderCrew', () => {
  it('is Local for a folder with no default crew', () => {
    expect(routeFolderCrew(folder(), ctx())).toEqual({ kind: 'local' })
    expect(routeFolderCrew(folder({ default_crew: '' }), ctx())).toEqual({ kind: 'local' })
    expect(routeFolderCrew(folder({ default_crew: '   ' }), ctx())).toEqual({ kind: 'local' })
  })

  it('is Local for an unknown folder', () => {
    expect(routeFolderCrew(undefined, ctx())).toEqual({ kind: 'local' })
  })

  it('routes to the crew when it is connected, naming it from the registry', () => {
    expect(routeFolderCrew(folder({ default_crew: 'i-nobita' }), ctx())).toEqual({ kind: 'crew', id: 'i-nobita', name: 'nobita' })
  })

  it('reports offline, never Local, when the crew holds no live tunnel', () => {
    expect(routeFolderCrew(folder({ default_crew: 'i-nobita' }), ctx({ warm: { 'i-other': {} } })))
      .toEqual({ kind: 'offline', id: 'i-nobita', name: 'nobita', registered: true })
    expect(routeFolderCrew(folder({ default_crew: 'i-nobita' }), ctx({ warm: {} })).kind).toBe('offline')
  })

  it('treats a store with no instances slice as offline, not as a crash', () => {
    expect(routeFolderCrew(folder({ default_crew: 'i-nobita' }), ctx({ warm: undefined })).kind).toBe('offline')
  })

  it('marks a crew the loaded registry no longer lists as not registered, named by its id', () => {
    expect(routeFolderCrew(folder({ default_crew: 'i-gone' }), ctx({ instances: [] })))
      .toEqual({ kind: 'offline', id: 'i-gone', name: 'i-gone', registered: false })
  })

  it('does not call a crew unregistered while the registry has not loaded', () => {
    // Unknown is not "removed": the notice must not say the crew is gone.
    expect(routeFolderCrew(folder({ default_crew: 'i-nobita' }), ctx({ instances: undefined, warm: {} })))
      .toEqual({ kind: 'offline', id: 'i-nobita', name: 'i-nobita', registered: true })
  })

  it('is Local whatever the pin says while the Remote crew sessions preview is off', () => {
    expect(routeFolderCrew(folder({ default_crew: 'i-nobita' }), ctx({ previewOn: false }))).toEqual({ kind: 'local' })
    expect(routeFolderCrew(folder({ default_crew: 'i-nobita' }), ctx({ previewOn: false, warm: {} }))).toEqual({ kind: 'local' })
  })

  it('does not match a warm key by prototype name', () => {
    expect(routeFolderCrew(folder({ default_crew: 'toString' }), ctx()).kind).toBe('offline')
  })
})

describe('startChatOnCrew follows the fork rule', () => {
  // A crew create takes seconds (capabilities, then the peer's create). A user
  // who opened another session meanwhile, even one they then left again, made
  // a choice the late response must not undo: the chat is listed on the crew,
  // but no window opens over where they are.
  const chat = (activeSlot: string, moves: number) => ({ chat: { activeSlot, activeSlotMoves: moves, activeSlotAutoMoves: 0 } }) as unknown as RootState

  async function run(after: RootState) {
    let state = chat('origin', 3)
    api.instancesCapabilities.mockImplementation(async () => {
      state = after
      return { version_match: true, version: '1', local_version: '1' }
    })
    api.crewPeerPost.mockResolvedValue({ key: 'peer-1' })
    const onOpenPeerSession = vi.fn()
    await startChatOnCrew({ getState: () => state, queryClient: new QueryClient(), onOpenPeerSession }, 'i-nobita', 'temporary')
    return onOpenPeerSession
  }

  it('opens the new chat when the user never moved', async () => {
    expect(await run(chat('origin', 3))).toHaveBeenCalledWith('i-nobita', 'peer-1')
  })

  it('opens nothing when the user went to another session and came back', async () => {
    expect(await run(chat('origin', 5))).not.toHaveBeenCalled()
  })
})
