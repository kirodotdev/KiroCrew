import { describe, expect, it } from 'vitest'

import type { ChatMessage } from '../../../types'
import { applyRunningState, createTurnGrouper } from '../groupDisplayItems'
import type { DisplayItem } from '../types'
import { grewOnlyAtTail, isFirstTurnOfEmptyChat } from './transcriptRows'

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

  it('holds for a streaming flush through the real grouping of a running turn', () => {
    const done = [msg('user', 'earlier', 't0'), msg('assistant', 'a', 'ta'), msg('tool', 't', 'tb'), msg('assistant', 'b', 'tc')]
    const turn = [...done, user, msg('assistant', 'looking', 't2'), msg('tool', 'ran', 't3'), msg('streaming', 'ans', 't4')]
    const group = createTurnGrouper()
    const rows = (messages: ChatMessage[]) => ({ messages, displayItems: applyRunningState(group(messages), true) })
    const first = rows(turn)
    expect(first.displayItems.map((it) => it.kind)).toEqual(['single', 'turn', 'single', 'turn'])
    const flushed = turn.slice()
    flushed[7] = { ...turn[7], content: 'answer' }
    expect(grewOnlyAtTail(first, rows(flushed))).toBe(true)
  })

  it('holds when a row was added after the last row', () => {
    const tool = msg('tool', 'ran', 't3')
    expect(grewOnlyAtTail(before, {
      messages: [user, tail, tool], displayItems: [...before.displayItems, single(tool, 2)],
    })).toBe(true)
  })

  it('holds for a sent prompt, the first chunk of its reply and a tool row, through the real grouper', () => {
    const done = [msg('user', 'earlier', 't0'), msg('assistant', 'a', 'ta'), msg('tool', 't', 'tb'), msg('assistant', 'b', 'tc')]
    const group = createTurnGrouper()
    const rows = (messages: ChatMessage[]) => ({ messages, displayItems: applyRunningState(group(messages), true) })
    // The frame the deferral holds while the turn runs: the transcript as it
    // was before the prompt. Every later frame is judged against this one.
    const opened = rows(done)
    const sent = [...done, user]
    expect(grewOnlyAtTail(opened, rows(sent))).toBe(true)
    const started = [...sent, msg('streaming', 'ans', 't2')]
    expect(grewOnlyAtTail(opened, rows(started))).toBe(true)
    // A tool row regroups the running turn's rows into one turn item. Those
    // rows all sit after the last row the deferral saw.
    const withTool = [...sent, msg('assistant', 'looking', 't2'), msg('tool', 'ran', 't3'), msg('streaming', 'answer', 't4')]
    const regrouped = rows(withTool)
    expect(regrouped.displayItems.map((it) => it.kind)).toEqual(['single', 'turn', 'single', 'turn'])
    expect(grewOnlyAtTail(opened, regrouped)).toBe(true)
  })

  it('holds when the last row changed and rows were added after it', () => {
    const settled = msg('assistant', 'ans', 't2')
    const next = msg('user', 'and then?', 't3')
    expect(grewOnlyAtTail(before, {
      messages: [user, settled, next], displayItems: [before.displayItems[0], single(settled, 1), single(next, 2)],
    })).toBe(true)
  })

  it('fails when a page of older rows landed in front', () => {
    const older = msg('assistant', 'older', 't0')
    expect(grewOnlyAtTail(before, {
      messages: [older, user, tail], displayItems: [single(older, 0), single(user, 1), single(tail, 2)],
    })).toBe(false)
  })

  it('fails when an earlier row was replaced while a row was added', () => {
    const edited = msg('user', 'question?', 't1')
    const tool = msg('tool', 'ran', 't3')
    expect(grewOnlyAtTail(before, {
      messages: [edited, tail, tool], displayItems: [...before.displayItems, single(tool, 2)],
    })).toBe(false)
    expect(grewOnlyAtTail(before, {
      messages: [user, tail, tool], displayItems: [single(user, 0), before.displayItems[1], single(tool, 2)],
    })).toBe(false)
  })

  it('fails when a row was removed', () => {
    expect(grewOnlyAtTail(before, { messages: [user], displayItems: [before.displayItems[0]] })).toBe(false)
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

  it('fails for the first rows of an empty transcript, which land as a page', () => {
    expect(grewOnlyAtTail({ messages: [], displayItems: [] }, before)).toBe(false)
  })
})

describe('isFirstTurnOfEmptyChat', () => {
  const empty = { messages: [] as ChatMessage[], loaded: true }

  it('holds for the first send into a loaded empty chat, its reply and a tool row', () => {
    expect(isFirstTurnOfEmptyChat(empty, { messages: [user] })).toBe(true)
    expect(isFirstTurnOfEmptyChat(empty, { messages: [user, tail] })).toBe(true)
    const withTool = [user, msg('assistant', 'looking', 't2'), msg('tool', 'ran', 't3'), msg('streaming', 'answer', 't4')]
    expect(isFirstTurnOfEmptyChat(empty, { messages: withTool })).toBe(true)
  })

  it('holds when the running turn takes a steer or a parked send', () => {
    const steer = { ...msg('user', 'also this', 't3'), meta: { steer: true } } as ChatMessage
    expect(isFirstTurnOfEmptyChat(empty, { messages: [user, tail, steer] })).toBe(true)
    const parked = { ...msg('user', 'and then', 't3'), meta: { queuedEntryId: 'q1' } } as ChatMessage
    expect(isFirstTurnOfEmptyChat(empty, { messages: [user, tail, parked] })).toBe(true)
  })

  it('fails for rows that hold a second turn, which is a page of history', () => {
    const page = [user, msg('assistant', 'answer', 't2'), msg('user', 'next question', 't3')]
    expect(isFirstTurnOfEmptyChat(empty, { messages: page })).toBe(false)
  })

  it('fails while the empty transcript is still loading', () => {
    expect(isFirstTurnOfEmptyChat({ messages: [], loaded: false }, { messages: [user] })).toBe(false)
    expect(isFirstTurnOfEmptyChat({ messages: [] }, { messages: [user] })).toBe(false)
  })

  it('fails when rows are already on screen, and when there is nothing to draw', () => {
    expect(isFirstTurnOfEmptyChat({ messages: [user], loaded: true }, { messages: [user, tail] })).toBe(false)
    expect(isFirstTurnOfEmptyChat(empty, { messages: [] })).toBe(false)
  })
})
