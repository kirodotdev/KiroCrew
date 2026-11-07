/**
 * Hook-level test for {@link useSkillsProvider}: with a chat open, the Skills
 * tab asks for the skills that chat's `$`-picker lists (its session key and
 * agent), because a `$token` outside the agent's mapping is sent as literal
 * text. The pure factory is covered in skillsProvider.test.ts.
 */
import { describe, it, expect, vi } from 'vitest'
import { waitFor } from '@testing-library/react'
import { renderHookWithProviders, createConnectedTestStore } from '../../../test/helpers'
import { setActiveSlot } from '../../../store/chatSlice'
import type { ChatSlot } from '../../../types'

const { skillsMock } = vi.hoisted(() => ({
  skillsMock: vi.fn((_slotKey?: string, agent?: string) => Promise.resolve(
    agent === 'reviewer'
      ? { skills: [{ name: 'mapped-skill' }], agent_scoped: true, agent: 'reviewer' }
      : [{ name: 'mapped-skill' }, { name: 'other-skill' }],
  )),
}))

vi.mock('../../../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../../api/client')>()
  const defaultAgent = () => Promise.resolve({ default_agent: '', default_template: 'reviewer', overridden: false })
  return { ...actual, api: { ...actual.api, skills: skillsMock, defaultAgent } }
})

import { useSkillsProvider } from './skillsProvider'

describe('useSkillsProvider — scope', () => {
  it('lists only the active chat agent\'s mapped skills', async () => {
    const slot = { key: 'chat-1', messages: 0, running: false, agent: 'reviewer', project: '/repo' } as ChatSlot
    const store = createConnectedTestStore({ dashboard: { slots: [slot] } as never })
    store.dispatch(setActiveSlot('chat-1'))
    const { result } = renderHookWithProviders(() => useSkillsProvider(), { store })
    const rows = await result.current.search('')
    expect(skillsMock).toHaveBeenCalledWith('dashboard:chat-1', 'reviewer', expect.anything())
    expect(rows.map((r) => r.title)).toEqual(['mapped-skill'])
  })

  it('asks for an agent-less chat\'s default template, as its picker does', async () => {
    const slot = { key: 'chat-2', messages: 0, running: false, agent: '', project: '/repo' } as ChatSlot
    const store = createConnectedTestStore({ dashboard: { slots: [slot] } as never })
    store.dispatch(setActiveSlot('chat-2'))
    const { result } = renderHookWithProviders(() => useSkillsProvider(), { store })
    await waitFor(async () => {
      expect((await result.current.search('')).map((r) => r.title)).toEqual(['mapped-skill'])
    })
    expect(skillsMock).toHaveBeenLastCalledWith('dashboard:chat-2', 'reviewer', expect.anything())
  })
})
