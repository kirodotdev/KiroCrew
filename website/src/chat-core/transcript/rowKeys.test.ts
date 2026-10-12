import { describe, expect, it } from 'vitest'
import type { ChatMessage } from '../../types'
import type { DisplayItem, TurnItem } from '../../pages/chat/types'
import { anchorAltIdFor, msgIdentityKey, stableAnchorIdFor, turnLeadKey, uniqueRowKeys, virtualKeyFor } from './rowKeys'

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

  describe('with a chrono map (rows drawn newest block first)', () => {
    const q2 = turn([single(msg('q2', 'user')), single(msg('a2'))])
    const headlessBefore = turn([single(msg('h1')), single(msg('h2'))])
    const headlessAfter = turn([single(msg('h0')), single(msg('h1')), single(msg('h2'))])

    it('tail-keys the headless oldest turn drawn last, across a landing that grows its head', () => {
      const before = uniqueRowKeys([q2, headlessBefore], key, undefined, [1, 0])
      const after = uniqueRowKeys([q2, headlessAfter], key, undefined, [1, 0])
      expect(before).toEqual(['row-q2', 'hlt-h2'])
      expect(after).toEqual(before)
    })

    it('gives each row its chronological key, written at its drawn index', () => {
      const chronological = uniqueRowKeys([headlessAfter, q2], key)
      expect(uniqueRowKeys([q2, headlessAfter], key, undefined, [1, 0])).toEqual([chronological[1], chronological[0]])
    })

    it('runs the tie-break chronologically, so a newer collider drawn first takes the suffix', () => {
      const older = single(msg('d', 'user'))
      const newer = single(msg('d', 'user'))
      expect(uniqueRowKeys([older, newer], key)).toEqual(['row-d', 'row-d~#1'])
      expect(uniqueRowKeys([newer, older], key, undefined, [1, 0])).toEqual(['row-d~#1', 'row-d'])
    })

    it('passes the drawn index to a preferred key', () => {
      const seen: number[] = []
      uniqueRowKeys([q2, headlessAfter], key, (_it, i) => { seen.push(i); return undefined }, [1, 0])
      expect(seen).toEqual([1, 0])
    })

    it('falls back to list order for a map that is not a permutation of the rows', () => {
      const plain = uniqueRowKeys([q2, headlessAfter], key)
      for (const bad of [[0, 0], [1], [0, 2], [0.5, 1], [-1, 0]]) {
        expect(uniqueRowKeys([q2, headlessAfter], key, undefined, bad)).toEqual(plain)
      }
    })
  })
})
