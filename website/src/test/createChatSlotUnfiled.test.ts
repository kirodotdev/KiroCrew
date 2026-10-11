/**
 * `api.createChatSlot` answers with the created slot. When the gateway opened the
 * session but refused to file it into the requested folder, the answered
 * `folder_id` differs from the one asked for, and the client logs ONE warning,
 * so every caller (the sidebar, the app session hooks, Code Review Sage) records it.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { api, __resetAuthRecoveryStateForTests } from '../api/client'

function res(status: number, body: unknown): Response {
  const text = JSON.stringify(body)
  return {
    ok: status >= 200 && status < 300,
    status,
    url: 'http://localhost:6776/api/chat/slots',
    headers: { get: () => null },
    json: async () => body,
    text: async () => text,
    clone: () => res(status, body),
  } as unknown as Response
}

const fetchMock = vi.fn()
let warn: ReturnType<typeof vi.spyOn>

beforeEach(() => {
  fetchMock.mockReset()
  vi.stubGlobal('fetch', fetchMock)
  __resetAuthRecoveryStateForTests()
  warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
})

afterEach(() => {
  warn.mockRestore()
  vi.unstubAllGlobals()
  __resetAuthRecoveryStateForTests()
})

const create = () => api.createChatSlot('demo', 'worker', undefined, undefined, 'persistent', undefined, undefined, 'f1')

const filingWarnings = () =>
  warn.mock.calls.filter(args => args.includes('[createChatSlot] session opened outside its folder'))

describe('createChatSlot unfiled create', () => {
  it('returns the unfiled slot and warns once naming the slot and the folder', async () => {
    fetchMock.mockResolvedValue(res(200, { key: 'demo', folder_id: '' }))
    const slot = await create()
    expect(slot.folder_id).toBe('')
    expect(filingWarnings()).toHaveLength(1)
    expect(filingWarnings()[0]).toContain('demo')
    expect(filingWarnings()[0]).toContain('f1')
  })

  it('does not warn when the slot was filed', async () => {
    fetchMock.mockResolvedValue(res(200, { key: 'demo', folder_id: 'f1' }))
    const slot = await create()
    expect(slot.folder_id).toBe('f1')
    expect(filingWarnings()).toEqual([])
  })

  it('does not warn for a create that asked for no folder', async () => {
    fetchMock.mockResolvedValue(res(200, { key: 'demo', folder_id: '' }))
    await api.createChatSlot('demo', 'worker', undefined, undefined, 'persistent')
    expect(filingWarnings()).toEqual([])
  })
})
