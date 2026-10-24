/**
 * Timeline threads (#19505): grouping replies under their root, the marker each
 * reply leaves at its own time, and the marker revealing a folded reply.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'

import type { TimelineEvent } from '../api'
import { groupRuns, groupThreads } from '../lib/threads'
import { ReplyMarker, ThreadReplies } from './ThreadReplies'

const c = (actor: string, day: number, thread_id: string | null, body = `${actor} says`): TimelineEvent => ({
  kind: 'comment', actor, created_at: `2026-10-${String(day).padStart(2, '0')}T00:00:00Z`, body, thread_id,
})

beforeEach(() => {
  vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => { cb(0); return 0 })
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('groupThreads', () => {
  it('nests replies under the root and leaves a marker at each reply', () => {
    const root = c('kim', 1, 't1')
    const reply1 = c('lee', 5, 't1')
    const reply2 = c('kim', 9, 't1')
    const other = c('max', 7, null)
    const label: TimelineEvent = { kind: 'labeled', actor: 'max', created_at: '2026-10-03T00:00:00Z' }
    // Newest first, the way the detail panes hold it.
    const items = groupThreads([reply2, other, reply1, label, root])
    expect(items.map((it) => it.type)).toEqual(['reply-marker', 'event', 'reply-marker', 'event', 'thread'])
    const thread = items[4]
    expect(thread.type === 'thread' && thread.replies).toEqual([reply1, reply2])
    expect(items[0].type === 'reply-marker' && items[0].root).toBe(root)
  })

  it('takes the comment whose id is the thread handle as the root, even on a timestamp tie', () => {
    // GitHub: the thread handle is the root comment's id, and a reply posted in
    // the same second carries the same created_at.
    const root: TimelineEvent = { ...c('kim', 1, '42'), id: 42 }
    const reply: TimelineEvent = { ...c('lee', 1, '42'), id: 43 }
    // Newest first, the reply is listed before the root it answers.
    const items = groupThreads([reply, root], { rootIsThreadId: true })
    const thread = items.find((it) => it.type === 'thread')
    expect(thread?.type === 'thread' && thread.root).toBe(root)
    expect(thread?.type === 'thread' && thread.replies).toEqual([reply])
  })

  it('does not take a reply whose id matches the thread as the root off GitHub', () => {
    // Azure DevOps: thread 2 numbers its comments 1, 2, ..., so reply 2 shares the
    // thread's id by coincidence. The oldest comment is the root.
    const root: TimelineEvent = { ...c('kim', 1, '2'), id: 1 }
    const reply: TimelineEvent = { ...c('lee', 2, '2'), id: 2 }
    const thread = groupThreads([reply, root]).find((it) => it.type === 'thread')
    expect(thread?.type === 'thread' && thread.root).toBe(root)
  })

  it('keeps the forge order between replies with the same timestamp', () => {
    const root = c('kim', 1, 't')
    const first = c('lee', 3, 't', 'first')
    const second = c('max', 3, 't', 'second')
    // Newest first: `second` came after `first`.
    const items = groupThreads([second, first, root])
    const thread = items.find((it) => it.type === 'thread')
    expect(thread?.type === 'thread' && thread.replies.map((r) => r.body)).toEqual(['first', 'second'])
  })

  it('leaves a single-comment thread as a plain event', () => {
    // Every GitLab note has a discussion id, so a lone note is a thread of one.
    const items = groupThreads([c('kim', 2, 'solo'), c('lee', 1, null)])
    expect(items.map((it) => it.type)).toEqual(['event', 'event'])
  })
})

/** A reply at a minute offset, for run windows. */
const m = (actor: string | null, minute: number, body = `${actor} at ${minute}`): TimelineEvent => ({
  kind: 'comment', actor, body, thread_id: 't',
  created_at: new Date(Date.UTC(2026, 9, 1, 0, minute)).toISOString(),
})

describe('groupRuns', () => {
  it('stacks back-to-back replies by one author within the window', () => {
    const runs = groupRuns([m('kim', 0), m('kim', 12), m('kim', 41), m('lee', 42), m('kim', 43)])
    expect(runs.map((r) => r.map((ev) => ev.body))).toEqual([
      ['kim at 0', 'kim at 12', 'kim at 41'], ['lee at 42'], ['kim at 43'],
    ])
  })

  it('breaks a run when the gap from the previous reply exceeds the window', () => {
    expect(groupRuns([m('kim', 0), m('kim', 30)]).map((r) => r.length)).toEqual([2])
    expect(groupRuns([m('kim', 0), m('kim', 31)]).map((r) => r.length)).toEqual([1, 1])
  })

  it('never joins replies without a known author', () => {
    expect(groupRuns([m(null, 0), m(null, 1)]).map((r) => r.length)).toEqual([1, 1])
  })
})

