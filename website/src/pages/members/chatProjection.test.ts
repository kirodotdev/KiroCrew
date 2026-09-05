import { describe, it, expect } from 'vitest'
import type { ChatMessage } from '../../types'
import { projectChatView, segmentTurns, isQuietReply } from './chatProjection'

const LONG = 'This is a substantive reply that says something new about the work. '.repeat(3)

function row(role: string, content = '', meta?: Record<string, unknown>, ts?: string): ChatMessage {
  return { role, content, cls: `msg msg-${role}`, ts, meta }
}
const user = (c = 'hi', mid?: string) => row('user', c, mid ? { mid } : undefined)
const tool = (name: string, purpose?: string) => row('tool', `🔧 ${name}`, { kind: 'tool', purpose })
const assistant = (c = LONG, mid?: string) => row('assistant', c, mid ? { mid } : undefined)
const nudge = (cycle: number) => row('nudge', `[auto-nudge cycle ${cycle}]\ncheck`, { nudge: { cycle }, mid: `n${cycle}` })

describe('segmentTurns', () => {
  it('splits at user / nudge / subagent / inject openers, leading rows form an opener-less turn', () => {
    const msgs = [row('system', 'boot'), user('a'), tool('grep'), assistant('x'), nudge(1), assistant('y'), row('inject', 'z')]
    const turns = segmentTurns(msgs)
    expect(turns.map(t => t.opener?.role ?? null)).toEqual([null, 'user', 'nudge', 'inject'])
    expect(turns[1].rows.map(r => r.role)).toEqual(['user', 'tool', 'assistant'])
  })
})

describe('isQuietReply', () => {
  it('treats absent, whitespace, zero-width, and short replies as quiet', () => {
    expect(isQuietReply(null)).toBe(true)
    expect(isQuietReply(assistant('  \n'))).toBe(true)
    expect(isQuietReply(assistant('\u200B'))).toBe(true)
    expect(isQuietReply(assistant('Nothing new.'))).toBe(true)
    expect(isQuietReply(assistant(LONG))).toBe(false)
  })
})

