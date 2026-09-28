import { readFileSync } from 'node:fs'
import path from 'node:path'
import { describe, expect, it } from 'vitest'
import type { ChatMessage } from '../../types'
import {
  crewmateBubbleClass,
  crewmateRunPosition,
  filterCrewmateChat,
  isCrewmateChatRow,
  isCrewmateSpeech,
  opensCrewmateRun,
} from './crewmateBubbles'

const at = (iso: string) => iso
const user = (ts: string, content = 'hi'): ChatMessage => ({ role: 'user', content, cls: 'msg msg-u', ts })
const said = (ts: string, content = 'Found the cause.'): ChatMessage => ({ role: 'assistant', content, cls: '', ts })
const tool = (ts: string): ChatMessage => ({ role: 'tool', content: '🔧 gh issue list', cls: '', ts, meta: { tool_call_id: `tc-${ts}` } })

describe('filterCrewmateChat', () => {
  it('drops the machinery and keeps what the crewmate says plus the user', () => {
    const rows: ChatMessage[] = [
      user(at('2026-09-20T09:12:00Z')),
      tool(at('2026-09-20T09:12:06Z')),
      { role: 'thinking', content: 'reasoning', cls: '', ts: at('2026-09-20T09:12:07Z') },
      said(at('2026-09-20T09:14:10Z')),
      { role: 'nudge', content: '[auto-nudge cycle 41]\nPatrol.', cls: 'msg msg-nudge', ts: at('2026-09-20T10:00:00Z'), meta: { nudge: { cycle: 41 } } },
      tool(at('2026-09-20T10:00:04Z')),
      // The say-nothing row a quiet patrol ends on.
      said(at('2026-09-20T10:00:09Z'), '\u200B'),
      { role: 'inject', content: '[Cron notification from "nightly"]\nSweep.\n[End of cron notification]', cls: 'msg msg-sys', ts: at('2026-09-21T02:00:00Z'), meta: { injectKind: 'cron', cronLabel: 'nightly' } },
      { role: 'subagent', content: '[Subagent completion event]\nAgent `7c2e91ab` completed ✅', cls: 'msg msg-sys', ts: at('2026-09-22T06:50:12Z') },
      said(at('2026-09-22T06:51:04Z'), 'Root cause of #4213: …'),
      { role: 'tool_call', content: 'x', cls: '', ts: at('2026-09-22T06:51:05Z') },
      { role: 'tool_result', content: 'y', cls: '', ts: at('2026-09-22T06:51:06Z') },
      // Turn-end marker and a state-only system row: nothing draws them.
      { role: 'done', content: '', cls: '', ts: at('2026-09-22T06:51:07Z') },
      { role: 'system', content: 'session reset', cls: '', ts: at('2026-09-22T06:51:08Z') },
    ]
    expect(filterCrewmateChat(rows).map(m => [m.role, m.ts])).toEqual([
      ['user', '2026-09-20T09:12:00Z'],
      ['assistant', '2026-09-20T09:14:10Z'],
      ['assistant', '2026-09-22T06:51:04Z'],
    ])
  })

  it('keeps rows that need or inform the user: errors, notices, files, approvals, the live stream', () => {
    const rows: ChatMessage[] = [
      { role: 'error', content: 'boom', cls: '', ts: at('2026-09-22T06:00:00Z') },
      { role: 'notice', content: 'note', cls: '', ts: at('2026-09-22T06:00:01Z') },
      { role: 'file', content: '{"path":"/a.txt"}', cls: '', ts: at('2026-09-22T06:00:02Z') },
      { role: 'permission', content: 'approve?', cls: '', ts: at('2026-09-22T06:00:03Z'), meta: { approval_id: 'a1' } },
      { role: 'mcp_oauth', content: 'auth', cls: '', ts: at('2026-09-22T06:00:04Z') },
      { role: 'streaming', content: 'typing…', cls: '' },
    ]
    expect(filterCrewmateChat(rows)).toBe(rows)
    for (const m of rows) expect(isCrewmateChatRow(m)).toBe(true)
  })

  it('an all-machinery transcript filters to nothing (so the empty hint can show)', () => {
    const rows: ChatMessage[] = [
      { role: 'nudge', content: '[auto-nudge cycle 1]', cls: '', ts: at('2026-09-22T04:00:00Z') },
      tool(at('2026-09-22T04:00:04Z')),
      said(at('2026-09-22T04:00:09Z'), '\u200B'),
      { role: 'done', content: '', cls: '', ts: at('2026-09-22T04:00:10Z') },
      // A resolved approval draws nothing (its renderer returns null); kept, it
      // would count as content and hide the quiet hint behind a blank chat.
      { role: 'permission', content: 'ok?', cls: '', ts: at('2026-09-22T04:00:11Z'), meta: { approval_id: 'a1', resolved: 'approved' } },
    ]
    expect(filterCrewmateChat(rows)).toEqual([])
  })

  it('a pending approval is kept, a resolved one is not — whatever the resolution', () => {
    expect(isCrewmateChatRow({ role: 'permission', content: 'ok?', cls: '', meta: { approval_id: 'a1' } })).toBe(true)
    for (const resolved of ['approved', 'denied', 'cancelled', true]) {
      expect(isCrewmateChatRow({ role: 'permission', content: 'ok?', cls: '', meta: { approval_id: 'a1', resolved } })).toBe(false)
    }
  })

  it('the stop card is the one system row that is drawn', () => {
    const stop: ChatMessage = { role: 'system', content: '{"kind":"stop_event"}', cls: '', ts: at('2026-09-22T06:00:00Z'), kind: 'stop_event', meta: { kind: 'stop_event' } }
    expect(isCrewmateChatRow(stop)).toBe(true)
  })

  it('hides gateway status written under the assistant role', () => {
    const compaction: ChatMessage = { role: 'assistant', content: 'Context summary…', cls: '', ts: at('2026-09-22T06:00:00Z'), meta: { kind: 'compaction' } }
    expect(isCrewmateChatRow(compaction)).toBe(false)
  })

  it('returns the same array when nothing is dropped', () => {
    const rows = [user(at('2026-09-20T09:12:00Z')), said(at('2026-09-20T09:14:10Z'))]
    expect(filterCrewmateChat(rows)).toBe(rows)
  })
})

