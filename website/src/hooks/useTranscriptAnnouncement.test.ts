/**
 * WHEN the transcript's live region speaks, which is the whole point of the hook.
 *
 * Each case here is a way the region used to say the wrong thing: on every
 * streaming flush, on a session switch, on first load, or forever after one
 * reply.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'

import { ANNOUNCEMENT_HOLD_MS, announcementTail, pendingApprovalsOf, sentSpokenSource, useTranscriptAnnouncement } from './useTranscriptAnnouncement'
import { mintSendId } from '../utils/sendDelivery'

const streaming = { role: 'streaming', content: 'partial' }
const settled = (content: string) => ({ role: 'assistant', content })

beforeEach(() => { vi.useFakeTimers(); vi.setSystemTime(new Date('2026-01-01T00:00:00Z')) })
afterEach(() => { vi.useRealTimers() })

describe('useTranscriptAnnouncement', () => {
  it('stays silent while a turn is streaming, however many flushes arrive', () => {
    const { result, rerender } = renderHook(
      ({ msg }) => useTranscriptAnnouncement(msg, msg.role === 'streaming').announcement,
      { initialProps: { msg: streaming } },
    )
    expect(result.current).toBe('')
    rerender({ msg: { role: 'streaming', content: 'partial and more' } })
    expect(result.current).toBe('')
  })

  it('speaks the settled reply as text once the turn lands', () => {
    const { result, rerender } = renderHook(
      ({ msg }) => useTranscriptAnnouncement(msg, msg.role === 'streaming').announcement,
      { initialProps: { msg: streaming } },
    )
    rerender({ msg: settled('## Done\n\nRan `npm ci` — see [the log](https://x/y).') })
    // Named like the transcript's heading for the reply, and the markdown
    // punctuation the bubble never shows is absent.
    expect(result.current).toBe('Kiro Crew: Done\nRan npm ci — see the log.')
  })

  it('speaks a reply that was never seen streaming when the turn flag drops', () => {
    // A polling host (the app embed) can miss the streaming row entirely: a
    // short reply goes from the user's row to the settled reply between two
    // polls. The host's turn flag still spans the turn, so its end is a landing.
    const { result, rerender } = renderHook(
      ({ msg, running }) => useTranscriptAnnouncement(msg, msg.role === 'streaming' || running).announcement,
      { initialProps: { msg: { role: 'user', content: 'hi' } as { role: string, content: string }, running: true } },
    )
    expect(result.current).toBe('')
    rerender({ msg: settled('Hello'), running: false })
    expect(result.current).toBe('Kiro Crew: Hello')
  })

  it('says nothing on first load or a session switch, only on a landing', () => {
    // No streaming state preceded these, so the transcript merely ENDS in a
    // reply — reading it back would announce a conversation the user opened,
    // not one that just answered them.
    const { result, rerender } = renderHook(
      ({ msg }) => useTranscriptAnnouncement(msg, msg.role === 'streaming').announcement,
      { initialProps: { msg: settled('an old reply') } },
    )
    expect(result.current).toBe('')
    rerender({ msg: settled('a different old reply') })
    expect(result.current).toBe('')
  })

  it('says nothing when a mid-stream session is left for one that ends in a reply', () => {
    // The chat page mounts the hook once for every session, so the streaming
    // state it saw belongs to the session left behind. Taking the switch for a
    // landing would read the idle session's old reply aloud.
    const { result, rerender } = renderHook(
      ({ msg, key }) => useTranscriptAnnouncement(msg, msg.role === 'streaming', undefined, key).announcement,
      { initialProps: { msg: streaming as { role: string, content: string }, key: 'busy' } },
    )
    rerender({ msg: settled('an old reply'), key: 'idle' })
    expect(result.current).toBe('')
    // A turn that then lands in the session switched to is still spoken.
    rerender({ msg: streaming, key: 'idle' })
    rerender({ msg: settled('a new reply'), key: 'idle' })
    expect(result.current).toBe('Kiro Crew: a new reply')
  })

  it('drops a held reply on a session switch, but keeps a held sent message', () => {
    const { result, rerender } = renderHook(
      ({ msg, key }) => useTranscriptAnnouncement(msg, msg?.role === 'streaming', undefined, key),
      { initialProps: { msg: streaming as { role: string, content: string }, key: 'a' } },
    )
    rerender({ msg: settled('done in a'), key: 'a' })
    expect(result.current.announcement).toBe('Kiro Crew: done in a')
    rerender({ msg: settled('old in b'), key: 'b' })
    expect(result.current.announcement).toBe('')
    // A new chat can get its key on the send itself; the sent message stays.
    rerender({ msg: undefined as unknown as { role: string, content: string }, key: null as unknown as string })
    act(() => { result.current.announceSent('hello') })
    rerender({ msg: { role: 'user', content: 'hello' }, key: 'c' })
    expect(result.current.announcement).toBe('You: hello')
  })

  it('drops a held sent message when the reader switches to another session', () => {
    const { result, rerender } = renderHook(
      ({ msg, key }) => useTranscriptAnnouncement(msg, false, undefined, key),
      { initialProps: { msg: settled('in a') as { role: string, content: string }, key: 'a' } },
    )
    act(() => { result.current.announceSent('for a only') })
    expect(result.current.announcement).toBe('You: for a only')
    rerender({ msg: settled('in b'), key: 'b' })
    expect(result.current.announcement).toBe('')
  })

  it('releases the text so it stops being a second copy of the reply', () => {
    const { result, rerender } = renderHook(
      ({ msg }) => useTranscriptAnnouncement(msg, msg.role === 'streaming').announcement,
      { initialProps: { msg: streaming } },
    )
    rerender({ msg: settled('all done') })
    expect(result.current).toBe('Kiro Crew: all done')
    act(() => { vi.advanceTimersByTime(ANNOUNCEMENT_HOLD_MS - 1) })
    expect(result.current).toBe('Kiro Crew: all done')
    act(() => { vi.advanceTimersByTime(1) })
    expect(result.current).toBe('')
  })

  it('speaks a second identical reply, because the region was cleared between', () => {
    const { result, rerender } = renderHook(
      ({ msg }) => useTranscriptAnnouncement(msg, msg.role === 'streaming').announcement,
      { initialProps: { msg: streaming } },
    )
    rerender({ msg: settled('same answer') })
    expect(result.current).toBe('Kiro Crew: same answer')
    rerender({ msg: streaming })
    expect(result.current).toBe('')
    rerender({ msg: settled('same answer') })
    expect(result.current).toBe('Kiro Crew: same answer')
  })

  it('stays silent when a turn ends without a reply', () => {
    const { result, rerender } = renderHook(
      ({ msg }) => useTranscriptAnnouncement(msg, msg.role === 'streaming').announcement,
      { initialProps: { msg: streaming } },
    )
    rerender({ msg: { role: 'stop_event', content: '' } })
    expect(result.current).toBe('')
  })

  it('speaks the sent message with the user label, and keeps it when the reply starts streaming', () => {
    const { result, rerender } = renderHook(
      ({ msg }) => useTranscriptAnnouncement(msg, msg.role === 'streaming'),
      { initialProps: { msg: settled('an old reply') as { role: string, content: string } } },
    )
    act(() => { result.current.announceSent('Run the **tests**') })
    expect(result.current.announcement).toBe('You: Run the tests')
    // The reply begins almost at once; clearing here would cut the sent message off.
    rerender({ msg: streaming })
    expect(result.current.announcement).toBe('You: Run the tests')
    rerender({ msg: settled('They pass.') })
    expect(result.current.announcement).toBe('Kiro Crew: They pass.')
  })

  it("speaks a reply under the host's speaker name when one is given", () => {
    // A crewmate's chat: the reply is the crewmate's, not the product's.
    const { result, rerender } = renderHook(
      ({ msg }) => useTranscriptAnnouncement(msg, msg.role === 'streaming', 'Radar').announcement,
      { initialProps: { msg: streaming as { role: string, content: string } } },
    )
    rerender({ msg: settled('Done.') })
    expect(result.current).toBe('Radar: Done.')
  })

  it('releases the sent message after the hold, and ignores an empty send', () => {
    const { result } = renderHook(() => useTranscriptAnnouncement(undefined, false))
    act(() => { result.current.announceSent('   ') })
    expect(result.current.announcement).toBe('')
    act(() => { result.current.announceSent('hello') })
    expect(result.current.announcement).toBe('You: hello')
    act(() => { vi.advanceTimersByTime(ANNOUNCEMENT_HOLD_MS) })
    expect(result.current.announcement).toBe('')
  })
})

describe('useTranscriptAnnouncement approvals', () => {
  type Row = { role: string, content: string, ts?: string, meta?: Record<string, unknown> }
  const now = () => String(Date.now() / 1000)
  const perm = (id: string, tool: string, ts: string, resolved?: string): Row =>
    ({ role: 'permission', content: `[agent] ${tool}`, ts, meta: { approval_id: id, ...(resolved ? { resolved } : {}) } })
  const render = (rows: Row[], key = 's1') => renderHook(
    ({ rows, key }) => useTranscriptAnnouncement(rows[rows.length - 1], false, undefined, key, pendingApprovalsOf(rows), rows.length).announcement,
    { initialProps: { rows, key } },
  )

  it('speaks a new approval once, naming the operation', () => {
    const { result, rerender } = render([{ role: 'user', content: 'ship it' }])
    const rows = [{ role: 'user', content: 'ship it' }, perm('a1', 'npm publish', now())]
    rerender({ rows, key: 's1' })
    expect(result.current).toBe('Kiro Crew is waiting for your approval: npm publish')
    act(() => { vi.advanceTimersByTime(ANNOUNCEMENT_HOLD_MS) })
    rerender({ rows: [...rows, { role: 'tool', content: 'x' }], key: 's1' })
    expect(result.current).toBe('')
  })

  it('stays silent for an approval that was already waiting when the chat opened', () => {
    const old = String(Date.now() / 1000 - 3600)
    const { result, rerender } = render([])
    // The transcript loads after the hook mounts: new to the hook, old to the reader.
    rerender({ rows: [perm('old', 'rm -rf build', old)], key: 's1' })
    expect(result.current).toBe('')
  })

  it('stays silent for a waiting approval in a session switched to', () => {
    const { result, rerender } = render([{ role: 'assistant', content: 'hi' }], 'a')
    rerender({ rows: [perm('b1', 'deploy', now())], key: 'b' })
    expect(result.current).toBe('')
  })

  it('speaks a new approval whose timestamp is behind the browser clock', () => {
    // The gateway's clock and the browser's are never compared: a remote
    // dashboard whose clock runs ahead still hears a new request.
    const { result, rerender } = render([{ role: 'user', content: 'go' }])
    const gatewayBehind = String(Date.now() / 1000 - 3600)
    rerender({ rows: [{ role: 'user', content: 'go' }, perm('a9', 'deploy', gatewayBehind)], key: 's1' })
    expect(result.current).toBe('Kiro Crew is waiting for your approval: deploy')
  })

  it('speaks only the new request after an earlier one is resolved', () => {
    const { result, rerender } = render([perm('a1', 'ls', now())])
    act(() => { vi.advanceTimersByTime(ANNOUNCEMENT_HOLD_MS) })
    rerender({ rows: [perm('a1', 'ls', now(), 'approved'), perm('a2', 'npm publish', now())], key: 's1' })
    expect(result.current).toBe('Kiro Crew is waiting for your approval: npm publish')
  })

  it('takes over from a held sent message', () => {
    const hook = renderHook(
      ({ rows }) => useTranscriptAnnouncement(rows[rows.length - 1], false, undefined, 's1', pendingApprovalsOf(rows), rows.length),
      { initialProps: { rows: [] as Row[] } },
    )
    act(() => { hook.result.current.announceSent('deploy please') })
    expect(hook.result.current.announcement).toBe('You: deploy please')
    const sent = { role: 'user', content: 'deploy please', meta: { clientTs: 'c1' } }
    hook.rerender({ rows: [sent] })
    hook.rerender({ rows: [sent, perm('d1', 'cdk deploy', now())] })
    expect(hook.result.current.announcement).toBe('Kiro Crew is waiting for your approval: cdk deploy')
  })

  it('does not read collapsed reasoning or option markers as the reply', () => {
    const { result, rerender } = renderHook(
      ({ msg }) => useTranscriptAnnouncement(msg, msg.role === 'streaming').announcement,
      { initialProps: { msg: { role: 'streaming', content: '' } } },
    )
    rerender({ msg: { role: 'assistant', content: '<thinking>weigh it</thinking>Pick one.\n\n[OPTIONS: Yes | No]' } })
    expect(result.current).toBe('Kiro Crew: Pick one.')
  })
})

describe('useTranscriptAnnouncement rows that arrive finished', () => {
  type Row = { role: string, content: string, ts?: string, meta?: Record<string, unknown> }
  const now = () => String(Date.now() / 1000)
  const render = (rows: Row[], streamingNow = false) => renderHook(
    ({ rows, streamingNow }) => useTranscriptAnnouncement(rows[rows.length - 1], streamingNow, undefined, 's1', pendingApprovalsOf(rows), rows.length).announcement,
    { initialProps: { rows, streamingNow } },
  )

  it('speaks a channel reply appended whole, with no streaming before it', () => {
    const { result, rerender } = render([{ role: 'user', content: 'status?' }])
    vi.advanceTimersByTime(1000)
    rerender({ rows: [{ role: 'user', content: 'status?' }, { role: 'assistant', content: 'All green.', ts: now(), meta: { mid: 'm2' } }], streamingNow: false })
    expect(result.current).toBe('Kiro Crew: All green.')
  })

  it('stays silent for a finished reply the first load brings in', () => {
    const { result, rerender } = render([])
    // The first non-empty transcript is the baseline, however recent its rows.
    rerender({ rows: [{ role: 'assistant', content: 'old answer', ts: now(), meta: { mid: 'm1' } }], streamingNow: false })
    expect(result.current).toBe('')
  })

  it('speaks a new reply whose timestamp is behind the browser clock', () => {
    const { result, rerender } = render([{ role: 'user', content: 'status?' }])
    const gatewayBehind = String(Date.now() / 1000 - 3600)
    rerender({ rows: [{ role: 'user', content: 'status?' }, { role: 'assistant', content: 'Late clock.', ts: gatewayBehind, meta: { mid: 'm3' } }], streamingNow: false })
    expect(result.current).toBe('Kiro Crew: Late clock.')
  })

  it('speaks a local send failure that carries no id or time', () => {
    const { result, rerender } = render([{ role: 'user', content: 'hi', meta: { clientTs: 'c1' } }])
    rerender({ rows: [{ role: 'user', content: 'hi', meta: { clientTs: 'c1' } }, { role: 'error', content: 'Could not reach the gateway.' }], streamingNow: false })
    expect(result.current).toBe('Kiro Crew: Could not reach the gateway.')
  })

  it('speaks the error a streaming turn ended in', () => {
    const { result, rerender } = render([{ role: 'streaming', content: 'part' }], true)
    rerender({ rows: [{ role: 'error', content: 'The model refused the request.', ts: now(), meta: { mid: 'e1' } }], streamingNow: false })
    expect(result.current).toBe('Kiro Crew: The model refused the request.')
  })

  it('speaks an error that arrives before anything streamed', () => {
    const { result, rerender } = render([{ role: 'user', content: 'hi' }])
    vi.advanceTimersByTime(1000)
    rerender({ rows: [{ role: 'user', content: 'hi' }, { role: 'error', content: 'Session failed to start.', ts: now(), meta: { mid: 'e2' } }], streamingNow: false })
    expect(result.current).toBe('Kiro Crew: Session failed to start.')
  })

  it('does not speak a reply again when it later gets its server identity', () => {
    const { result, rerender } = render([{ role: 'streaming', content: 'x' }], true)
    const at = now()
    rerender({ rows: [{ role: 'assistant', content: 'Done.', ts: at }], streamingNow: false })
    expect(result.current).toBe('Kiro Crew: Done.')
    act(() => { vi.advanceTimersByTime(ANNOUNCEMENT_HOLD_MS) })
    rerender({ rows: [{ role: 'assistant', content: 'Done.', ts: at, meta: { mid: 'm9' } }], streamingNow: false })
    expect(result.current).toBe('')
  })

  it('does not speak a streamed reply again when the reload brings its server copy', () => {
    // The streamed row settles with no server ts or mid; the reload after the
    // turn brings the same reply with both.
    const { result, rerender } = render([{ role: 'streaming', content: 'x' }], true)
    rerender({ rows: [{ role: 'assistant', content: 'Done.' }], streamingNow: false })
    expect(result.current).toBe('Kiro Crew: Done.')
    act(() => { vi.advanceTimersByTime(1000) })
    rerender({ rows: [{ role: 'assistant', content: 'Done.\n', ts: now(), meta: { mid: 'm7' } }], streamingNow: false })
    expect(result.current).toBe('Kiro Crew: Done.')
  })

  it('speaks a second, different row that reads the same as the first', () => {
    const { result, rerender } = render([{ role: 'user', content: 'ping' }])
    vi.advanceTimersByTime(1000)
    const r1 = { role: 'assistant', content: 'pong', ts: now(), meta: { mid: 'r1' } }
    rerender({ rows: [r1], streamingNow: false })
    expect(result.current).toBe('Kiro Crew: pong')
    act(() => { vi.advanceTimersByTime(ANNOUNCEMENT_HOLD_MS) })
    rerender({ rows: [r1, { role: 'assistant', content: 'pong', ts: now(), meta: { mid: 'r2' } }], streamingNow: false })
    expect(result.current).toBe('Kiro Crew: pong')
  })

  it('says nothing for an invisible reply the renderer does not draw', () => {
    const { result, rerender } = render([{ role: 'user', content: 'tick', meta: { clientTs: 'c1' } }])
    rerender({ rows: [{ role: 'user', content: 'tick', meta: { clientTs: 'c1' } }, { role: 'assistant', content: '\u200b', meta: { mid: 'q1' } }], streamingNow: false })
    expect(result.current).toBe('')
  })

  it('speaks a later identical reply after an intervening turn', () => {
    // The first reply settles with no mid; then a user turn, then a finished
    // reply with the same text and a mid. That is a new row, not enrichment.
    const { result, rerender } = render([{ role: 'streaming', content: 'x', meta: { clientTs: 's1' } }], true)
    rerender({ rows: [{ role: 'assistant', content: 'OK.', meta: { clientTs: 's1' } }], streamingNow: false })
    expect(result.current).toBe('Kiro Crew: OK.')
    act(() => { vi.advanceTimersByTime(ANNOUNCEMENT_HOLD_MS) })
    const user = { role: 'user', content: 'again', meta: { clientTs: 'u2' } }
    rerender({ rows: [{ role: 'assistant', content: 'OK.', meta: { clientTs: 's1' } }, user], streamingNow: false })
    rerender({ rows: [{ role: 'assistant', content: 'OK.', meta: { clientTs: 's1' } }, user, { role: 'assistant', content: 'OK.', ts: now(), meta: { mid: 'm5' } }], streamingNow: false })
    expect(result.current).toBe('Kiro Crew: OK.')
  })

  it('names every approval that arrives at once', () => {
    const perm = (mid: string, tool: string): Row => ({ role: 'permission', content: `[agent] ${tool}`, ts: now(), meta: { mid, approval_id: mid } })
    const { result, rerender } = render([{ role: 'user', content: 'go', meta: { clientTs: 'c1' } }])
    rerender({ rows: [{ role: 'user', content: 'go', meta: { clientTs: 'c1' } }, perm('p1', 'npm ci'), perm('p2', 'npm publish')], streamingNow: false })
    expect(result.current).toBe('Kiro Crew is waiting for your approval: npm ci and npm publish')
  })

  it('does not speak a system notice such as a compaction as a reply', () => {
    const { result, rerender } = render([{ role: 'user', content: 'hi', meta: { clientTs: 'c1' } }])
    rerender({ rows: [{ role: 'user', content: 'hi', meta: { clientTs: 'c1' } }, { role: 'assistant', content: 'Compacted: a long hidden summary', meta: { mid: 'k1', kind: 'compaction' } }], streamingNow: false })
    expect(result.current).toBe('')
  })

  it("speaks someone else's message that arrives in the transcript", () => {
    const { result, rerender } = render([{ role: 'assistant', content: 'hi', meta: { mid: 'a1' } }])
    rerender({ rows: [{ role: 'assistant', content: 'hi', meta: { mid: 'a1' } }, { role: 'user', content: 'from Ann', meta: { mid: 'u9', author: { id: 'p_ann' } } }], streamingNow: false })
    expect(result.current).toBe('You: from Ann')
  })

  it("does not speak the reader's own optimistic send row again", () => {
    const { result, rerender } = render([{ role: 'assistant', content: 'hi', meta: { mid: 'a1' } }])
    rerender({ rows: [{ role: 'assistant', content: 'hi', meta: { mid: 'a1' } }, { role: 'user', content: 'mine', meta: { sendId: 's1', optimistic: true } }], streamingNow: false })
    expect(result.current).toBe('')
  })

  it("speaks a teammate's message that carries the teammate's own send id", () => {
    // The server keeps the sender's `sendId` on the row it broadcasts.
    const { result, rerender } = render([{ role: 'assistant', content: 'hi', meta: { mid: 'a1' } }])
    rerender({ rows: [{ role: 'assistant', content: 'hi', meta: { mid: 'a1' } }, { role: 'user', content: 'from Ann', meta: { mid: 'u9', sendId: 's-ann-1', author: { id: 'p_ann' } } }], streamingNow: false })
    expect(result.current).toBe('You: from Ann')
  })

  it("does not speak the server copy of a row this tab sent", () => {
    const sendId = mintSendId()
    const { result, rerender } = render([{ role: 'assistant', content: 'hi', meta: { mid: 'a1' } }])
    rerender({ rows: [{ role: 'assistant', content: 'hi', meta: { mid: 'a1' } }, { role: 'user', content: 'mine', meta: { mid: 'u1', sendId } }], streamingNow: false })
    expect(result.current).toBe('')
  })

  it('speaks a notice row that arrives, as its text alone', () => {
    const { result, rerender } = render([{ role: 'user', content: 'hi', meta: { clientTs: 'c1' } }])
    rerender({ rows: [{ role: 'user', content: 'hi', meta: { clientTs: 'c1' } }, { role: 'notice', content: 'Delivery not confirmed yet.' }], streamingNow: false })
    expect(result.current).toBe('Delivery not confirmed yet.')
  })

  it('speaks a new approval that reuses an earlier request id', () => {
    const perm = (mid: string, tool: string, resolved?: string): Row =>
      ({ role: 'permission', content: `[agent] ${tool}`, ts: now(), meta: { mid, approval_id: 'req-1', ...(resolved ? { resolved } : {}) } })
    const { result, rerender } = render([perm('p1', 'ls')])
    act(() => { vi.advanceTimersByTime(ANNOUNCEMENT_HOLD_MS) })
    rerender({ rows: [perm('p1', 'ls', 'approved'), perm('p2', 'npm publish')], streamingNow: false })
    expect(result.current).toBe('Kiro Crew is waiting for your approval: npm publish')
  })
})

describe('useTranscriptAnnouncement sent messages', () => {
  it('changes the region when the same message is sent twice inside the hold', () => {
    const { result } = renderHook(() => useTranscriptAnnouncement(undefined, false))
    act(() => { result.current.announceSent('yes') })
    const first = result.current.announcement
    act(() => { result.current.announceSent('yes') })
    expect(first).toBe('You: yes')
    expect(result.current.announcement).not.toBe(first)
    expect(result.current.announcement.trim()).toBe('You: yes')
  })

  it('keeps a backslash in a POSIX file name, as the attachment card does', () => {
    expect(sentSpokenSource('[attached_file 1] /tmp/foo\\bar')).toBe('foo\\bar')
    expect(sentSpokenSource('[attached_file 1] C:\\Users\\me\\report.pdf')).toBe('report.pdf')
  })

  it('reads a gateway status row as its localized card copy, not its raw content', () => {
    const row = { role: 'error', content: 'RAW ENGLISH FALLBACK', cls: '', meta: { notice: 'connection_lost_give_up' } }
    const tail = announcementTail([row])
    expect(tail?.content).not.toBe('RAW ENGLISH FALLBACK')
    expect(announcementTail([{ role: 'assistant', content: 'plain', cls: '' }])?.content).toBe('plain')
  })

  it('names an attachment by its file name, never its marker or path', () => {
    expect(sentSpokenSource('review this\n[attached_file 1] /home/u/docs/report.pdf\n[attached_dir 2] /home/u/src/'))
      .toBe('review this\nreport.pdf\nsrc')
  })

  it("speaks a teammate's message that reads the same as the reader's last send", () => {
    const base = [{ role: 'assistant', content: 'hi', meta: { mid: 'a1' } }]
    const { result, rerender } = renderHook(
      ({ rows }) => useTranscriptAnnouncement(rows[rows.length - 1], false, undefined, 's1', [], rows.length),
      { initialProps: { rows: base as Row[] } },
    )
    act(() => { result.current.announceSent('yes') })
    act(() => { vi.advanceTimersByTime(ANNOUNCEMENT_HOLD_MS) })
    rerender({ rows: [...base, { role: 'user', content: 'yes', meta: { mid: 'u2', author: { id: 'p_ann' } } }] })
    expect(result.current.announcement).toBe('You: yes')
  })

  it('treats an id-less copy of the sent text as the send once, then speaks a later one', () => {
    const base = [{ role: 'assistant', content: 'hi', meta: { mid: 'a1' } }]
    const { result, rerender } = renderHook(
      ({ rows }) => useTranscriptAnnouncement(rows[rows.length - 1], false, undefined, 's1', [], rows.length),
      { initialProps: { rows: base as Row[] } },
    )
    act(() => { result.current.announceSent('yes') })
    act(() => { vi.advanceTimersByTime(ANNOUNCEMENT_HOLD_MS) })
    const echo = { role: 'user', content: 'yes', meta: { mid: 'u2' } }
    rerender({ rows: [...base, echo] })
    expect(result.current.announcement).toBe('')
    rerender({ rows: [...base, echo, { role: 'user', content: 'yes', meta: { mid: 'u3' } }] })
    expect(result.current.announcement).toBe('You: yes')
  })

  it('speaks a later unstamped row with the sent text once the send came back with its id', () => {
    const base = [{ role: 'assistant', content: 'hi', meta: { mid: 'a1' } }]
    const { result, rerender } = renderHook(
      ({ rows }) => useTranscriptAnnouncement(rows[rows.length - 1], false, undefined, 's1', [], rows.length),
      { initialProps: { rows: base as Row[] } },
    )
    act(() => { result.current.announceSent('yes') })
    act(() => { vi.advanceTimersByTime(ANNOUNCEMENT_HOLD_MS) })
    const own = { role: 'user', content: 'yes', meta: { mid: 'u2', sendId: mintSendId() } }
    rerender({ rows: [...base, own] })
    expect(result.current.announcement).toBe('')
    rerender({ rows: [...base, own, { role: 'user', content: 'yes', meta: { mid: 'u3' } }] })
    expect(result.current.announcement).toBe('You: yes')
  })
})
