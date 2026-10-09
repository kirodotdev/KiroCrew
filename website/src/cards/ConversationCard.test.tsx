import { describe, it, expect } from 'vitest'
import type { ChatMessage } from '../types'
import { CARD_ROLE, readCardRef } from './ConversationCard'

const SLOT = 'member-mate'

function row(card: unknown): ChatMessage {
  return { role: CARD_ROLE, content: '', cls: 'msg msg-card', meta: { mid: 'm1', card } } as ChatMessage
}

describe('a guide offer row', () => {
  it('reads only a well-formed guide reference off a card row', () => {
    const base = row({ surface: 'guide', id: 'g1', slot: SLOT, kind: 'settings.show', status: 'offered' })
    expect(readCardRef(base)).toMatchObject({ surface: 'guide', id: 'g1', slot: SLOT, status: 'offered' })
    expect(readCardRef({ ...base, role: 'assistant' })).toBeNull()
    // A surface this build does not draw is not a reference: nothing renders for it.
    expect(readCardRef(row({ surface: 'change', id: 'c1', slot: SLOT }))).toBeNull()
    expect(readCardRef(row({ surface: 'other', id: 'g1' }))).toBeNull()
    expect(readCardRef(row({ surface: 'guide' }))).toBeNull()
    expect(readCardRef({ ...base, meta: { card: 'g1' } })).toBeNull()
  })

  it('keeps a recorded end reason only when the row carries one', () => {
    const ended = row({ surface: 'guide', id: 'g1', slot: SLOT, status: 'cancelled', reason: 'saved_without_guide' })
    expect(readCardRef(ended)?.reason).toBe('saved_without_guide')
    expect(readCardRef(row({ surface: 'guide', id: 'g1', slot: SLOT, status: 'completed' }))).not.toHaveProperty('reason')
  })
})
