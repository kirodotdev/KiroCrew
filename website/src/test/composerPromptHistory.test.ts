import { describe, expect, it } from 'vitest'
import {
  livePromptHistoryCursor,
  promptHistoryFromMessages,
  samePromptHistory,
  stepPromptHistory,
  type PromptHistoryCursor,
  type PromptHistoryItem,
} from '../components/composerPromptHistory'
import type { ChatMessage } from '../types'

/** Walk `keys` from a fresh composer holding `draft`, returning each text shown. */
function walk(items: PromptHistoryItem[], draft: string, keys: ('older' | 'newer')[]) {
  let cursor: PromptHistoryCursor | null = null
  let text = draft
  const shown: string[] = []
  for (const key of keys) {
    const step = stepPromptHistory(items, livePromptHistoryCursor(cursor, text), key, text)
    if (!step) { shown.push('<native>'); continue }
    cursor = step.cursor
    text = step.text
    shown.push(text)
  }
  return { shown, cursor, text }
}

function user(content: string, meta?: Record<string, unknown>): ChatMessage {
  return { role: 'user', content, cls: 'msg msg-u', ...(meta ? { meta } : {}) }
}

/** Prompt-history entries with no id, the shape a list without ids takes. */
function hist(...texts: string[]): PromptHistoryItem[] {
  return texts.map(text => ({ text }))
}

/** Prompt-history entries that all share one id, as a `ts:` id run does. */
function sameId(id: string, ...texts: string[]): PromptHistoryItem[] {
  return texts.map(text => ({ text, id }))
}

