import { describe, it, expect } from 'vitest'
import { renderHook } from '@testing-library/react'
import { useStableRowKeys, useTranscriptRows } from '../pages/chat/page/transcriptRows'
import type { ChatMessage } from '../types'

/**
 * The page's row keys in the top-down layout. The rows come newest block
 * first, so the partly loaded (headless) oldest turn is drawn LAST. Its key
 * must still be the tail key the chronological layout gives it: an older page
 * landing grows that turn's head, and a lead key would rename the row on every
 * landing (React remounts it and its height cache entry is lost).
 */
const row = (role: string, content: string, ts: string): ChatMessage =>
  ({ role, content, cls: '', ts } as ChatMessage)

const HEADLESS = [
  row('assistant', 'h1', 'zzq-03'), row('tool', 't1', 'zzq-04'), row('assistant', 'h2', 'zzq-05'),
]
const NEWEST = [row('user', 'q2', 'zzq-06'), row('assistant', 'a2', 'zzq-07')]
const OLDER_PAGE = [row('assistant', 'h0', 'zzq-01'), row('tool', 't0', 'zzq-02')]

const pageKeys = (messages: ChatMessage[], topDown: boolean, appToolCallIds: ReadonlySet<string>) =>
  renderHook(() => {
    const rows = useTranscriptRows({
      messages,
      slotRunning: false,
      activeSlot: 'zzq-slot',
      appToolCallIds,
      displayItemsRef: { current: [] },
      scrollerRef: { current: null } as never,
      topDown,
    })
    const { virtualKey } = useStableRowKeys({
      renderedDisplayItems: rows.renderedDisplayItems,
      appToolCallIds,
      activeSlot: 'zzq-slot',
      topDown,
      rowChrono: rows.rowChrono,
    })
    return rows.renderedDisplayItems.map((it, i) => virtualKey(it, i))
  }).result.current

describe('useStableRowKeys -- the headless oldest turn in the top-down layout', () => {
  // Both key paths: the plain one, and the one an MCP App payload switches on.
  for (const [label, apps] of [['no app payload', new Set<string>()], ['an app payload', new Set(['zzq-none'])]] as const) {
    it(`keeps its tail key when an older page grows its head (${label})`, () => {
      const before = pageKeys([...HEADLESS, ...NEWEST], true, apps)
      const after = pageKeys([...OLDER_PAGE, ...HEADLESS, ...NEWEST], true, apps)
      // Newest block first: q2, a2, then the headless turn.
      expect(before).toHaveLength(3)
      expect(before[2]).toBe('hlt-zzq-05')
      expect(after[2]).toBe(before[2])
      expect(after.slice(0, 2)).toEqual(before.slice(0, 2))
    })
  }

  it('gives each row the key the chronological layout gives it', () => {
    const messages = [...OLDER_PAGE, ...HEADLESS, ...NEWEST]
    const chronological = pageKeys(messages, false, new Set())
    const topDown = pageKeys(messages, true, new Set())
    // Same keys; only the order differs (the headless turn moves to the end).
    expect(topDown).toEqual([...chronological.slice(1), chronological[0]])
  })
})
