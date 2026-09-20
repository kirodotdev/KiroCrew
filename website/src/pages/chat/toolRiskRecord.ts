/**
 * The read model behind the tool card's risk badge.
 *
 * The gateway stamps one record on a TOOL row when the Decisions (Jev) seam
 * answered `tool.risk` for that call (`decisions/points/tool_risk.py`). The badge
 * renders that record and nothing else: it makes no request of its own to learn
 * what happened, so a transcript reloaded from disk and one that arrived live say
 * the same thing.
 *
 * The badge is an ANNOTATION. A record on a row never means the call was stopped,
 * changed or delayed — the permission decision was made without consulting this
 * seam — so nothing here may read as a verdict about whether the call ran.
 *
 * Every field is validated here rather than at the render site, for the reason
 * `decisionRecord.ts` gives: this payload names what left the machine and what
 * came back, and a badge that prints a shape it did not check would describe a
 * decision nobody made. A record that fails validation renders nothing at all,
 * because a half-drawn receipt is worse than no receipt.
 *
 * `safe` is deliberately NOT a readable tier. The producer stamps a record only
 * for `caution` and `risky`, so "a record exists" and "this call was flagged" are
 * one fact; accepting `safe` here would put a badge on every tool card of a
 * sampled session and cost the flag its meaning.
 */
import type { ChatMessage } from '../../types'

/** The tiers that earn a badge, in increasing severity. Mirrors `tool_risk.FLAGGED_TIERS`. */
export const TOOL_RISK_TIERS = ['caution', 'risky'] as const

export type ToolRiskTier = typeof TOOL_RISK_TIERS[number]

/** One risk annotation, as the badge prints it. */
export interface ToolRiskRecord {
  /** Identifies this annotation; the feedback POST's subject. */
  turnId: string
  /** The tool the oracle was asked about, as the gateway recorded it. */
  tool: string
  /** How risky it said the call was. */
  tier: ToolRiskTier
  /** Jev's own confidence, or `null` when the answer carried none. */
  p: number | null
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null
}

/**
 * The raw field off a tool row, or `undefined`.
 *
 * Returned as it sits on the message so the reference is stable across renders:
 * `ToolCallLine` is memoised, and handing it a freshly built object every render
 * would defeat that for the row carrying the badge.
 *
 * Both doors are read — the row's own top level and `meta` — the same split
 * `decisionStripFieldOf` reads, so a producer that stamps the key at either
 * level is understood rather than silently ignored.
 */
export function toolRiskFieldOf(msg: Pick<ChatMessage, 'meta' | 'decisions_tool_risk'>): unknown {
  return msg.decisions_tool_risk ?? msg.meta?.decisions_tool_risk
}

/**
 * Validate one raw record. `null` means "draw nothing".
 *
 * A `turn_id` is required because it is what a verdict is filed against: a badge
 * whose thumbs would post a verdict about nothing is a control that lies about
 * being one. A tier outside `TOOL_RISK_TIERS` is `null` rather than a
 * best-effort reading — `safe`, an unknown word and a future fourth tier all
 * render nothing, which is the compatible direction for a surface whose whole
 * claim is that a flag means something.
 */
export function readToolRiskRecord(raw: unknown): ToolRiskRecord | null {
  const root = asRecord(raw)
  if (!root) return null
  const turnId = typeof root.turn_id === 'string' ? root.turn_id : ''
  if (!turnId) return null
  const tier = TOOL_RISK_TIERS.find(t => t === root.tier)
  if (!tier) return null
  const tool = typeof root.tool === 'string' ? root.tool : ''
  // A probability outside 0–1 is not a probability; the badge prints no number
  // rather than one the reader would take at face value.
  const rawP = root.p
  const p = typeof rawP === 'number' && Number.isFinite(rawP) && rawP >= 0 && rawP <= 1 ? rawP : null
  return { turnId, tool, tier, p }
}
