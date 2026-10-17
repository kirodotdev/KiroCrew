/**
 * What a card refusal or step failure tells the person, localized by the
 * gateway's code. A code this build does not know falls back to the message the
 * gateway (or the route) sent.
 */
import { i18nT } from '../i18n/t'
import { errorCode } from '../api/cards'

const REFUSALS: Record<string, () => string> = {
  owner_only: () => i18nT('components.changeCards.refusal_owner_only'),
  plan_mismatch: () => i18nT('components.changeCards.refusal_plan_mismatch'),
  changed_since_preview: () => i18nT('components.changeCards.refusal_changed_since_preview'),
  changed_since_apply: () => i18nT('components.changeCards.refusal_changed_since_apply'),
  stale_revision: () => i18nT('components.changeCards.refusal_stale_revision'),
  card_busy: () => i18nT('components.changeCards.refusal_card_busy'),
  step_out_of_order: () => i18nT('components.changeCards.refusal_step_out_of_order'),
  undo_unavailable: () => i18nT('components.changeCards.refusal_undo_unavailable'),
}

export interface CardProblem {
  code: string | null
  message: string
}

export function problemFrom(e: unknown): CardProblem {
  return { code: errorCode(e), message: e instanceof Error ? e.message : String(e) }
}

export function problemText(p: CardProblem): string {
  return (p.code && REFUSALS[p.code]?.()) || p.message
}

/** A refusal that a fresh read of the card resolves. */
export function wantsRefresh(code: string | null): boolean {
  // `changed_since_apply` is not one: a re-read cannot make the undo valid again.
  return code === 'changed_since_preview' || code === 'stale_revision'
}