describe('stepPromptHistory', () => {
  const sent = hist('first', 'second', 'third')

  it('walks newest to oldest, stops at the oldest, and walks back to the draft', () => {
    const { shown, cursor } = walk(sent, 'draft', ['older', 'older', 'older', 'older', 'newer', 'newer', 'newer'])
    expect(shown).toEqual(['third', 'second', 'first', 'first', 'second', 'third', 'draft'])
    expect(cursor).toBeNull()
  })

  it('does not treat ↓ as a history step while not browsing', () => {
    expect(stepPromptHistory(sent, null, 'newer', 'draft')).toBeNull()
  })

  it('does nothing on an empty history', () => {
    expect(stepPromptHistory([], null, 'older', '')).toBeNull()
  })

  it('keeps its entry when older history is loaded in front mid-browse', () => {
    const first = stepPromptHistory(sent, null, 'older', '')!
    const second = stepPromptHistory(sent, first.cursor, 'older', first.text)!
    expect(second.text).toBe('second')
    const grown = [...hist('zero-a', 'zero-b'), ...sent]
    expect(stepPromptHistory(grown, second.cursor, 'older', second.text)!.text).toBe('first')
    expect(stepPromptHistory(grown, second.cursor, 'newer', second.text)!.text).toBe('third')
  })

  it('keeps its entry when a new prompt is appended mid-browse', () => {
    const first = stepPromptHistory(sent, null, 'older', '')!
    const second = stepPromptHistory(sent, first.cursor, 'older', first.text)!
    const grown = [...sent, ...hist('queued and sent')]
    expect(stepPromptHistory(grown, second.cursor, 'older', second.text)!.text).toBe('first')
    expect(stepPromptHistory(grown, second.cursor, 'newer', second.text)!.text).toBe('third')
  })

  it('follows the id when an optimistic entry is replaced by the server copy', () => {
    const before: PromptHistoryItem[] = [{ text: 'a', id: 's1' }, { text: 'b', id: 's2' }, { text: 'c', id: 's3' }]
    const up1 = stepPromptHistory(before, null, 'older', '')!
    const up2 = stepPromptHistory(before, up1.cursor, 'older', up1.text)!
    expect(up2.cursor!.id).toBe('s2')
    // Same ids, one older row loaded in front, and the text of "b" differs on
    // the server copy: the id still finds it.
    const after: PromptHistoryItem[] = [{ text: 'z', id: 's0' }, { text: 'a', id: 's1' }, { text: 'b (server)', id: 's2' }, { text: 'c', id: 's3' }]
    expect(stepPromptHistory(after, up2.cursor, 'older', up2.text)!.text).toBe('a')
    expect(stepPromptHistory(after, up2.cursor, 'newer', up2.text)!.text).toBe('c')
  })

  it('walks a whole run of rows sharing one id, then back through each to the draft', () => {
    // A slot rehydration stamps one timestamp onto every queued row it restores,
    // so `ts:` ids collide across a run of prompts.
    const items = sameId('ts:7', 'p1', 'p2', 'p3')
    const { shown, cursor } = walk(items, 'draft', ['older', 'older', 'older', 'newer', 'newer', 'newer'])
    expect(shown).toEqual(['p3', 'p2', 'p1', 'p2', 'p3', 'draft'])
    expect(cursor).toBeNull()
  })

  it('keeps its entry when a row with the same id is appended mid-browse', () => {
    const items = sameId('ts:7', 'p1', 'p2', 'p3')
    const up1 = stepPromptHistory(items, null, 'older', '')!
    const up2 = stepPromptHistory(items, up1.cursor, 'older', up1.text)!
    expect(up2.text).toBe('p2')
    const grown = sameId('ts:7', 'p1', 'p2', 'p3', 'p4')
    expect(stepPromptHistory(grown, up2.cursor, 'older', up2.text)!.text).toBe('p1')
    expect(stepPromptHistory(grown, up2.cursor, 'newer', up2.text)!.text).toBe('p3')
  })

  it('keeps its entry among duplicated texts sharing an id when a row is appended to the run', () => {
    // Drained queue rows share one `ts:` id; with a repeated text the text
    // shortcut cannot decide, so the position among the id's rows must not
    // move when another row joins the run at its newest end.
    const items = sameId('ts:7', 'continue', 'check the logs', 'continue')
    const up1 = stepPromptHistory(items, null, 'older', '')!
    const up2 = stepPromptHistory(items, up1.cursor, 'older', up1.text)!
    const up3 = stepPromptHistory(items, up2.cursor, 'older', up2.text)!
    expect([up1.text, up2.text, up3.text]).toEqual(['continue', 'check the logs', 'continue'])
    const grown = sameId('ts:7', 'continue', 'check the logs', 'continue', 'deploy')
    const down1 = stepPromptHistory(grown, up3.cursor, 'newer', up3.text)!
    expect(down1.text).toBe('check the logs')
    const down2 = stepPromptHistory(grown, down1.cursor, 'newer', down1.text)!
    expect(down2.text).toBe('continue')
    expect(stepPromptHistory(grown, down2.cursor, 'newer', down2.text)!.text).toBe('deploy')
  })

  it('follows a row sharing an id by its position among them when its text is rewritten', () => {
    const items = sameId('ts:7', 'a', 'b', 'c')
    const up1 = stepPromptHistory(items, null, 'older', '')!
    const up2 = stepPromptHistory(items, up1.cursor, 'older', up1.text)!
    expect(up2.text).toBe('b')
    const rewritten = sameId('ts:7', 'a', 'b (server)', 'c')
    expect(stepPromptHistory(rewritten, up2.cursor, 'older', up2.text)!.text).toBe('a')
    expect(stepPromptHistory(rewritten, up2.cursor, 'newer', up2.text)!.text).toBe('c')
  })

  it('tells identical non-adjacent prompts apart by their position among equals', () => {
    const items = hist('same', 'other', 'same')
    const { shown, cursor } = walk(items, '', ['older', 'older', 'older'])
    expect(shown).toEqual(['same', 'other', 'same'])
    expect(stepPromptHistory(items, cursor, 'newer', 'same')!.text).toBe('other')
    // Loading older rows in front does not move it onto the newer "same".
    const grown = [...hist('same', 'x'), ...items]
    expect(stepPromptHistory(grown, cursor, 'older', 'same')!.text).toBe('x')
    expect(stepPromptHistory(grown, cursor, 'newer', 'same')!.text).toBe('other')
  })

  it('falls back to the distance from the newest entry when its entry is gone', () => {
    const up1 = stepPromptHistory(sent, null, 'older', '')!
    const up2 = stepPromptHistory(sent, up1.cursor, 'older', up1.text)!
    expect(stepPromptHistory(hist('p', 'q', 'r', 's'), up2.cursor, 'older', up2.text)!.text).toBe('q')
    expect(stepPromptHistory(hist('p', 'q', 'r', 's'), up2.cursor, 'newer', up2.text)!.text).toBe('s')
    expect(stepPromptHistory(hist('only'), up2.cursor, 'older', up2.text)!.text).toBe('only')
  })

  it('keeps the saved draft restorable across list changes while browsing', () => {
    const up1 = stepPromptHistory(sent, null, 'older', 'my draft')!
    const grown = [...hist('older-row'), ...sent, ...hist('newer-row')]
    const down1 = stepPromptHistory(grown, up1.cursor, 'newer', up1.text)!
    expect(down1.text).toBe('newer-row')
    const down2 = stepPromptHistory(grown, down1.cursor, 'newer', down1.text)!
    expect(down2).toEqual({ cursor: null, text: 'my draft' })
  })
})

describe('livePromptHistoryCursor', () => {
  it('ends browsing once the composer text no longer equals the recalled entry', () => {
    const up = stepPromptHistory(hist('first', 'second'), null, 'older', 'draft')!
    expect(livePromptHistoryCursor(up.cursor, 'second')).toBe(up.cursor)
    expect(livePromptHistoryCursor(up.cursor, 'second, edited')).toBeNull()
    expect(livePromptHistoryCursor(up.cursor, '')).toBeNull()
  })

  it('makes ↓ after an edit a native caret move, not a history step', () => {
    const up = stepPromptHistory(hist('first', 'second'), null, 'older', '')!
    const edited = 'second!'
    expect(stepPromptHistory(hist('first', 'second'), livePromptHistoryCursor(up.cursor, edited), 'newer', edited)).toBeNull()
  })
})

