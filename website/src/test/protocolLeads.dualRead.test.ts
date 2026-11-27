/**
 * Every SPA reader of a protocol lead the gateway writes reads BOTH spellings:
 * the plain word current gateways write and the glyph saved history keeps.
 * One `it.each` per parser, old form and new form side by side.
 */
import { describe, it, expect } from 'vitest'
import type { ChatMessage } from '../types'
import {
  toolRowKind,
  isToolCallRow,
  isToolRefusedRow,
  stripToolRowLead,
  stripToolCallLead,
} from '../lib/toolRowContract'
import { isBrowserToolMessage } from '../pages/chat/BrowserLiveCard'
import { deriveLoadedMcpTools } from '../lib/mcpLoadedTools'
import { parseNotice } from '../pages/chat/NoticeCard'
import { parseCompactionNotice } from '../pages/chat/CompactionCard'
import { resolveGatewayNotice, stripNoticeLead } from '../pages/chat/gatewayNotice'
import { parseSubagentCompletion } from '../pages/chat/subagentCompletion'
import { approvalToolTitle } from '../pages/ChannelPage'

const tool = (content: string, meta?: Record<string, unknown>): ChatMessage =>
  ({ role: 'tool', content, ts: '2026-11-17T00:00:00Z', ...(meta ? { meta } : {}) }) as unknown as ChatMessage

describe('toolRowContract reads both leads', () => {
  it.each([
    ['\u{1F527} bash', 'call', 'bash'],
    ['Tool: bash', 'call', 'bash'],
    ['\u2705 bash', 'approved', 'bash'],
    ['Approved: bash', 'approved', 'bash'],
    ['\u{1F6AB} bash — Blocked by security policy: rm', 'refused', 'bash — Blocked by security policy: rm'],
    ['Refused: bash — Blocked by security policy: rm', 'refused', 'bash — Blocked by security policy: rm'],
  ])('%s', (content, kind, body) => {
    expect(toolRowKind(content)).toBe(kind)
    expect(stripToolRowLead(content)).toBe(body)
    expect(isToolCallRow(content)).toBe(kind === 'call')
    expect(isToolRefusedRow(content)).toBe(kind === 'refused')
  })

  it('leaves unled text alone and strips only the call lead in stripToolCallLead', () => {
    expect(toolRowKind('bash')).toBeNull()
    expect(stripToolRowLead('Tooling: x')).toBe('Tooling: x')
    expect(stripToolCallLead('Approved: bash')).toBe('Approved: bash')
    expect(stripToolCallLead('Tool: bash')).toBe('bash')
  })
})

describe('isBrowserToolMessage reads both leads', () => {
  it.each(['\u{1F527} @kirocrew-core/browser', 'Tool: @kirocrew-core/browser'])('%s', content => {
    expect(isBrowserToolMessage(tool(content))).toBe(true)
  })
  it('refuses a refused row', () => {
    expect(isBrowserToolMessage(tool('Refused: @kirocrew-core/browser'))).toBe(false)
  })
})

describe('deriveLoadedMcpTools reads both leads', () => {
  it.each(['\u{1F527} @kirocrew-core/browser', 'Tool: @kirocrew-core/browser'])('%s', content => {
    expect(deriveLoadedMcpTools([tool(content, { mcp_server: 'kirocrew-core' })])).toEqual(
      new Set(['kirocrew-core::browser']),
    )
  })
})

describe('parseNotice reads both leads', () => {
  it.each([
    ['\u2139 Saved.', 'info'],
    ['Note: Saved.', 'info'],
    ['\u26A0\uFE0F Saved.', 'warn'],
    ['Warning: Saved.', 'warn'],
    ['\u26D4 Saved.', 'blocked'],
    ['Blocked: Saved.', 'blocked'],
  ])('%s', (content, tone) => {
    expect(parseNotice(content)).toEqual({ tone, text: 'Saved.' })
  })
})

