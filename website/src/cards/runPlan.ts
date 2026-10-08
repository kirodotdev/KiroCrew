/**
 * Execute a card's apply or undo plan: its steps in order, each against the
 * existing route it names, each tagged with the card headers. Stops at the first
 * refused step — a later step that depends on it (the schedule of a crewmate that
 * was not created) must not run. The outcome the user sees is the gateway's
 * record of these requests, read back afterwards; nothing here decides it.
 */
import { replayedCard, sendCardStep, type Card, type CardOp, type CardPlanStep } from '../api/cards'

/** How often a `repeat` step is resent, and for how long at most. */
export const REPEAT_POLL_MS = 2_000
export const REPEAT_DEADLINE_MS = 5 * 60_000

export type PlanOutcome = { ok: true; card?: Card | null } | { ok: false; step: number; error: unknown }

export interface RunOptions {
  /** What the person typed for each `user` fill field. Never stored here. */
  inputs?: Record<string, string>
  /** Re-read this card from the gateway (it records every step). */
  reload?: () => Promise<Card | null>
  /** Each response of a `repeat` step, e.g. to show a sign-in link. */
  onRepeat?: (response: unknown) => void
  /** Continue an interrupted apply: the first step to send and the earlier steps' responses. */
  resume?: { step: number; responses: unknown[] }
  pollMs?: number
  deadlineMs?: number
  sleep?: (ms: number) => Promise<void>
}

/** The `user` fill fields of a plan: what the card must ask the person for. */
export function userFields(card: Card): string[] {
  const out: string[] = []
  for (const step of [...card.plan.apply, ...(card.plan.undo ?? [])]) {
    for (const f of step.fill ?? []) if (f.source === 'user' && !out.includes(f.field)) out.push(f.field)
  }
  return out
}

/**
 * The body a step is sent with: the planned body with ONLY its declared fill
 * fields replaced — a `user` field from what the person typed, a `step` field
 * from that earlier step's response at `key`. Everything else is sent as planned.
 */
export function fillBody(step: CardPlanStep, inputs: Record<string, string>, responses: unknown[]): unknown {
  if (!step.fill?.length || !step.body || typeof step.body !== 'object') return step.body
  const out = { ...(step.body as Record<string, unknown>) }
  for (const f of step.fill) {
    if (f.source === 'user') out[f.field] = inputs[f.field] ?? ''
    else {
      const res = responses[f.step]
      out[f.field] = res && typeof res === 'object' ? (res as Record<string, unknown>)[f.key] : undefined
    }
  }
  return out
}

const defaultSleep = (ms: number) => new Promise<void>(r => setTimeout(r, ms))

export async function runCardPlan(card: Card, op: CardOp, opts: RunOptions = {}): Promise<PlanOutcome> {
  const steps = op === 'apply' ? card.plan.apply : (card.plan.undo ?? [])
  const inputs = opts.inputs ?? {}
  const sleep = opts.sleep ?? defaultSleep
  const responses: unknown[] = [...(opts.resume?.responses ?? [])]
  for (let i = opts.resume?.step ?? 0; i < steps.length; i++) {
    const step = steps[i]
    const deadline = Date.now() + (opts.deadlineMs ?? REPEAT_DEADLINE_MS)
    for (;;) {
      let response: unknown
      try {
        response = await sendCardStep(card, op, i, step, fillBody(step, inputs, responses))
      } catch (error) {
        return { ok: false, step: i, error }
      }
      const replay = replayedCard(response)
      if (replay) return { ok: true, card: replay }
      responses[i] = response
      if (!step.repeat) break
      opts.onRepeat?.(response)
      const current = opts.reload ? await opts.reload() : null
      // The gateway decides when the poll is over: it stops waiting, or the
      // card leaves `applying` (granted, failed, abandoned).
      if (!current || current.status !== 'applying') return { ok: true, card: current }
      if (!current.progress?.waiting) break
      if (Date.now() >= deadline) return { ok: true, card: current }
      await sleep(opts.pollMs ?? REPEAT_POLL_MS)
    }
  }
  return { ok: true }
}
