import { describe, expect, it } from 'vitest'

import { kasControls, kasRow } from '../../../test/kasFixtures'
import { summarizeSidebarActivity } from './useKasWorkflows'

describe('summarizeSidebarActivity', () => {
  const done = kasRow({
    workflowId: 'done',
    slotKey: 'chat-1',
    status: 'completed',
    controls: kasControls({ display: 'completed', primary: null, canStop: false, canDelete: true }),
  })

  it('names one live run of this chat with its current step', () => {
    const live = kasRow({ workflowId: 'live', slotKey: 'chat-1', name: 'Investigate', blocking: null })
    const out = summarizeSidebarActivity([done, live, kasRow({ workflowId: 'other', slotKey: 'chat-2' })], 'chat-1')
    expect(out?.count).toBe(1)
    expect(out?.name).toBe('Investigate')
    expect(out?.attention).toBe(false)
  })

  it('leads with a run that needs a person, and counts every live run', () => {
    const a = kasRow({ workflowId: 'a', slotKey: 'chat-1', name: 'A', blocking: null })
    const b = kasRow({
      workflowId: 'b',
      slotKey: 'chat-1',
      name: 'B',
      blocking: { nodeId: 'n', label: 'Review', kind: 'permission', sessionId: 's', toolCallId: null },
    })
    const out = summarizeSidebarActivity([a, b], 'chat-1')
    expect(out).toMatchObject({ count: 2, name: 'B', attention: true })
    expect(out?.phase).toContain('Review')
  })

  it('says nothing for a chat whose runs have all finished', () => {
    expect(summarizeSidebarActivity([done], 'chat-1')).toBeUndefined()
    expect(summarizeSidebarActivity([], 'chat-1')).toBeUndefined()
  })
})