describe('ThreadReplies runs', () => {
  const renderPart = (ev: TimelineEvent, part: { continuation: boolean }) =>
    <p>{part.continuation ? 'cont' : 'head'}:{ev.body}</p>

  it('shows a continuation time only when it reads differently from the reply before', () => {
    const seen: boolean[] = []
    const record = (ev: TimelineEvent, part: { showTime: boolean }) => {
      seen.push(part.showTime)
      return <p>{ev.body}</p>
    }
    // Long ago, 20 minutes apart: both read as the same day, so the second hides it.
    const old = (min: number): TimelineEvent => ({
      kind: 'comment', actor: 'kim', body: `old ${min}`, thread_id: 't',
      created_at: new Date(Date.UTC(2025, 0, 1, 12, min)).toISOString(),
    })
    render(<ThreadReplies replies={[old(0), old(20)]} renderReply={record} />)
    expect(seen).toEqual([true, false])
  })

  it('draws a run as one card, with a header on its first reply only', () => {
    render(<ThreadReplies replies={[m('kim', 0), m('kim', 2), m('lee', 3)]} renderReply={renderPart} />)
    const runs = screen.getAllByTestId('reply-run')
    expect(runs.map((r) => r.textContent)).toEqual(['head:kim at 0cont:kim at 2', 'head:lee at 3'])
    // Each message still carries its own key, so a marker can flash it.
    expect(runs[0].querySelectorAll('[data-thread-reply]')).toHaveLength(2)
  })

  it('folds whole runs and counts the replies they hold', () => {
    // Runs: [a0] [b1] [c2 c3 c4]. The newest run alone reaches the tail of 3.
    const replies = [m('a', 0), m('b', 1), m('c', 2), m('c', 3), m('c', 4)]
    render(<ThreadReplies replies={replies} renderReply={renderPart} />)
    expect(screen.getAllByTestId('reply-run')).toHaveLength(1)
    expect(screen.getByRole('button', { name: /earlier/i }).textContent).toContain('2')
  })

  it('never splits a run across the fold', () => {
    // Runs: [a0 a1 a2] [b3] [c4]. Keeping 3 replies would cut the first run; it
    // shows whole instead.
    const replies = [m('a', 0), m('a', 1), m('a', 2), m('b', 3), m('c', 4)]
    render(<ThreadReplies replies={replies} renderReply={renderPart} />)
    expect(screen.queryByRole('button', { name: /earlier/i })).toBeNull()
    expect(screen.getAllByTestId('reply-run')).toHaveLength(3)
  })
})

describe('ThreadReplies', () => {
  const replies = [2, 3, 4, 5, 6].map((d) => c(`u${d}`, d, 't'))
  const renderReply = (ev: TimelineEvent) => <p>{ev.body}</p>

  it('folds all but the newest replies, and unfolds on demand', () => {
    render(<ThreadReplies replies={replies} renderReply={renderReply} />)
    expect(screen.queryByText('u2 says')).toBeNull()
    expect(screen.getByText('u6 says')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: /earlier/i }))
    expect(screen.getByText('u2 says')).toBeTruthy()
  })

  it('wraps the marker label instead of cutting it, and truncates only the preview', () => {
    render(<ReplyMarker ev={replies[0]} root={c('kim', 1, 't')} />)
    const [label, preview] = Array.from(screen.getByTestId('reply-marker').querySelectorAll('span'))
    // The label may shrink and wrap, so it never overflows a narrow pane...
    expect(label.className).toContain('min-w-0')
    expect(label.className).toContain('[overflow-wrap:anywhere]')
    expect(label.className).not.toMatch(/whitespace-nowrap|shrink-0/)
    // ...and it is never cut: only the preview truncates.
    expect(label.className).not.toContain('truncate')
    expect(preview.className).toContain('truncate')
  })

  it('reveals a folded reply when its marker is clicked', () => {
    render(
      <>
        <ReplyMarker ev={replies[0]} root={c('kim', 1, 't')} />
        <ThreadReplies replies={replies} renderReply={renderReply} />
      </>,
    )
    expect(screen.getByTestId('reply-marker').textContent).toContain('u2')
    fireEvent.click(screen.getByTestId('reply-marker'))
    // The marker previews the same text, so look in the thread itself.
    const reply = screen.getAllByText('u2 says').find((el) => el.closest('[data-thread-reply]'))
    expect(reply?.parentElement?.className).toContain('ring-accent')
  })
})
