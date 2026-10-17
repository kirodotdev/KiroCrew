import { describe, it, expect } from 'vitest'
import { routeFolderCrew } from '../pages/chat-sidebar/folderCrew'
import type { ChatFolder } from '../types'

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
      .toEqual({ kind: 'offline', id: 'i-nobita', name: 'nobita' })
    expect(routeFolderCrew(folder({ default_crew: 'i-nobita' }), ctx({ warm: {} })).kind).toBe('offline')
  })

  it('treats a store with no instances slice as offline, not as a crash', () => {
    expect(routeFolderCrew(folder({ default_crew: 'i-nobita' }), ctx({ warm: undefined })).kind).toBe('offline')
  })

  it('falls back to the id as the display name for a crew the registry does not list', () => {
    expect(routeFolderCrew(folder({ default_crew: 'i-gone' }), ctx({ instances: [] })))
      .toEqual({ kind: 'offline', id: 'i-gone', name: 'i-gone' })
  })

  it('is Local whatever the pin says while the Remote crew sessions preview is off', () => {
    expect(routeFolderCrew(folder({ default_crew: 'i-nobita' }), ctx({ previewOn: false }))).toEqual({ kind: 'local' })
    expect(routeFolderCrew(folder({ default_crew: 'i-nobita' }), ctx({ previewOn: false, warm: {} }))).toEqual({ kind: 'local' })
  })

  it('does not match a warm key by prototype name', () => {
    expect(routeFolderCrew(folder({ default_crew: 'toString' }), ctx()).kind).toBe('offline')
  })
})
