import { fmtList, fmtNumber } from '../../../i18n/format'
import { i18nT } from '../../../i18n/t'

/**
 * The crew main card's numbers in the reader's language.
 *
 * The backend sends every derived field twice: as finished English (what any surface
 * without the catalog shows) and as a phrase under {@link CREW_MAIN_PHRASES_FIELD} — a
 * catalog id and the counts it is built from. This module renders the phrases through the
 * catalog. The counts are the fold's own: the browser formats them and picks a plural form
 * with them, and computes nothing from them.
 *
 * Anything it cannot render exactly keeps the backend's English for that field: an
 * unknown id, a missing or extra param, a value of the wrong shape. A field the card does
 * not carry is never added, so a phrase cannot paint a cell the layout did not bind.
 *
 * The English catalog values match the backend's renderer placeholder for placeholder; the
 * shared fixture `test/fixtures/crew_main_phrases.json` is read by both test suites. The two
 * differ only in number formatting: counts here are grouped for the active locale (`1,480`),
 * where the backend's English keeps plain digits (`1480`).
 */

/** The card data key the phrases ride under (`crew_main_contract.PHRASES_FIELD`). */
export const CREW_MAIN_PHRASES_FIELD = 'crew-main-phrases'

/** Each phrase's catalog key, and the exact params the backend sends with it. */
const PHRASES = {
  not_recorded: { key: 'commandCenter.crewMain.not_recorded', params: [] },
  unreadable: { key: 'commandCenter.crewMain.unreadable', params: [] },
  session_open: { key: 'commandCenter.crewMain.session_open', params: [] },
  session_closed: { key: 'commandCenter.crewMain.session_closed', params: [] },
  session_unknown: { key: 'commandCenter.crewMain.session_unknown', params: [] },
  turn_running: { key: 'commandCenter.crewMain.turn_running', params: [] },
  no_turn: { key: 'commandCenter.crewMain.no_turn', params: [] },
  stop_end_turn: { key: 'commandCenter.crewMain.stop_end_turn', params: [] },
  stop_max_tokens: { key: 'commandCenter.crewMain.stop_max_tokens', params: [] },
  stop_max_turn_requests: { key: 'commandCenter.crewMain.stop_max_turn_requests', params: [] },
  stop_refusal: { key: 'commandCenter.crewMain.stop_refusal', params: [] },
  stop_cancelled: { key: 'commandCenter.crewMain.stop_cancelled', params: [] },
  stop_failed: { key: 'commandCenter.crewMain.stop_failed', params: [] },
  stop_timeout: { key: 'commandCenter.crewMain.stop_timeout', params: [] },
  stop_interrupted: { key: 'commandCenter.crewMain.stop_interrupted', params: [] },
  stop_other: { key: 'commandCenter.crewMain.stop_other', params: [] },
  turns: { key: 'commandCenter.crewMain.turns', params: ['n'], plural: true },
  turns_refused: { key: 'commandCenter.crewMain.turns_refused', params: ['n', 'refused'], plural: true },
  entries: { key: 'commandCenter.crewMain.entries', params: ['n'], plural: true },
  no_items: { key: 'commandCenter.crewMain.no_items', params: [] },
  items_open: { key: 'commandCenter.crewMain.items_open', params: ['open', 'total'] },
  items_progress: { key: 'commandCenter.crewMain.items_progress', params: ['k', 'open'] },
  items_progress_none: { key: 'commandCenter.crewMain.items_progress_none', params: [] },
  items_blocked: { key: 'commandCenter.crewMain.items_blocked', params: ['k', 'open'] },
  items_blocked_none: { key: 'commandCenter.crewMain.items_blocked_none', params: [] },
  items_accepted: { key: 'commandCenter.crewMain.items_accepted', params: ['k', 'total'] },
  items_question: { key: 'commandCenter.crewMain.items_question', params: ['k', 'open'] },
  items_question_none: { key: 'commandCenter.crewMain.items_question_none', params: [] },
  omitted_none: { key: 'commandCenter.crewMain.omitted_none', params: [] },
  omitted: { key: 'commandCenter.crewMain.omitted', params: ['n'], plural: true },
  credits: { key: 'commandCenter.crewMain.credits', params: ['amount'] },
  credits_tiny: { key: 'commandCenter.crewMain.credits_tiny', params: ['amount'] },
  subagent_none: { key: 'commandCenter.crewMain.subagent_none', params: [] },
  subagent: { key: 'commandCenter.crewMain.subagent', params: ['amount', 'n'], plural: true },
  subagent_tiny: { key: 'commandCenter.crewMain.subagent_tiny', params: ['amount', 'n'], plural: true },
  tokens_none: { key: 'commandCenter.crewMain.tokens_none', params: [] },
  tokens: { key: 'commandCenter.crewMain.tokens', params: ['n'], plural: true },
  approvals_open: { key: 'commandCenter.crewMain.approvals_open', params: ['n'], plural: true },
  approvals_open_of: { key: 'commandCenter.crewMain.approvals_open_of', params: ['k', 'total'] },
  approvals_decided: { key: 'commandCenter.crewMain.approvals_decided', params: ['n'] },
  approvals_decided_by: { key: 'commandCenter.crewMain.approvals_decided_by', params: ['n', 'rows'] },
} as const satisfies Record<string, { key: string; params: readonly string[]; plural?: true }>