describe('crewmateRunPosition', () => {
  const t0 = Date.parse('2026-09-22T06:51:04Z')
  const iso = (offsetMs: number) => new Date(t0 + offsetMs).toISOString()

  it('stamps a three-message run first / middle / last', () => {
    const rows = [user(iso(-60_000)), said(iso(0)), said(iso(27_000)), said(iso(66_000))]
    expect(rows.map((_, i) => rows[i].role === 'assistant' ? crewmateRunPosition(rows, i) : null))
      .toEqual([null, 'start', 'cont', 'end'])
  })

  it('a lone message is single', () => {
    const rows = [user(iso(-60_000)), said(iso(0)), user(iso(30_000))]
    expect(crewmateRunPosition(rows, 1)).toBe('single')
  })

  it('a user message breaks the run', () => {
    const rows = [said(iso(0)), user(iso(10_000)), said(iso(20_000))]
    expect(crewmateRunPosition(rows, 0)).toBe('single')
    expect(crewmateRunPosition(rows, 2)).toBe('single')
  })

  it('a run is one turn: a filtered patrol wake between two replies starts a new run', () => {
    // Two turns three minutes apart: turn 1 (one reply), a nudge the filter
    // drops, turn 2 (two replies). Drawn adjacency alone would chain all three.
    const transcript: ChatMessage[] = [
      said(iso(0)),
      { role: 'nudge', content: '[auto-nudge cycle 2]', cls: '', ts: iso(120_000) },
      tool(iso(150_000)),
      said(iso(180_000)),
      said(iso(190_000)),
    ]
    const drawn = filterCrewmateChat(transcript)
    expect(drawn.map((m) => m.content)).toEqual([transcript[0].content, transcript[3].content, transcript[4].content])
    expect(crewmateRunPosition(drawn, 0, transcript)).toBe('single')
    expect(crewmateRunPosition(drawn, 1, transcript)).toBe('start')
    expect(crewmateRunPosition(drawn, 2, transcript)).toBe('end')
  })

  it("a turn's own machinery between two replies does not split the run", () => {
    const transcript: ChatMessage[] = [
      said(iso(0)),
      tool(iso(5_000)),
      { role: 'thinking', content: '…', cls: '', ts: iso(6_000) },
      { role: 'done', content: '', cls: '', ts: iso(7_000) },
      said(iso(8_000)),
    ]
    const drawn = filterCrewmateChat(transcript)
    expect(crewmateRunPosition(drawn, 0, transcript)).toBe('start')
    expect(crewmateRunPosition(drawn, 1, transcript)).toBe('end')
  })

  it('an envelope between two replies is a turn boundary too', () => {
    for (const role of ['inject', 'subagent']) {
      const transcript: ChatMessage[] = [
        said(iso(0)),
        { role, content: '[Cron notification …]', cls: '', ts: iso(60_000) },
        said(iso(120_000)),
      ]
      const drawn = filterCrewmateChat(transcript)
      expect(crewmateRunPosition(drawn, 0, transcript)).toBe('single')
      expect(crewmateRunPosition(drawn, 1, transcript)).toBe('single')
    }
  })

  it('a completion envelope written under the ASSISTANT role is a turn boundary too', () => {
    // The gateway lands a workflow or sub-agent result as an assistant row and
    // that result wakes a follow-up turn. Filtered out of the drawn list, it must
    // still split the two replies it sits between — an assistant row is not
    // pass-through when it is an envelope.
    const envelopes: ChatMessage[] = [
      { role: 'assistant', content: '[Workflow completion event]\nWorkflow `triage` (wf_9f2c) → **finished**\n\nDone.', cls: '', ts: iso(60_000) },
      { role: 'assistant', content: '[Subagent completion event]\nAgent `w-triage-2` ✅\n\nTriaged 4 issues.', cls: '', ts: iso(60_000) },
      { role: 'assistant', content: '[Subagent batch completion event]\nBatch results 1/2 — 3 of 6 delivered, 3 still running.\n\n- w1 ✅', cls: '', ts: iso(60_000) },
    ]
    for (const envelope of envelopes) {
      const transcript: ChatMessage[] = [said(iso(0)), envelope, said(iso(120_000))]
      const drawn = filterCrewmateChat(transcript)
      expect(drawn).toHaveLength(2)
      expect(crewmateRunPosition(drawn, 0, transcript)).toBe('single')
      expect(crewmateRunPosition(drawn, 1, transcript)).toBe('single')
    }
    // …while an assistant row that only QUOTES the prefix is speech, drawn, and
    // breaks the run as any drawn row does (the neighbour is the quote itself).
    const quoted: ChatMessage = { role: 'assistant', content: '[Subagent completion event] is the header my sub-agents send back.', cls: '', ts: iso(60_000) }
    const transcript: ChatMessage[] = [said(iso(0)), quoted, said(iso(120_000))]
    const drawn = filterCrewmateChat(transcript)
    expect(drawn).toHaveLength(3)
    expect(crewmateRunPosition(drawn, 0, transcript)).toBe('start')
    expect(crewmateRunPosition(drawn, 1, transcript)).toBe('cont')
    expect(crewmateRunPosition(drawn, 2, transcript)).toBe('end')
  })

  it('a long silence inside one turn does not break the run (no time rule)', () => {
    const rows = [said(iso(0)), said(iso(30 * 60_000))]
    expect(crewmateRunPosition(rows, 0, rows)).toBe('start')
    expect(crewmateRunPosition(rows, 1, rows)).toBe('end')
  })

  it('a streaming row with no timestamp continues the run', () => {
    const rows: ChatMessage[] = [said(iso(0)), { role: 'streaming', content: 'more…', cls: '' }]
    expect(crewmateRunPosition(rows, 0)).toBe('start')
    expect(crewmateRunPosition(rows, 1)).toBe('end')
  })

  it('a resolved approval between two messages does not split the run', () => {
    const rows: ChatMessage[] = [
      said(iso(0)),
      { role: 'permission', content: 'ok?', cls: '', ts: iso(5_000), meta: { approval_id: 'a1', resolved: 'approved' } },
      said(iso(20_000)),
    ]
    expect(crewmateRunPosition(rows, 0)).toBe('start')
    expect(crewmateRunPosition(rows, 2)).toBe('end')
  })

  it('a stop card is drawn, so it breaks the run like any row the user sees', () => {
    const rows: ChatMessage[] = [
      said(iso(0)),
      { role: 'system', content: '{"kind":"stop_event"}', cls: '', ts: iso(5_000), kind: 'stop_event', meta: { kind: 'stop_event', state: 'stopped' } },
      said(iso(20_000)),
    ]
    expect(crewmateRunPosition(rows, 0)).toBe('single')
    expect(crewmateRunPosition(rows, 2)).toBe('single')
  })

  it('a pending approval is a boundary the user sees, so it breaks the run', () => {
    const rows: ChatMessage[] = [
      said(iso(0)),
      { role: 'permission', content: 'ok?', cls: '', ts: iso(5_000), meta: { approval_id: 'a1' } },
      said(iso(20_000)),
    ]
    expect(crewmateRunPosition(rows, 0)).toBe('single')
    expect(crewmateRunPosition(rows, 2)).toBe('single')
  })

  it('only the run opener carries the author line', () => {
    expect(opensCrewmateRun('single')).toBe(true)
    expect(opensCrewmateRun('start')).toBe(true)
    expect(opensCrewmateRun('cont')).toBe(false)
    expect(opensCrewmateRun('end')).toBe(false)
  })
})

