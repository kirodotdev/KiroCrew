import { readFileSync } from 'node:fs'
import path from 'node:path'
import { describe, expect, it } from 'vitest'
import type { ChatMessage } from '../types'
import { isHiddenReplyAt } from '../app-sdk/turnPolicyBlock'

/** The frontend half of the hidden-reply pin: the backend's `is_hidden_reply_at`
 *  (`src/kiro_crew/dashboard/hidden_reply.py`, tested by
 *  `test/test_hidden_reply_twin.py`) reads the SAME file. Regenerate's server scan
 *  asks the backend twin, so a row the two judge differently is a Regenerate that
 *  targets a row other than the one the button sits on. */
describe('hidden-reply twins agree (shared fixture)', () => {
  const FIXTURE = path.resolve(__dirname, '../../../test/fixtures/hidden_reply_rows.json')
  type Row = { role: string; content: string; meta?: Record<string, unknown>; variants?: { content: string }[] }
  type Case = { name: string; rows: Row[]; hidden: boolean[] }
  const cases: Case[] = JSON.parse(readFileSync(FIXTURE, 'utf8')).cases
  it('has enough cases to mean something', () => {
    expect(cases.length).toBeGreaterThanOrEqual(21)
    expect(new Set(cases.map(c => c.hidden[c.hidden.length - 1]))).toEqual(new Set([true, false]))
  })
  for (const c of cases) {
    it(c.name, () => {
      const rows: ChatMessage[] = c.rows.map(r => ({ ...r, cls: '', ts: '2026-11-21T06:00:00Z' }) as ChatMessage)
      expect(rows.map((m, i) => isHiddenReplyAt(m, rows, i))).toEqual(c.hidden)
    })
  }
})