type PhraseId = keyof typeof PHRASES

/** The approval decisions the catalog names. Any other name keeps the field English. */
const DECISIONS = {
  approved: 'commandCenter.crewMain.decision_approved',
  rejected: 'commandCenter.crewMain.decision_rejected',
  rejected_once: 'commandCenter.crewMain.decision_rejected_once',
  unknown: 'commandCenter.crewMain.decision_unknown',
} as const

/** A credit charge exactly as the backend rounds it: whole units and two places. */
const AMOUNT = /^\d{1,20}\.\d{2}$/

function isCount(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

/** The approvals breakdown, `5 approved, 1 rejected`, or null when a row cannot be named. */
function breakdown(rows: unknown): string | null {
  if (!Array.isArray(rows) || rows.length === 0) return null
  const parts: string[] = []
  for (const row of rows) {
    if (!Array.isArray(row) || row.length !== 2) return null
    const [name, count] = row as unknown[]
    if (typeof name !== 'string' || !Object.hasOwn(DECISIONS, name) || !isCount(count)) return null
    parts.push(i18nT(DECISIONS[name as keyof typeof DECISIONS], { n: fmtNumber(count) }))
  }
  // Narrow conjunction: comma-joined in English like the backend, and each locale's own
  // enumeration mark elsewhere (、 in Chinese and Japanese).
  return fmtList(parts, { type: 'conjunction', style: 'narrow' })
}

/** One phrase as text in the active language, or null when it cannot be rendered exactly. */
export function renderCrewMainPhrase(phrase: unknown): string | null {
  if (!Array.isArray(phrase) || phrase.length < 1 || phrase.length > 2) return null
  const [id, rawParams = {}] = phrase as unknown[]
  if (typeof id !== 'string' || !Object.hasOwn(PHRASES, id) || !isPlainObject(rawParams)) return null
  const spec: { key: string; params: readonly string[]; plural?: true } = PHRASES[id as PhraseId]
  const names = Object.keys(rawParams)
  if (names.length !== spec.params.length || !spec.params.every(name => Object.hasOwn(rawParams, name))) return null
  const vars: Record<string, string | number> = {}
  for (const name of spec.params) {
    const value = rawParams[name]
    if (name === 'amount') {
      if (typeof value !== 'string' || !AMOUNT.test(value)) return null
      vars.amount = fmtNumber(Number(value), { minimumFractionDigits: 2, maximumFractionDigits: 2 })
    } else if (name === 'rows') {
      const text = breakdown(value)
      if (text === null) return null
      vars.breakdown = text
    } else {
      if (!isCount(value)) return null
      vars[name] = fmtNumber(value)
    }
  }
  // The plural form is picked by the raw count; the text shows it formatted, as `n`.
  if (spec.plural) vars.count = rawParams.n as number
  return i18nT(spec.key, vars)
}

/**
 * *data* with every derived field the backend phrased rendered in the active language,
 * and the phrases key itself dropped. A card with no phrases is returned unchanged.
 */
export function localizeCrewMainData(data: Record<string, string>): Record<string, string> {
  if (!Object.hasOwn(data, CREW_MAIN_PHRASES_FIELD)) return data
  const out: Record<string, string> = {}
  for (const [field, value] of Object.entries(data)) {
    if (field !== CREW_MAIN_PHRASES_FIELD) out[field] = value
  }
  let phrases: unknown
  try {
    phrases = JSON.parse(data[CREW_MAIN_PHRASES_FIELD])
  } catch {
    return out
  }
  if (!isPlainObject(phrases)) return out
  for (const [field, phrase] of Object.entries(phrases)) {
    if (!Object.hasOwn(out, field)) continue
    const text = renderCrewMainPhrase(phrase)
    if (text !== null) out[field] = text
  }
  return out
}