describe('promptHistoryFromMessages', () => {
  it('keeps user prompts oldest first with their ids, collapsing consecutive duplicates', () => {
    const messages: ChatMessage[] = [
      user('hi', { sendId: 'a' }),
      { role: 'assistant', content: 'hello', cls: '' },
      user('again', { sendId: 'b' }),
      user('again', { sendId: 'c' }),
      user('again', { mid: 'm1' }),
      user('elsewhere', { mid: 'm2' }),
      user('plain'),
      user(''),
      { role: 'queued', content: 'not yet', cls: '' },
    ]
    expect(promptHistoryFromMessages(messages)).toEqual([
      { text: 'hi', id: 'a' },
      { text: 'again', id: 'b' },
      { text: 'elsewhere', id: 'm2' },
      { text: 'plain' },
    ])
  })

  it('prefers the client send id over the server id, and rawText over content', () => {
    expect(promptHistoryFromMessages([{ ...user('shown', { sendId: 's', mid: 'm' }), rawText: 'typed' }]))
      .toEqual([{ text: 'typed', id: 's' }])
  })

  it('keeps a ts-only duplicate anchored when the same prompt is appended', () => {
    const messages: ChatMessage[] = [
      { ...user('same'), ts: '1' },
      { ...user('older neighbour'), ts: '2' },
      { ...user('same'), ts: '3' },
      { ...user('newer neighbour'), ts: '4' },
    ]
    const history = promptHistoryFromMessages(messages)
    const newest = stepPromptHistory(history, null, 'older', 'draft')!
    const recalled = stepPromptHistory(history, newest.cursor, 'older', newest.text)!
    expect(recalled.text).toBe('same')

    const grown = promptHistoryFromMessages([...messages, { ...user('same'), ts: '5' }])
    expect(stepPromptHistory(grown, recalled.cursor, 'older', recalled.text)!.text).toBe('older neighbour')
    expect(stepPromptHistory(grown, recalled.cursor, 'newer', recalled.text)!.text).toBe('newer neighbour')
  })

  it('keeps a timestamp-less row anchored on its reducer-minted clientTs', () => {
    // A linked-channel prompt arrives without `ts`, so the reducer gives it a
    // `meta.clientTs`. Without that id a later identical prompt relocates recall.
    const messages: ChatMessage[] = [
      user('same', { clientTs: 'msg-a' }),
      user('older neighbour', { clientTs: 'msg-b' }),
      user('same', { clientTs: 'msg-c' }),
      user('newer neighbour', { clientTs: 'msg-d' }),
    ]
    const history = promptHistoryFromMessages(messages)
    expect(history[2]).toEqual({ text: 'same', id: 'msg-c' })
    const newest = stepPromptHistory(history, null, 'older', 'draft')!
    const recalled = stepPromptHistory(history, newest.cursor, 'older', newest.text)!
    expect(recalled.text).toBe('same')

    const grown = promptHistoryFromMessages([...messages, user('same', { clientTs: 'msg-e' })])
    expect(stepPromptHistory(grown, recalled.cursor, 'older', recalled.text)!.text).toBe('older neighbour')
    expect(stepPromptHistory(grown, recalled.cursor, 'newer', recalled.text)!.text).toBe('newer neighbour')
  })

  it('ranks clientTs after sendId and before mid, and keeps the ts form when clientTs copies ts', () => {
    expect(promptHistoryFromMessages([user('a', { sendId: 's', clientTs: 'msg-x', mid: 'm' })])).toEqual([{ text: 'a', id: 's' }])
    expect(promptHistoryFromMessages([user('a', { clientTs: 'msg-x', mid: 'm' })])).toEqual([{ text: 'a', id: 'msg-x' }])
    expect(promptHistoryFromMessages([{ ...user('a', { clientTs: '7' }), ts: '7' }])).toEqual([{ text: 'a', id: 'ts:7' }])
  })
})

describe('samePromptHistory', () => {
  it('compares text and id element-wise', () => {
    expect(samePromptHistory([{ text: 'a', id: '1' }], [{ text: 'a', id: '1' }])).toBe(true)
    expect(samePromptHistory([{ text: 'a', id: '1' }], [{ text: 'a', id: '2' }])).toBe(false)
    expect(samePromptHistory(hist('a'), [{ text: 'a' }])).toBe(true)
    expect(samePromptHistory(hist('a'), hist('a', 'b'))).toBe(false)
  })
})
