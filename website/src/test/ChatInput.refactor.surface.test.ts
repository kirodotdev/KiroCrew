import { describe, expect, it } from 'vitest'
import * as chatInputModule from '../components/ChatInput'
import * as effort from '../lib/effort'

/* ── The public surface of `components/ChatInput`. The module is the composer's
 *    only import path: ChatPage and ChatPane render its default export, about
 *    thirty specs mock it by this id, three of them spread `importOriginal`, and
 *    ReasoningEffortDropdown reads `effortLabel` through it. Its owners live in
 *    `components/chat-input/`, which nothing outside the facade imports. ── */

describe('ChatInput public surface', () => {
  it('exports exactly the established names', () => {
    expect(Object.keys(chatInputModule).sort()).toEqual([
      'EFFORT_LABEL_KEY',
      'EFFORT_LEVELS',
      'REASONING_EFFORT_PROVIDERS',
      'UNATTENDED_APPROVAL_SOURCES',
      'default',
      'effortLabel',
      'modelSupportsEffort',
    ])
  })

  it('keeps the default export a memo component', () => {
    const component = chatInputModule.default as unknown as { $$typeof: symbol; type: unknown }
    expect(component.$$typeof).toBe(Symbol.for('react.memo'))
    expect(typeof component.type).toBe('function')
  })

  it('re-exports the effort vocabulary by identity, not by copy', () => {
    expect(chatInputModule.effortLabel).toBe(effort.effortLabel)
    expect(chatInputModule.modelSupportsEffort).toBe(effort.modelSupportsEffort)
    expect(chatInputModule.EFFORT_LABEL_KEY).toBe(effort.EFFORT_LABEL_KEY)
    expect(chatInputModule.EFFORT_LEVELS).toBe(effort.EFFORT_LEVELS)
    expect(chatInputModule.REASONING_EFFORT_PROVIDERS).toBe(effort.REASONING_EFFORT_PROVIDERS)
  })

  it('names the unattended approval sources the Trust affordances are withheld for', () => {
    expect([...chatInputModule.UNATTENDED_APPROVAL_SOURCES].sort()).toEqual(['cron', 'heartbeat', 'taskrunner'])
  })
})