describe('parseCompactionNotice reads both leads', () => {
  it.each([
    ['\u2705 Conversation compacted: the summary', 'completed', 'the summary'],
    ['Conversation compacted: the summary', 'completed', 'the summary'],
    ['\u2705 Conversation compacted.', 'completed', ''],
    ['Conversation compacted.', 'completed', ''],
  ])('%s', (content, status, summary) => {
    const parsed = parseCompactionNotice(content)
    expect(parsed.status).toBe(status)
    expect(parsed.summary).toBe(summary)
  })

  it.each([
    '\u274C Compaction failed: boom',
    'Error: Compaction failed: boom',
    '\u26A0 Compaction timed out.',
    'Warning: Compaction timed out.',
  ])('%s is a failure', content => {
    expect(parseCompactionNotice(content).status).toBe('failed')
  })
})

describe('gateway notice table reads both leads', () => {
  it.each([
    ['\u2139\uFE0F Auto-continue cancelled — your message takes over.', 'Auto-continue cancelled — your message takes over.'],
    ['Note: Auto-continue cancelled — your message takes over.', 'Auto-continue cancelled — your message takes over.'],
    ["\u26A0 Your account cannot use model 'a' — running on 'b' instead.", "Your account cannot use model 'a' — running on 'b' instead."],
    ["Warning: Your account cannot use model 'a' — running on 'b' instead.", "Your account cannot use model 'a' — running on 'b' instead."],
  ])('%s', (content, stripped) => {
    expect(stripNoticeLead(content)).toBe(stripped)
    const m = { role: 'notice', content, ts: '2026-11-17T00:00:00Z' } as unknown as ChatMessage
    expect(resolveGatewayNotice(m, [m], 0)).not.toBeNull()
  })
})

describe('parseSubagentCompletion reads both outcome forms', () => {
  const single = (header: string) =>
    `[Subagent completion event]\nAgent \`ab12\` ${header}\nTask: look\n\nUsage: 1.00 credits · 5s\n\nthe answer`

  it.each([
    ['completed \u2705', 'ok', ''],
    ['completed', 'ok', ''],
    ['failed \u274C', 'failed', ''],
    ['failed', 'failed', ''],
    ['stopped by user \u23F9', 'stopped', ''],
    ['stopped by user', 'stopped', ''],
    ['\u26A0\uFE0F cut off mid-turn by gateway restart', 'interrupted', 'cut off mid-turn by gateway restart'],
    ['interrupted: cut off mid-turn by gateway restart', 'interrupted', 'cut off mid-turn by gateway restart'],
    ['\u274C lost to gateway restart', 'failed', 'lost to gateway restart'],
    ['failed: lost to gateway restart', 'failed', 'lost to gateway restart'],
  ])('%s', (header, outcome, note) => {
    const parsed = parseSubagentCompletion(single(header))
    expect(parsed?.kind).toBe('single')
    if (parsed?.kind !== 'single') return
    expect(parsed.outcome).toBe(outcome)
    expect(parsed.agentId).toBe('ab12')
    expect(parsed.body.startsWith(note)).toBe(true)
  })

  it('refuses a header that names no outcome', () => {
    expect(parseSubagentCompletion(single('hello'))).toBeNull()
  })

  it.each([
    '2 \u2705 · 1 \u274C · 0 \u23F9 of 3 agents.',
    '2 completed · 1 failed · 0 stopped of 3 agents.',
  ])('wave tallies: %s', tally => {
    const parsed = parseSubagentCompletion(
      `[Subagent batch completion event]\nBatch results 1/1 — wave finished: ${tally} All results delivered.\n\n— \`ab12\` completed · look`,
    )
    expect(parsed?.kind).toBe('batch')
    if (parsed?.kind !== 'batch') return
    expect([parsed.ok, parsed.failed, parsed.stopped, parsed.total]).toEqual([2, 1, 0, 3])
  })
})

describe('approvalToolTitle reads both leads', () => {
  it.each([
    '\u26A0 Approval needed: **bash**\n```\nls\n```',
    'Approval needed: **bash**\n```\nls\n```',
  ])('%s', content => {
    expect(approvalToolTitle(content)).toBe('bash')
  })
})
