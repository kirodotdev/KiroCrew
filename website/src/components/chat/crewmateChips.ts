/**
 * crewmateChips — which suggested replies a crewmate's chat offers.
 *
 * A suggested reply (an `[OPTIONS:]` chip) is the user's next message, so a
 * chip the crewmate cannot honour is a broken promise the user clicks into.
 * The one shape that keeps doing it is a chip asking the crewmate to raise a
 * card ("Ask for a card to let you install it", bug-bash H4): when a card fits
 * the crewmate raises it itself, because the card IS the question to the user;
 * when none fits, nothing can answer the chip. And "card" is our word, not the
 * user's. So in a crewmate chat such a chip is never offered.
 *
 * The prompt says the same (`conductor.how_you_work` in
 * `crewmate_prompts.json`); this filter is the backstop for a model that
 * writes one anyway. Display only: the transcript keeps the reply as written.
 */
import type { FollowUpDerivation } from '../../app-sdk/protocol'

/** A chip that asks the crewmate to RAISE one of our cards, matched narrowly:
 *  one of our own card names after an article (a change, approval, permission
 *  or capability card), the `propose_change` tool, or the request shape the H4 chip
 *  used, "ask (me) for a card" / "raise a card". A generic verb with a card
 *  noun ("Give me the card number", "Make the cards bigger", "Muestra la
 *  tarjeta de crédito", "クレジットカードを表示して") is the user's own task
 *  and is kept. Missing a card-asking chip phrased some other way costs a
 *  chip the mate cannot honour; hiding a real task costs the user's next
 *  step, so the matcher errs toward keeping. */
const CARD_ASK_RES: readonly RegExp[] = [
  // The article keeps the verb "change" out: "Change card colors" is a task.
  /\b(?:a|an|the|this|that|another)\s+(?:change|approval|capability|permission)\s+cards?\b/i,
  /\b(?:ask(?:\s+me)?\s+for|raise)\s+(?:a|an|another|one)\s+(?:new\s+)?card\b/i,
  /\bpropose_change\b/i,
  /(?:变更|审批|权限|能力)卡片/,
  /(?:変更|承認|許可)カード/,
  /(?:변경|승인|권한)\s?카드/,
  // The `u` flag and `\p{L}` because `\w` and `\b` are ASCII-only: without
  // them a Cyrillic ending or a word opening with `Ä` never matches.
  /карточк\p{L}*\s+(?:изменени|одобрени|разрешени)/iu,
  /(?<!\p{L})cartes?\s+(?:de\s+)?(?:modification|validation|autorisation)(?!\p{L})/iu,
  /(?<!\p{L})(?:änderungs|freigabe|berechtigungs)karten?(?!\p{L})/iu,
  /(?<!\p{L})tarjetas?\s+de\s+(?:cambio|aprobación|permiso)(?!\p{L})/iu,
  /(?<!\p{L})cart(?:ão|ões)\s+de\s+(?:alteração|aprovação|permissão)(?!\p{L})/iu,
  /(?<!\p{L})sched[ae]\s+di\s+(?:modifica|approvazione|autorizzazione)(?!\p{L})/iu,
]

/** True when *label* asks the crewmate to raise a card. */
export function asksForCard(label: string): boolean {
  return CARD_ASK_RES.some(re => re.test(label))
}

const NONE: FollowUpDerivation = Object.freeze({
  followUpOptions: Object.freeze([]) as readonly string[],
  followUpSourceKey: null,
  followUpMulti: true,
})

/**
 * The derivation a crewmate chat offers: *d* without its card-asking chips.
 * Returns *d* itself when nothing is dropped, so a memoized consumer keeps its
 * identity, and the shared empty offer when every chip is dropped.
 */
export function crewmateFollowUps(d: FollowUpDerivation): FollowUpDerivation {
  const kept = d.followUpOptions.filter(label => !asksForCard(label))
  if (kept.length === d.followUpOptions.length) return d
  if (!kept.length) return NONE
  return { ...d, followUpOptions: kept }
}
