import { describe, it, expect, vi } from 'vitest'
import { sessionControlContext } from '../pages/chat/page/sessionControls'
import type { ChatSlot } from '../types'

vi.mock('../api/client', () => ({ api: {} }))

const AGENTS = [
  { name: 'koda', display_name: 'Koda', selection_kind: 'member' as const },
  { name: 'koda-tpl', display_name: 'Not a Bot', selection_kind: 'template' as const },
]

const slot = (over: Partial<ChatSlot> = {}): ChatSlot => ({
  key: 'chat-2', messages: 0, running: false, folder_id: 'f1', project: '/work', ...over,
})

describe('sessionControlContext', () => {
  it("names the Bot by its display name in the Bot's own thread", () => {
    const ctx = sessionControlContext({
      sessionKey: 'dashboard:chat-2', currentSlot: slot({ mode: 'member', agent: 'koda' }), folderName: 'Work', agents: AGENTS,
    })
    expect(ctx).toEqual({
      sessionKey: 'dashboard:chat-2', folderId: 'f1', folderName: 'Work', cwd: '/work', member: { name: 'Koda' },
    })
  })

  it('falls back to the Bot name while the roster does not list it', () => {
    const ctx = sessionControlContext({
      sessionKey: 'dashboard:chat-2', currentSlot: slot({ mode: 'member', agent: 'nova' }), folderName: '', agents: AGENTS,
    })
    expect(ctx.member).toEqual({ name: 'nova' })
  })

  it('leaves member undefined in every other chat', () => {
    const plain = sessionControlContext({
      sessionKey: 'dashboard:chat-2', currentSlot: slot({ agent: 'koda' }), folderName: '', agents: AGENTS,
    })
    expect(plain.member).toBeUndefined()
    expect('member' in plain).toBe(false)
    const none = sessionControlContext({ sessionKey: '', currentSlot: undefined, folderName: '', agents: AGENTS })
    expect(none).toEqual({ sessionKey: '', folderId: '', folderName: '', cwd: '' })
  })
})
