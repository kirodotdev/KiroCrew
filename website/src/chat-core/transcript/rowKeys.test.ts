import { describe, expect, it } from 'vitest'
import type { ChatMessage } from '../../types'
import type { DisplayItem, TurnItem } from '../../pages/chat/types'
import { anchorAltIdFor, leadMessage, msgIdentityKey, rowOfMessage, stableAnchorIdFor, turnLeadKey, uniqueRowKeys, virtualKeyFor } from './rowKeys'

const msg = (ts: string, role = 'assistant', mid?: string): ChatMessage =>
  ({ role, content: ts, ts, ...(mid ? { meta: { mid } } : {}) }) as ChatMessage
const key = (m: ChatMessage) => m.ts as string
const single = (m: ChatMessage, idx = 0): TurnItem => ({ kind: 'single', msg: m, idx })
const group = (msgs: ChatMessage[], startIdx = 0): TurnItem => ({ kind: 'group', msgs, startIdx })
const turn = (items: TurnItem[], complete = true): DisplayItem => ({ kind: 'turn', items, complete })

describe('rowKeys', () => {
  it('suffixes the message key with meta.mid when present', () => {
    expect(msgIdentityKey(msg('t1'), key)).toBe('t1')
    expect(msgIdentityKey(msg('t1', 'assistant', 'm9'), key)).toBe('t1~m9')
  })

  it('keys singles on the message and groups on their first message, index only when empty', () => {
    expect(turnLeadKey(single(msg('a')), key)).toBe('row-a')
    expect(turnLeadKey(group([msg('b'), msg('c')]), key)).toBe('grp-b')
    expect(turnLeadKey(group([], 4), key)).toBe('grp-idx-4')
  })

  it('reads the lead and tail anchors, with positional fallbacks for empty rows', () => {
    const t = turn([single(msg('u1', 'user')), group([msg('a1'), msg('a2')])])
    expect(anchorAltIdFor(t, 0, key)).toBe('l-u1')
    expect(stableAnchorIdFor(t, 0, key)).toBe('a-a2')
    expect(anchorAltIdFor(single(msg('s')), 1, key)).toBe('l-s')
    expect(stableAnchorIdFor(group([msg('g1'), msg('g2')]), 1, key)).toBe('a-g2')
    expect(anchorAltIdFor(turn([]), 3, key)).toBe('alt-empty-3')
    expect(stableAnchorIdFor(turn([]), 3, key)).toBe('anchor-empty-3')
    expect(anchorAltIdFor(group([]), 5, key)).toBe('alt-empty-5')
    expect(stableAnchorIdFor(group([]), 5, key)).toBe('anchor-empty-5')
  })

  it('tail-keys only a complete, non-trailing headless turn at index 0', () => {
    const headless = turn([single(msg('a1')), single(msg('a2'))])
    expect(virtualKeyFor(headless, 0, key)).toBe('hlt-a2')
    expect(virtualKeyFor(headless, 0, key, true)).toBe('row-a1')
    expect(virtualKeyFor(headless, 1, key)).toBe('row-a1')
    expect(virtualKeyFor(turn([single(msg('a1')), single(msg('a2'))], false), 0, key)).toBe('row-a1')
    expect(virtualKeyFor(turn([single(msg('u1', 'user')), single(msg('a2'))]), 0, key)).toBe('row-u1')
    expect(virtualKeyFor(turn([]), 2, key)).toBe('turn-empty-2')
    expect(virtualKeyFor(single(msg('x')), 2, key)).toBe('row-x')
  })

  it('gives colliding rows unique keys, skipping a suffix a natural key already holds', () => {
    const rows: DisplayItem[] = [single(msg('d')), single(msg('d~#1')), single(msg('d')), single(msg('d'))]
    expect(uniqueRowKeys(rows, key)).toEqual(['row-d', 'row-d~#1', 'row-d~#2', 'row-d~#3'])
  })

  it('lets a host supply a preferred key and still dedupes it', () => {
    const rows: DisplayItem[] = [single(msg('a')), single(msg('b'))]
    expect(uniqueRowKeys(rows, key, () => 'same')).toEqual(['same', 'same~#1'])
    expect(uniqueRowKeys(rows, key, () => undefined)).toEqual(['row-a', 'row-b'])
  })

  it('reads the first message a row draws, null for an empty row', () => {
    const prompt = msg('u1', 'user')
    expect(leadMessage(turn([single(prompt), group([msg('a1')])]))).toBe(prompt)
    expect(leadMessage(group([msg('g1'), msg('g2')]))?.ts).toBe('g1')
    expect(leadMessage(turn([]))).toBeNull()
    expect(leadMessage(group([]))).toBeNull()
  })

  it('finds the row that draws a message, by the object first and by its key once the object is replaced', () => {
    const lone = msg('s', 'user')
    const reply = msg('a2')
    const rows: DisplayItem[] = [single(lone), turn([single(msg('u1', 'user')), group([msg('a1'), reply])])]
    expect(rowOfMessage(rows, lone, key)).toBe(0)
    expect(rowOfMessage(rows, reply, key)).toBe(1)
    // The store copies a message object when it updates it.
    expect(rowOfMessage(rows, { ...reply }, key)).toBe(1)
    expect(rowOfMessage(rows, msg('gone'), key)).toBe(-1)
    expect(rowOfMessage([], reply, key)).toBe(-1)
  })

  it('tells two messages that share a key apart by object, and takes the first row once the object is replaced', () => {
    const older = msg('same')
    const newer = msg('same')
    const rows: DisplayItem[] = [single(older), single(newer)]
    expect(rowOfMessage(rows, newer, key)).toBe(1)
    expect(rowOfMessage(rows, { ...newer }, key)).toBe(0)
  })
})
