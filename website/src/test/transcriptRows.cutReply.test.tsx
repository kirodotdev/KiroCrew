import { describe, it, expect } from 'vitest'
import { renderHook } from '@testing-library/react'
import { useTranscriptRows } from '../pages/chat/page/transcriptRows'
import type { ChatMessage } from '../types'
import type { DisplayItem } from '../pages/chat/types'

/**
 * The MCP App promotion pass ends a loose region at a reply carrying turn
 * stats. A reply an error cut short carries them too, and the recovery that
 * resumes the turn is drawn below it: at every point of that turn the app rows
 * and the cut reply must stay in ONE turn, or the iframe remounts when the
 * region splits.
 */
const row = (role: string, content: string, meta?: Record<string, unknown>): ChatMessage =>
  ({ role, content, cls: '', ...(meta ? { meta } : {}) } as unknown as ChatMessage)
const stamped = (content: string) => row('assistant', content, { turn_stats: { elapsed_ms: 500 } })

const contents = (items: DisplayItem[]): string[][] =>
  items.map(d => (d.kind === 'turn'
    ? d.items.map(t => (t.kind === 'single' ? t.msg.content : 'group'))
    : [d.kind === 'single' ? d.msg.content : 'group']))

const rendered = (messages: ChatMessage[], slotRunning: boolean) =>
  renderHook(() => useTranscriptRows({
    messages,
    slotRunning,
    activeSlot: 'zzq-slot',
    appToolCallIds: new Set(['zzq-app-1', 'zzq-app-2']),
    displayItemsRef: { current: [] },
    scrollerRef: { current: null } as never,
  })).result.current.renderedDisplayItems

describe('useTranscriptRows -- an app turn an error cut short and a recovery resumed', () => {
  const full = [
    row('user', 'U'),
    row('tool', 'APP1', { tool_call_id: 'zzq-app-1' }),
    stamped('CUT'),
    row('error', 'ERR'),
    row('inject', 'REC', { injectKind: 'recovery' }),
    row('tool', 'APP2', { tool_call_id: 'zzq-app-2' }),
    stamped('RECOVERED'),
  ]

  it('keeps the app row and the cut reply in one turn at every point of the turn', () => {
    for (let n = 3; n <= full.length; n++) {
      for (const running of [true, false]) {
        const groups = contents(rendered(full.slice(0, n), running))
        const withApp = groups.find(g => g.includes('APP1'))
        expect(withApp, `prefix ${n}, running ${running}`).toEqual(full.slice(1, n).map(m => m.content))
      }
    }
  })
})
