import { describe, expect, it } from 'vitest'
import type { ChatMessage } from '../../types'
import { createSegmentComparer, readPageSegment } from './transcript'

const anchor: ChatMessage = {
  role: 'user', content: 'Keep the open reply.', cls: 'msg msg-u',
  ts: '2026-01-01T00:00:00Z',
  meta: { mid: 'm-notice-anchor', sendId: 's-notice-anchor' },
}
const open: ChatMessage = {
  role: 'streaming', content: 'Visible partial output.', cls: 'msg msg-a',
  seq: 4, gen: 'g-notice',
}
const notices: Array<[string, ChatMessage]> = [
  ['top-level stuck-turn compaction', {
    role: 'assistant', content: 'The turn appears stuck.', cls: 'msg msg-a',
    kind: 'compaction', meta: { mid: 'm-stuck-top', notice: 'stuck_turn' },
  }],
  ['persisted stuck-turn compaction', {
    role: 'assistant', content: 'The turn appears stuck.', cls: 'msg msg-a',
    meta: { mid: 'm-stuck-meta', kind: 'compaction', notice: 'stuck_turn' },
  }],
  ['top-level session reload', {
    role: 'assistant', content: 'Session reloaded.', cls: 'msg msg-a',
    kind: 'session_reload', meta: { mid: 'm-reload-top' },
  }],
  ['persisted session reload', {
    role: 'assistant', content: 'Session reloaded.', cls: 'msg msg-a',
    meta: { mid: 'm-reload-meta', kind: 'session_reload' },
  }],
]

describe('assistant system notices are not reply segments', () => {
  it.each(notices)('keeps an open stream open past a %s notice', (_name, notice) => {
    expect(readPageSegment([open, notice])).toEqual(expect.objectContaining({
      status: 'open', index: 0, message: open,
    }))
  })

  it.each(notices)('treats a %s notice-only page as absent', (_name, notice) => {
    expect(readPageSegment([notice])).toEqual({ status: 'absent' })
  })

  it('does not increment the ordinal for a notice but still counts an ordinary assistant', () => {
    const notice = notices[0][1]
    const targetLeft: ChatMessage = {
      role: 'assistant', content: 'Canonical reply.', cls: 'msg msg-a',
      meta: { mid: 'm-canonical-reply' },
    }
    const targetRight: ChatMessage = {
      role: 'assistant', content: 'Local reply.', cls: 'msg msg-a',
      meta: { clientTs: 'client-local-reply' },
    }
    expect(createSegmentComparer(
      [anchor, notice, targetLeft], [anchor, targetRight],
    ).compare(2, 1)).toBe('same')

    const ordinary: ChatMessage = {
      role: 'assistant', content: 'Earlier real reply.', cls: 'msg msg-a',
      meta: { mid: 'm-earlier-reply' },
    }
    expect(createSegmentComparer(
      [anchor, ordinary, targetLeft], [anchor, targetRight],
    ).compare(2, 1)).toBe('different')
    expect(readPageSegment([anchor, ordinary])).toEqual(expect.objectContaining({
      status: 'finalized', message: ordinary,
    }))
  })

  it('declines a unique id attached to rows with different timestamps', () => {
    const leftAnchor: ChatMessage = {
      role: 'user', content: 'First row.', cls: 'msg msg-u',
      ts: '2026-01-01T00:00:00Z', meta: { mid: 'm-reused' },
    }
    const rightAnchor: ChatMessage = {
      role: 'user', content: 'Different row.', cls: 'msg msg-u',
      ts: '2026-01-01T00:00:01Z', meta: { mid: 'm-reused' },
    }
    const leftTarget: ChatMessage = {
      role: 'assistant', content: 'Left reply.', cls: 'msg msg-a',
      meta: { clientTs: 'left-target' },
    }
    const rightTarget: ChatMessage = {
      role: 'assistant', content: 'Right reply.', cls: 'msg msg-a',
      meta: { clientTs: 'right-target' },
    }
    expect(createSegmentComparer(
      [leftAnchor, leftTarget], [rightAnchor, rightTarget],
    ).compare(1, 1)).toBe('unknown')
  })

  it('accepts a receipt-confirmed send across canonical timestamp replacement', () => {
    const localAnchor: ChatMessage = {
      role: 'user', content: 'Sent locally.', cls: 'msg msg-u',
      ts: '2026-01-01T00:00:00Z', meta: { sendId: 's-receipt', mid: 'm-receipt' },
    }
    const serverAnchor: ChatMessage = {
      role: 'user', content: 'Sent locally.', cls: 'msg msg-u',
      ts: '2026-01-01T00:00:01Z', meta: { sendId: 's-receipt', mid: 'm-receipt' },
    }
    const leftTarget: ChatMessage = {
      role: 'assistant', content: 'Canonical reply.', cls: 'msg msg-a',
      meta: { mid: 'm-canonical-after-receipt' },
    }
    const rightTarget: ChatMessage = {
      role: 'assistant', content: 'Local reply.', cls: 'msg msg-a',
      meta: { clientTs: 'client-after-receipt' },
    }
    expect(createSegmentComparer(
      [serverAnchor, leftTarget], [localAnchor, rightTarget],
    ).compare(1, 1)).toBe('same')
  })

  it('declines a reused send id when the server mids disagree', () => {
    const leftAnchor: ChatMessage = {
      role: 'user', content: 'First send.', cls: 'msg msg-u',
      ts: '2026-01-01T00:00:00Z', meta: { sendId: 's-reused', mid: 'm-left' },
    }
    const rightAnchor: ChatMessage = {
      role: 'user', content: 'Different send.', cls: 'msg msg-u',
      ts: '2026-01-01T00:00:01Z', meta: { sendId: 's-reused', mid: 'm-right' },
    }
    const target: ChatMessage = {
      role: 'assistant', content: 'Reply.', cls: 'msg msg-a',
      meta: { clientTs: 'target' },
    }
    expect(createSegmentComparer(
      [leftAnchor, target], [rightAnchor, target],
    ).compare(1, 1)).toBe('unknown')
  })

  it('declines a unique id attached to rows with different roles', () => {
    const leftAnchor: ChatMessage = {
      role: 'user', content: 'User row.', cls: 'msg msg-u',
      ts: '2026-01-01T00:00:00Z', meta: { mid: 'm-role-reused' },
    }
    const rightAnchor: ChatMessage = {
      role: 'assistant', content: 'Assistant row.', cls: 'msg msg-a',
      ts: '2026-01-01T00:00:00Z', meta: { mid: 'm-role-reused' },
    }
    const target: ChatMessage = {
      role: 'assistant', content: 'Reply.', cls: 'msg msg-a',
      meta: { clientTs: 'target' },
    }
    expect(createSegmentComparer(
      [leftAnchor, target], [rightAnchor, target],
    ).compare(1, 1)).toBe('unknown')
  })
})