describe('crewmateBubbleClass', () => {
  it('applies the corner rule on the left side only', () => {
    expect(crewmateBubbleClass('single')).toMatch(/\brounded-2xl\b/)
    expect(crewmateBubbleClass('single')).not.toMatch(/rounded-(bl|tl|l)-md/)
    expect(crewmateBubbleClass('start')).toMatch(/\brounded-bl-md\b/)
    expect(crewmateBubbleClass('start')).not.toMatch(/rounded-tl-md|rounded-l-md/)
    expect(crewmateBubbleClass('cont')).toMatch(/\brounded-l-md\b/)
    expect(crewmateBubbleClass('end')).toMatch(/\brounded-tl-md\b/)
    expect(crewmateBubbleClass('end')).not.toMatch(/rounded-bl-md|rounded-l-md/)
    for (const pos of ['single', 'start', 'cont', 'end'] as const) {
      expect(crewmateBubbleClass(pos)).not.toMatch(/rounded-(r|tr|br)-/)
      expect(crewmateBubbleClass(pos)).toMatch(/\bbg-card\b/)
      expect(crewmateBubbleClass(pos)).toMatch(/\bborder-border\b/)
    }
  })
})

/** The frontend half of the shared pin: the backend's `is_speech_row`
 *  (`src/kiro_crew/dashboard/system_notices.py`, tested by
 *  `test/test_members_preview_speech_only.py`) reads the SAME file, so what the
 *  chat draws and what the roster quotes cannot drift apart silently. The chat
 *  draws the crewmate's rows through
 *  `isCrewmateSpeech` (and the user's through `isCrewmateChatRow`, which drops the
 *  legacy user-role sub-agent envelope), so that is the composition pinned here —
 *  the functions the chat really calls, not a second exported spelling. */
const isSpeechRow = (m: ChatMessage): boolean => (m.role === 'user' ? isCrewmateChatRow(m) : isCrewmateSpeech(m))

describe('speech twins agree (shared fixture)', () => {
  const FIXTURE = path.resolve(__dirname, '../../../../test/fixtures/crewmate_speech_rows.json')
  type Case = { name: string; role: string; content: string; meta?: Record<string, unknown>; kind?: string; speech: boolean; frontend_only?: boolean }
  const cases: Case[] = JSON.parse(readFileSync(FIXTURE, 'utf8')).cases
  it('has enough rows to mean something', () => { expect(cases.length).toBeGreaterThanOrEqual(10) })
  for (const c of cases) {
    it(c.name, () => {
      const row: ChatMessage = { role: c.role, content: c.content, cls: '', ts: '2026-09-22T06:00:00Z', meta: c.meta, kind: c.kind }
      expect(isSpeechRow(row)).toBe(c.speech)
    })
  }
})