describe('projectChatView', () => {
  it('hides tool rows and keeps the user row and the final reply', () => {
    const out = projectChatView([user('do it'), tool('grep', 'Find callers'), tool('read'), assistant('done', 'a1')], { running: false })
    expect(out.map(m => m.role)).toEqual(['user', 'assistant'])
    expect(out[1].content).toBe('done')
  })

  it('selects the LAST assistant row of a turn as the reply; earlier ones join the process', () => {
    const out = projectChatView([user(), assistant('first draft'), tool('edit'), assistant('final answer')], { running: false })
    expect(out.map(m => m.role)).toEqual(['user', 'assistant'])
    expect(out[1].content).toBe('final answer')
    const process = out[1].meta?.chat_process as ChatMessage[]
    expect(process.map(p => p.role)).toEqual(['assistant', 'tool'])
    expect(process[0].content).toBe('first draft')
  })

  it('attaches chat_process (hidden rows) and chat_process_count (tool rows) without mutating the source', () => {
    const src = [user(), tool('grep'), row('notice', 'model fell back'), tool('read'), assistant('ok', 'a1')]
    const out = projectChatView(src, { running: false })
    const reply = out[1]
    expect(reply.meta?.chat_process_count).toBe(2)
    expect((reply.meta?.chat_process as ChatMessage[]).map(p => p.role)).toEqual(['tool', 'notice', 'tool'])
    expect(reply.meta?.mid).toBe('a1')
    // Shallow copy: the stored row did not gain the projection fields.
    expect(src[4].meta?.chat_process).toBeUndefined()
    expect(reply).not.toBe(src[4])
  })

  it('keeps the streaming row of the trailing turn while running, drops it once idle', () => {
    const src = [user(), tool('grep'), row('streaming', 'partial…')]
    expect(projectChatView(src, { running: true }).map(m => m.role)).toEqual(['user', 'streaming'])
    expect(projectChatView(src, { running: false }).map(m => m.role)).toEqual(['user'])
  })

  it('folds three silent nudge rounds into ONE chat_fold row carrying every folded row', () => {
    const src = [
      user('start'), assistant(LONG),
      nudge(1), tool('check'), assistant('\u200B'),
      nudge(2), tool('check'), assistant('Nothing new.'),
      nudge(3), tool('check'),
      user('any news?'), assistant(LONG),
    ]
    const out = projectChatView(src, { running: false })
    expect(out.map(m => m.role)).toEqual(['user', 'assistant', 'chat_fold', 'user', 'assistant'])
    const fold = out[2]
    expect(fold.meta?.kind).toBe('silent_rounds')
    expect(fold.meta?.count).toBe(3)
    expect((fold.meta?.rows as ChatMessage[]).length).toBe(8)
    expect(fold.meta?.mid).toBe('fold-n1')
  })

  it('does not fold a nudge round whose reply is substantive; it renders as a normal reply with its process', () => {
    const src = [user('start'), assistant(LONG), nudge(1), tool('check'), assistant(LONG + ' Found a regression.')]
    const out = projectChatView(src, { running: false })
    expect(out.map(m => m.role)).toEqual(['user', 'assistant', 'assistant'])
    expect(out[2].meta?.chat_process_count).toBe(1)
    // The nudge opener itself is part of that reply's process, not a row.
    expect((out[2].meta?.chat_process as ChatMessage[]).map(p => p.role)).toEqual(['nudge', 'tool'])
  })

  it('never folds the trailing nudge round while the session is still running', () => {
    const src = [user('start'), assistant(LONG), nudge(1), tool('check')]
    expect(projectChatView(src, { running: true }).map(m => m.role)).toEqual(['user', 'assistant'])
    expect(projectChatView(src, { running: false }).map(m => m.role)).toEqual(['user', 'assistant', 'chat_fold'])
  })

  it('keeps an escalation row, even inside an otherwise quiet nudge round', () => {
    const esc = row('escalation', 'Need a decision', { kind: 'escalation', escalation_id: 'e1', from_session: 's', mid: 'm1' })
    const src = [user('go'), assistant(LONG), nudge(1), tool('check'), esc, assistant('\u200B')]
    const out = projectChatView(src, { running: false })
    // The invisible-only reply row stays (the assistant renderer draws nothing
    // for it); what matters is that the escalation is not folded away.
    expect(out.map(m => m.role)).toEqual(['user', 'assistant', 'escalation', 'assistant'])
    expect(out[2]).toBe(esc)
  })

  it('keeps a member\u2019s file row (file_send download / media card), even inside a quiet nudge round', () => {
    const file = row('file', 'report.pdf', { kind: 'file', path: '/outbox/report.pdf', mid: 'f1' })
    const src = [user('go'), tool('write'), file, assistant('Sent the report.', 'a1')]
    const out = projectChatView(src, { running: false })
    expect(out.map(m => m.role)).toEqual(['user', 'file', 'assistant'])
    expect(out[1]).toBe(file)
    // Not part of the reply's process trail either: it is its own row.
    expect((out[2].meta?.chat_process as ChatMessage[]).map(p => p.role)).toEqual(['tool'])
    // A file keeps its nudge round out of the silent fold.
    const quiet = [user('start'), assistant(LONG), nudge(1), tool('check'), file, assistant('\u200B')]
    expect(projectChatView(quiet, { running: false }).map(m => m.role)).toEqual(['user', 'assistant', 'file', 'assistant'])
  })

  it('keeps permission rows (approval UI); thinking and subagent rows join the process trail, system rows are dropped', () => {
    const perm = row('permission', 'allow?', { approval_id: 'p1' })
    const thinking = row('thinking', 'hmm')
    const sys = row('system', 'x')
    const src = [user(), thinking, perm, sys, assistant('ok')]
    const out = projectChatView(src, { running: false })
    expect(out.map(m => m.role)).toEqual(['user', 'permission', 'assistant'])
    expect(out[1]).toBe(perm)
    // The reasoning row is reachable behind the reply's View process, the
    // system row is nowhere.
    const process = out[2].meta?.chat_process as ChatMessage[]
    expect(process).toEqual([thinking])
    expect(process).not.toContain(sys)
  })

  it('a subagent opener lands in its own turn\u2019s process trail rather than vanishing', () => {
    const sub = row('subagent', 'spawned worker', { mid: 'sa1' })
    const src = [user('go'), assistant(LONG, 'a1'), sub, tool('check'), assistant('done', 'a2')]
    const out = projectChatView(src, { running: false })
    expect(out.map(m => m.role)).toEqual(['user', 'assistant', 'assistant'])
    expect(out[2].meta?.chat_process).toEqual([sub, src[3]])
    expect(out[2].meta?.chat_process_count).toBe(1)
  })
})
