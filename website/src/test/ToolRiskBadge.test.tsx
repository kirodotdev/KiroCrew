/**
 * The tool card's risk badge — its reader, its wording, and what it never draws.
 *
 * Three things are pinned here, and each is a way the badge could lie:
 *
 *  - the READER. Every field arrives over the wire, and `safe` is deliberately
 *    not a readable tier: the producer stamps a record only for `caution` and
 *    `risky`, so "a record exists" and "this call was flagged" are one fact. A
 *    reader that accepted `safe` would put a badge on every tool card of a
 *    sampled session and cost the flag its meaning.
 *  - the CLAIM. The call was approved by the session's own permission policy
 *    without consulting Jev, so nothing on the badge may read as a block. That is
 *    why the tooltip states it in words and why `risky` is not drawn in the
 *    danger colour this transcript uses for a blocked call.
 *  - the TOOL ROW. An ordinary row carries no record — the field is absent on
 *    every call the seam did not answer, which is most of them — so the
 *    absent-field path is the common path and the row must render exactly as it
 *    does without one.
 */
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

import { api } from '../api/client'
import { queryClient } from '../api/queryClient'
import { __resetVerdicts } from '../pages/chat/decisionRecord'
import ToolRiskBadge from '../pages/chat/ToolRiskBadge'
import {
  readToolRiskRecord,
  TOOL_RISK_TIERS,
  toolRiskFieldOf,
} from '../pages/chat/toolRiskRecord'
import type { ChatMessage } from '../types'

/** A full record as the gateway sends it: the outcome row the point wrote. */
const WIRE = {
  ts: '2026-09-20T07:00:00Z',
  point: 'tool.risk',
  session: '0123456789ab',
  latency_ms: 420,
  scrubbed: false,
  answers: null,
  error: null,
  turn_id: 'tr-7',
  tool: 'bash',
  tier: 'risky',
  p: 0.88,
  policy: 'trust',
  flagged: true,
}

beforeEach(() => {
  __resetVerdicts()
  queryClient.clear()
})

afterEach(() => {
  cleanup()
  queryClient.clear()
  vi.restoreAllMocks()
})

describe('readToolRiskRecord', () => {
  it('reads a full record and keeps only what the badge prints', () => {
    expect(readToolRiskRecord(WIRE)).toEqual({
      turnId: 'tr-7',
      tool: 'bash',
      tier: 'risky',
      p: 0.88,
    })
  })

  it('reads the milder tier too', () => {
    expect(readToolRiskRecord({ ...WIRE, tier: 'caution' })!.tier).toBe('caution')
  })

  it('draws nothing for a safe verdict', () => {
    // The producer does not stamp one; a reader that accepted it anyway would
    // badge every tool card of a sampled session.
    expect(readToolRiskRecord({ ...WIRE, tier: 'safe' })).toBeNull()
  })

  it('draws nothing for a tier this build does not know', () => {
    for (const tier of ['', 'catastrophic', 'RISKY', 7, null]) {
      expect(readToolRiskRecord({ ...WIRE, tier })).toBeNull()
    }
  })

  it('draws nothing without a turn id, because the thumbs would rate nothing', () => {
    for (const bad of [undefined, null, 'x', 7, [], {}, { tier: 'risky' }, { ...WIRE, turn_id: '' }, { ...WIRE, turn_id: 3 }]) {
      expect(readToolRiskRecord(bad)).toBeNull()
    }
  })

  it('prints no score rather than a number a reader would take at face value', () => {
    for (const p of [1.5, -0.1, Number.NaN, Number.POSITIVE_INFINITY, '0.9', null, undefined]) {
      expect(readToolRiskRecord({ ...WIRE, p })!.p).toBeNull()
    }
    expect(readToolRiskRecord({ ...WIRE, p: 0 })!.p).toBe(0)
    expect(readToolRiskRecord({ ...WIRE, p: 1 })!.p).toBe(1)
  })

  it('tolerates a missing tool name, since the card already names the call', () => {
    expect(readToolRiskRecord({ ...WIRE, tool: undefined })!.tool).toBe('')
  })

  it('names exactly the two tiers the producer flags', () => {
    expect([...TOOL_RISK_TIERS]).toEqual(['caution', 'risky'])
  })
})

describe('toolRiskFieldOf', () => {
  it('reads both doors, and returns the raw field so the memo stays stable', () => {
    expect(toolRiskFieldOf({ decisions_tool_risk: WIRE })).toBe(WIRE)
    expect(toolRiskFieldOf({ meta: { decisions_tool_risk: WIRE } })).toBe(WIRE)
    expect(toolRiskFieldOf({})).toBeUndefined()
    const msg = { meta: { decisions_tool_risk: WIRE } }
    expect(toolRiskFieldOf(msg)).toBe(toolRiskFieldOf(msg))
  })
})

