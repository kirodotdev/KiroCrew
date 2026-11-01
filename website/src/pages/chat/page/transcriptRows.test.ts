import { describe, expect, it } from 'vitest'

import type { ChatMessage } from '../../../types'
import type { DisplayItem } from '../types'
import { grewOnlyAtTail } from './transcriptRows'

const msg = (role: string, content: string, ts: string) => ({ role, content, ts, cls: '' }) as ChatMessage
const single = (m: ChatMessage, idx: number): DisplayItem => ({ kind: 'single', msg: m, idx })

const user = msg('user', 'question', 't1')
const tail = msg('streaming', 'ans', 't2')
const before = { messages: [user, tail], displayItems: [single(user, 0), single(tail, 1)] }

describe('grewOnlyAtTail', () => {
  it('holds when only the last message and the last display item were replaced', () => {
    const grown = msg('streaming', 'answer', 't2')
    expect(grewOnlyAtTail(before, { messages: [user, grown], displayItems: [before.displayItems[0], single(grown, 1)] })).toBe(true)
  })

  it('fails when a row was added', () => {
    const tool = msg('tool', 'ran', 't3')
    expect(grewOnlyAtTail(before, {
      messages: [user, tail, tool], displayItems: [...before.displayItems, single(tool, 2)],
    })).toBe(false)
  })

  it('fails when the last row is a different row or changed role', () => {
    const other = msg('streaming', 'ans', 't9')
    expect(grewOnlyAtTail(before, { messages: [user, other], displayItems: [before.displayItems[0], single(other, 1)] })).toBe(false)
    const settled = msg('assistant', 'ans', 't2')
    expect(grewOnlyAtTail(before, { messages: [user, settled], displayItems: [before.displayItems[0], single(settled, 1)] })).toBe(false)
  })

  it('fails when an earlier message or display item was replaced', () => {
    const edited = msg('user', 'question?', 't1')
    expect(grewOnlyAtTail(before, { messages: [edited, tail], displayItems: before.displayItems })).toBe(false)
    expect(grewOnlyAtTail(before, { messages: before.messages, displayItems: [single(user, 0), before.displayItems[1]] })).toBe(false)
  })

  it('fails on an empty transcript', () => {
    expect(grewOnlyAtTail({ messages: [], displayItems: [] }, { messages: [], displayItems: [] })).toBe(false)
  })
})