describe('the badge', () => {
  const record = readToolRiskRecord(WIRE)!

  it('names the tier and the score on one line', () => {
    render(<ToolRiskBadge record={record} />)
    const badge = screen.getByTestId('tool-risk-badge')
    expect(badge).toHaveAttribute('data-tier', 'risky')
    expect(badge.textContent).toContain('Jev: risky')
    expect(screen.getByTestId('tool-risk-badge-confidence').textContent).toContain('0.88')
  })

  it('says the flag changed nothing, and never claims the call ran', () => {
    // Both halves matter and the second is the one that was wrong: a later
    // PreToolUse denial can refuse a call that already carries this badge, so a
    // tooltip asserting it executed tells the reader something false about the very
    // case the badge is most likely to appear in.
    render(<ToolRiskBadge record={record} />)
    const title = screen.getByTestId('tool-risk-badge-tier').getAttribute('title') ?? ''
    expect(title).toContain('changed nothing')
    expect(title).toContain('may still have been refused')
    expect(title).not.toContain('still ran')
  })

  it('keeps the danger colour for a blocked call, not for an annotation on one that ran', () => {
    const { container } = render(<ToolRiskBadge record={record} />)
    expect(container.querySelector('.text-danger')).toBeNull()
    expect(container.querySelector('.text-warn')).not.toBeNull()
  })

  it('uses the milder word for the milder tier', () => {
    render(<ToolRiskBadge record={readToolRiskRecord({ ...WIRE, tier: 'caution' })!} />)
    const badge = screen.getByTestId('tool-risk-badge')
    expect(badge).toHaveAttribute('data-tier', 'caution')
    expect(badge.textContent).toContain('worth a look')
    expect(badge.textContent).not.toContain('risky')
  })

  it('prints no score when the answer carried none', () => {
    render(<ToolRiskBadge record={readToolRiskRecord({ ...WIRE, p: null })!} />)
    expect(screen.queryByTestId('tool-risk-badge-confidence')).toBeNull()
  })

  it('posts the verdict as Jev’s own side, and takes it back on a second press', async () => {
    const send = vi.spyOn(api, 'sendDecisionsFeedback').mockResolvedValue({})
    render(<ToolRiskBadge record={record} />)

    fireEvent.click(screen.getByTestId('tool-risk-badge-right-jev'))
    await waitFor(() => expect(screen.getByTestId('tool-risk-badge-right-jev')).toHaveAttribute('aria-pressed', 'true'))
    expect(send).toHaveBeenLastCalledWith('tr-7', 'right', 'jev')

    fireEvent.click(screen.getByTestId('tool-risk-badge-right-jev'))
    await waitFor(() => expect(send).toHaveBeenLastCalledWith('tr-7', null, 'jev'))

    fireEvent.click(screen.getByTestId('tool-risk-badge-wrong-jev'))
    await waitFor(() => expect(send).toHaveBeenLastCalledWith('tr-7', 'wrong', 'jev'))
  })

  it('carries each thumb’s meaning as a tooltip too, since the button renders no text', () => {
    render(<ToolRiskBadge record={record} />)
    expect(screen.getByTestId('tool-risk-badge-right-jev')).toHaveAttribute('title', 'Jev read the risk right')
    expect(screen.getByTestId('tool-risk-badge-wrong-jev')).toHaveAttribute('title', 'Jev read the risk wrong')
  })

  it('names what the thumbs rate, visibly and not only to a screen reader', () => {
    render(<ToolRiskBadge record={record} />)
    expect(screen.getByTestId('tool-risk-badge-rate-label-jev').textContent).toBe('Jev')
  })

  it('surfaces a refused verdict beside the thumbs rather than silently reverting', async () => {
    vi.spyOn(api, 'sendDecisionsFeedback').mockRejectedValue(new Error('nope'))
    render(<ToolRiskBadge record={record} />)

    fireEvent.click(screen.getByTestId('tool-risk-badge-right-jev'))
    await waitFor(() => expect(screen.getByTestId('tool-risk-badge-feedback-error-jev')).toBeInTheDocument())
    expect(screen.getByTestId('tool-risk-badge-right-jev')).toHaveAttribute('aria-pressed', 'false')
  })

  it('addresses its own thumbs apart from the strip’s, so both can sit in one transcript', () => {
    render(<ToolRiskBadge record={record} />)
    expect(screen.queryByTestId('decision-strip-right-jev')).toBeNull()
    expect(screen.getByTestId('tool-risk-badge-right-jev')).toBeInTheDocument()
  })
})

describe('the tool row', () => {
  const row = (over: Partial<ChatMessage> = {}): ChatMessage =>
    ({ role: 'tool', content: '🔧 bash', cls: 'msg msg-tool', ts: '07:00', ...over }) as ChatMessage

  it('reads no record off an ordinary row, which is the shipping state', () => {
    expect(readToolRiskRecord(toolRiskFieldOf(row()))).toBeNull()
    expect(readToolRiskRecord(toolRiskFieldOf(row({ meta: { tool_call_id: 'tc-1' } })))).toBeNull()
  })

  it('reads one off a row the gateway stamped', () => {
    const read = readToolRiskRecord(toolRiskFieldOf(row({ meta: { decisions_tool_risk: WIRE } })))
    expect(read).not.toBeNull()
    expect(read!.tier).toBe('risky')
  })

  it('reads none off a row whose record it cannot validate', () => {
    expect(
      readToolRiskRecord(toolRiskFieldOf(row({ meta: { decisions_tool_risk: { tool: 'bash' } } }))),
    ).toBeNull()
  })
})
