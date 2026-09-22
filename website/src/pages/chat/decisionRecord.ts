/**
 * The read model behind the transcript's decision strip.
 *
 * The gateway stamps one record on the row a decision shaped. The decisions of a
 * turn ride the ASSISTANT row that ends it: the skill set chosen by asking Jev
 * (`decisions/points/skills_select.py`) and the model tier it routed the turn to
 * (`decisions/points/model_route.py`). A message whose mid-turn handling was
 * chosen (`decisions/points/message_steer.py`) carries its record on that
 * message's own USER row, because that is what the decision was about, and a
 * compaction whose tool calls were scored in the shadow
 * (`decisions/points/compaction_keep.py`) carries its own on the compaction notice
 * row, for the same reason. The last two are read DIRECTLY by the surface that
 * draws them rather than dispatched out of an assistant row's list, because
 * neither rides an assistant row at all. Every one
 * renders from the record and nothing else: none makes a request of its own to
 * learn what happened, so a transcript reloaded from disk and one that arrived
 * live say the same thing.
 *
 * The field carries a LIST, because two points can decide one reply. A bare
 * object is still accepted and read as a list of one: rows written before the
 * second point existed carry that shape, and they are history this reader must
 * keep rendering.
 *
 * Which record a payload is comes from its own `point`, never from its position
 * in the list or from which fields happen to be present. A record whose point is
 * unknown to this reader draws nothing rather than being guessed at as the older
 * shape -- a model row rendered as a skill row would print two empty skill lists
 * and a check mark saying the sides agreed.
 *
 * Every field is validated here rather than at the render site, for the reason
 * `decisionsPreview.ts` gives about consent: this payload names what left the
 * machine and what came back, and a strip that prints a shape it did not check
 * would describe a decision nobody made. A record that fails validation renders
 * nothing at all — the absent-field path — because a half-drawn receipt is worse
 * than no receipt.
 *
 * The gateway stamps the record under `meta` on the assistant row, and `meta`
 * is what both doors carry: the live websocket frame and a row reloaded from
 * history. The row's own top level is read too, the same split
 * `CompactionCard.noticeKindOf` reads `kind` through, so a producer that stamps
 * the key there is understood rather than silently ignored.
 *
 * The stem is `decisionRecord`, not `decisionStrip`, so it cannot differ from
 * `DecisionStrip.tsx` beside it in case alone. A case-only pair resolves to ONE
 * module on a case-insensitive filesystem, so every import of both breaks on
 * macOS and Windows while a Linux checkout compiles — see the guard in
 * `test/fileNameCasing.test.ts`.
 */
import type { DecisionFeedbackSide, DecisionVerdictValue } from '../../api/client'
import type { ChatMessage } from '../../types'
import {
  DECISIONS_COMPACTION_POINT,
  DECISIONS_LIVE_POINT,
  DECISIONS_MEMORY_POINT,
  DECISIONS_MODEL_POINT,
  DECISIONS_STEER_POINT,
} from '../settings/decisionsPreview'

/** Most records one reply may carry, mirroring the gateway's own per-session cap.
 *
 *  Two points stamp an assistant row, so this bounds a producer that stamped
 *  something unexpected rather than anything real: a row is rendered, so an
 *  unbounded list would be an unbounded number of mounted components in one
 *  transcript row.
 */
const MAX_RECORDS = 8

/** A skill the answer named that the gate then refused, with its own score. */
export interface DecisionStripDropped {
  key: string
  p: number
}

/**
 * One decision, as the strip prints it.
 *
 * `agree` is not read straight from the wire: see `readDecisionStrip`. The two
 * name lists are the whole claim the strip makes, so the flag that decides
 * whether to print one list or two is derived from them.
 */
export interface DecisionStripRecord {
  /** Identifies the turn this decision belongs to; the feedback POST's subject. */
  turnId: string
  /** The decision point, e.g. `skills.select`. */
  point: string
  /** What the shipped word-matching rule would have loaded. */
  baseline: string[]
  /** What Jev answered. */
  jev: string[]
  /** The two lists hold the same skills, in any order. */
  agree: boolean
  /** Jev's own confidence, or `null` when the answer carried none. */
  p: number | null
  /** Prompt tokens the narrower set saved, `0` when it saved none. */
  tokensSaved: number
  /** Skills the gate offered Jev to choose from. */
  candidates: number
  /**
   * Characters of the message excerpt the question sent, or `null` when the
   * record does not state it.
   *
   * Nullable, unlike every other count here, because 0 is not a credible
   * measurement of this one: a turn that reached the selector had text. So a
   * missing field read as 0 would print "message 0 chars" over a question that
   * did send an excerpt -- the same always-zero row this strip exists to remove,
   * reintroduced for every record stamped before the field existed.
   */
  messageChars: number | null
  /** Characters of conversation history the question carried. */
  historyChars: number
  /** Milliseconds between asking Jev and its answer. */
  latencyMs: number
  /** Answers the gate refused, each with the score it came with. */
  dropped: DecisionStripDropped[]
  /** Why the decision failed, or `null` when it did not. */
  error: string | null
}

/**
 * One model-routing decision, as the strip prints it.
 *
 * `point` is `model.route` and is what tells this record from the skill one, so
 * it is kept rather than derived: the discriminator has to survive into the
 * rendered value, or the renderer would have to re-sniff fields.
 */
export interface DecisionModelRecord {
  /** Identifies the turn this decision belongs to; the feedback POST's subject. */
  turnId: string
  /** Always `model.route`. */
  point: typeof DECISIONS_MODEL_POINT
  /** The difficulty tier Jev answered: `simple`, `medium` or `complex`. */
  tier: string
  /** The model the tier routed this turn to, or `''` when that tier is unpinned
   *  and the turn kept its session's model. */
  modelChosen: string
  /** The model the turn would have run on, or `''` when the backend chose. */
  baselineModel: string
  /** Jev's own confidence, or `null` when the answer carried none. */
  p: number | null
  /** How long the decision took, in whole milliseconds. */
  latencyMs: number
  /** Characters of conversation history the question carried. */
  historyChars: number
  /** Prior turns clipped to fit the question inside its budget. */
  truncated: number
  /** Whether the switch took. `true` for a row that predates the field: only an
   *  observed failure writes `false`, so an older row must not read as one. */
  applied: boolean
  /** The model the turn RAN on, when that was observed. `''` otherwise. */
  modelUsed: string
  /** Why the decision failed, or `null` when it did not. */
  error: string | null
}

/** Either decision, as the strip receives it. */
export type DecisionRecord = DecisionStripRecord | DecisionModelRecord

/** Whether *record* is a model-routing decision rather than a skill selection. */
export function isModelRecord(record: DecisionRecord): record is DecisionModelRecord {
  return record.point === DECISIONS_MODEL_POINT
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null
}

/** A whole, non-negative, finite number, or `0` for anything else. */
function asCount(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0
    ? Math.floor(value)
    : 0
}

/**
 * The same count, but `null` rather than `0` for a value that is not one.
 *
 * For a field whose 0 a reader would take as a measurement. Folding "the record
 * did not say" into "the record said zero" is the one shape a receipt must not
 * take: the absent case is every record a producer stamped before the field
 * existed, and it is silent rather than rare.
 */
function asCountOrNull(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0
    ? Math.floor(value)
    : null
}

/**
 * Skill keys off the wire, or `null` when the value is not a list of them.
 *
 * `null`, not a filtered list. Dropping unreadable entries looks forgiving and is
 * the opposite: two lists that were each unreadable in a DIFFERENT way both
 * filter down to `[]`, and `[]` equals `[]`, so the strip would print "same pick"
 * over a record whose two sides it never actually read. That is the one claim
 * this surface must not invent, and the module's own rule already says so --
 * a record that fails validation renders nothing.
 *
 * An EMPTY array is valid and returns `[]`: "no skill applies" is a real answer
 * (`/no skill applies` in the gate), and it is the answer the whole no-skill path
 * produces.
 */
function asNames(value: unknown): string[] | null {
  if (!Array.isArray(value)) return null
  if (!value.every((n): n is string => typeof n === 'string' && n.length > 0)) return null
  return value
}

/** Same members, ignoring order — what "the two sides agreed" means. */
function sameSet(a: string[], b: string[]): boolean {
  const left = new Set(a)
  const right = new Set(b)
  if (left.size !== right.size) return false
  for (const name of right) if (!left.has(name)) return false
  return true
}

/**
 * The raw field off an assistant row, or `undefined`.
 *
 * Returned as it sits on the message so the reference is stable across renders:
 * `AssistantMessage` is memoised, and handing it a freshly built object every
 * render would defeat that for the row carrying the strip.
 */
export function decisionStripFieldOf(msg: Pick<ChatMessage, 'meta' | 'decisions_strip'>): unknown {
  return msg.decisions_strip ?? msg.meta?.decisions_strip
}

/**
 * Validate one raw record. `null` means "draw nothing".
 *
 * `agree` is recomputed from the two name lists instead of being read from the
 * wire. The strip prints ONE list when the sides agreed and BOTH when they did
 * not, so a flag that disagreed with the lists beside it would hide a real
 * divergence behind a check mark. Recomputing makes that impossible.
 */
export function readDecisionStrip(raw: unknown): DecisionStripRecord | null {
  const root = asRecord(raw)
  if (!root) return null
  const turnId = typeof root.turn_id === 'string' ? root.turn_id : ''
  if (!turnId) return null
  const point = typeof root.point === 'string' && root.point ? root.point : DECISIONS_LIVE_POINT
  // Both lists must be readable before anything is drawn: the strip's whole
  // claim is who picked what, and it cannot make that claim about a list it
  // could not read.
  const baseline = asNames(root.baseline)
  const jev = asNames(root.jev)
  if (baseline === null || jev === null) return null
  // A probability outside 0–1 is not a probability; the strip prints no number
  // rather than one the reader would take at face value.
  const rawP = root.p
  const p = typeof rawP === 'number' && Number.isFinite(rawP) && rawP >= 0 && rawP <= 1 ? rawP : null
  const dropped = Array.isArray(root.dropped)
    ? root.dropped.flatMap((entry): DecisionStripDropped[] => {
      const d = asRecord(entry)
      const key = d && typeof d.key === 'string' ? d.key : ''
      if (!key) return []
      const score = typeof d?.p === 'number' && Number.isFinite(d.p) ? d.p : 0
      return [{ key, p: score }]
    })
    : []
  const error = typeof root.error === 'string' && root.error.trim() ? root.error : null
  return {
    turnId,
    point,
    baseline,
    jev,
    agree: sameSet(baseline, jev),
    p,
    tokensSaved: asCount(root.tokens_saved),
    candidates: asCount(root.candidates),
    messageChars: asCountOrNull(root.message_chars),
    historyChars: asCount(root.history_chars),
    latencyMs: asCount(root.latency_ms),
    dropped,
    error,
  }
}

/**
 * One mid-turn handling decision, as the line on the user row prints it.
 *
 * `point` is `message.steer` and is what tells this record from the skill one, so
 * it is kept rather than derived: the discriminator has to survive into the
 * rendered value, or the renderer would have to re-sniff fields.
 */
export interface SteerDecisionRecord {
  /** Identifies the decision this line is about; the feedback POST's subject. */
  turnId: string
  /** Always `message.steer`. */
  point: typeof DECISIONS_STEER_POINT
  /** Which path Jev chose: `steer` (interrupt the running turn) or `queue`. */
  choice: 'steer' | 'queue'
  /** Jev's own confidence, or `null` when the answer carried none. */
  p: number | null
  /**
   * How long the decision took, in whole milliseconds.
   *
   * There is no `error` field, and its absence is the producer's contract rather
   * than an omission here: a `message.steer` decision that FAILED takes the
   * shipped steer path and stamps no record at all, so a receipt exists only for
   * a decision that was actually made.
   */
  latencyMs: number
}

/** The two paths a `message.steer` record may name. Any other value is not one. */
const STEER_CHOICES = ['steer', 'queue'] as const

/**
 * Validate one raw mid-turn handling record. `null` means "draw nothing".
 *
 * `choice` is required and is held against the two paths the gateway offers,
 * unlike the model point's open tier list: this value names one of exactly two
 * shipped code paths, so a third would describe a branch that does not exist
 * rather than one this reader has not learned about yet.
 *
 * The record's `baseline` is deliberately NOT read: it is `steer` on every row --
 * the one arm a refusal keeps -- so a line printing it would print the same word
 * forever, which is the furniture this file's own rule excludes. It stays on the
 * logged row, where a `jq` fold over the day-files uses it.
 */
export function readSteerRecord(raw: unknown): SteerDecisionRecord | null {
  const root = asRecord(raw)
  if (!root) return null
  if (typeof root.point === 'string' && root.point && root.point !== DECISIONS_STEER_POINT) return null
  const turnId = typeof root.turn_id === 'string' ? root.turn_id : ''
  if (!turnId) return null
  const raw_choice = typeof root.choice === 'string' ? root.choice.trim() : ''
  const choice = STEER_CHOICES.find(name => name === raw_choice)
  if (!choice) return null
  const rawP = root.p
  const p = typeof rawP === 'number' && Number.isFinite(rawP) && rawP >= 0 && rawP <= 1 ? rawP : null
  return {
    turnId,
    point: DECISIONS_STEER_POINT,
    choice,
    p,
    latencyMs: asCount(root.latency_ms),
  }
}

/**
 * One compaction scored in the shadow, as the line on the compaction card prints it.
 *
 * `point` is `compaction.keep` and is what tells this record from the other two, so it
 * is kept rather than derived — the same reason the steer record keeps its own.
 */
export interface CompactionKeepRecord {
  /** Identifies the decision this line is about; the feedback POST's subject. */
  turnId: string
  /** Always `compaction.keep`. */
  point: typeof DECISIONS_COMPACTION_POINT
  /** Tool calls the transcript held when the compaction fired. */
  totalCalls: number
  /** How many of them Jev would have kept, whole or call-only, pinned ones included. */
  keptCalls: number
  /**
   * Tool calls the gateway's walk found past its cap and did not score, or `0`.
   *
   * A COUNT rather than a flag, because the line states it: "23 of 61 (+140 not
   * scored)" is true about a capped session where a bare "23 of 61" is not — 61 is
   * the cap, not the session's call count. `0` draws nothing extra, which is every
   * ordinary session.
   */
  truncatedCalls: number
  /**
   * An UPPER BOUND on the characters Jev's keep-set would carry, as a fraction of
   * everything the transcript held, or `null` when the record does not state both
   * sides.
   *
   * A bound rather than the figure, and the copy says so ("up to"): the gateway's
   * numerator counts every conversation row the replay admits BY ROLE, while
   * `build_session_replay` additionally applies row quotas and a character budget, so
   * a long session is replayed with less than this. The gateway names its own field
   * `chars_jev_eligible` for the same reason.
   *
   * Nullable rather than `0`, the rule `messageChars` states one interface up: a record
   * stamped without one of the two numbers would otherwise print "0% of characters"
   * over a keep-set that carries plenty.
   *
   * `null` ALSO when the fraction exceeds 1. The gateway keeps both arms on one
   * character universe, so a share above 100% means the two were measured against
   * different things — and the honest reading of a ratio whose denominator does not
   * contain its numerator is "this record cannot say", not a clamp to 100%. Clamping
   * would print a plausible number over a broken measurement, which is the one shape
   * a receipt must not take.
   */
  charsShare: number | null
}

/**
 * Validate one raw compaction record. `null` means "draw nothing".
 *
 * `keptCalls` is DERIVED from the three tallies rather than read as a field, the same
 * rule `readDecisionStrip` applies to `agree`: the line prints "N of M", and a count
 * that disagreed with the tallies beside it would be a number nobody could reconcile
 * with the log. A record whose parts do not add up to `total_calls` is refused, because
 * the only honest reading of a fraction is one over a denominator it was measured
 * against — which is also why a PARTIAL answer is never stamped in the first place.
 *
 * `calls_truncated` is NOT a refusal, and that is the difference between a wrong
 * number and a qualified one: the tallies describe a prefix of the session, and the
 * line says so ("+K not scored") instead of the reader hiding a measurement that is
 * accurate about everything it covers. It is read through `asCount`, so an absent
 * field — every record stamped before it existed — reads as `0` and adds nothing.
 */
export function readCompactionKeepRecord(raw: unknown): CompactionKeepRecord | null {
  const root = asRecord(raw)
  if (!root) return null
  if (root.point !== DECISIONS_COMPACTION_POINT) return null
  const turnId = typeof root.turn_id === 'string' ? root.turn_id : ''
  if (!turnId) return null
  const totalCalls = asCountOrNull(root.total_calls)
  const keptBoth = asCountOrNull(root.kept_both)
  const keptCall = asCountOrNull(root.kept_call)
  const dropped = asCountOrNull(root.dropped)
  if (totalCalls === null || keptBoth === null || keptCall === null || dropped === null) return null
  if (totalCalls === 0 || keptBoth + keptCall + dropped !== totalCalls) return null
  const charsAll = asCountOrNull(root.chars_all)
  const charsJev = asCountOrNull(root.chars_jev_eligible)
  const share = charsAll !== null && charsJev !== null && charsAll > 0
    ? charsJev / charsAll
    : null
  // Above 1 means the two arms were measured against different universes, which is a
  // broken record rather than a keep-set that kept more than existed.
  const charsShare = share !== null && share <= 1 ? share : null
  return {
    turnId,
    point: DECISIONS_COMPACTION_POINT,
    totalCalls,
    keptCalls: keptBoth + keptCall,
    truncatedCalls: asCount(root.calls_truncated),
    charsShare,
  }
}

/**
 * Validate one raw model-routing record. `null` means "draw nothing".
 *
 * `tier` is required: it IS the answer, and the whole claim this row makes is
 * which tier the turn landed in. It is NOT held against a fixed list of three --
 * the tiers are the backend's own question domain, and a reader that refused a
 * fourth would hide a shipped tier instead of showing it.
 *
 * `modelChosen` may be `''`, and that is the SHIPPED state rather than a broken
 * producer: every tier of `decisions.model_route` is unpinned until an owner pins
 * one, because no model id may be hardcoded as a default. The row then reads
 * "complex -> (unpinned)" -- the tier was answered and nothing was applied, which
 * is exactly the evidence an owner pins from. Refusing it would hide the feature
 * for every install that has not been configured yet.
 */
export function readModelRecord(raw: unknown): DecisionModelRecord | null {
  const root = asRecord(raw)
  if (!root) return null
  const turnId = typeof root.turn_id === 'string' ? root.turn_id : ''
  if (!turnId) return null
  const tier = typeof root.tier === 'string' ? root.tier.trim() : ''
  if (!tier) return null
  const rawP = root.p
  const p = typeof rawP === 'number' && Number.isFinite(rawP) && rawP >= 0 && rawP <= 1 ? rawP : null
  const error = typeof root.error === 'string' && root.error.trim() ? root.error : null
  return {
    turnId,
    point: DECISIONS_MODEL_POINT,
    tier,
    // `''` for an unpinned tier, and for a non-string: both mean "no model was
    // applied", which is what the row prints.
    modelChosen: typeof root.model_chosen === 'string' ? root.model_chosen.trim() : '',
    // `''` is a real value and the common one: it is what the gateway records for
    // a session running the backend's own default, and printing "the default"
    // needs to be distinguishable from printing a model name.
    baselineModel: typeof root.baseline_model === 'string' ? root.baseline_model.trim() : '',
    p,
    latencyMs: asCount(root.latency_ms),
    historyChars: asCount(root.history_chars),
    truncated: asCount(root.truncated),
    // Only an exact `false` is a failed switch. The field is absent on an
    // unpinned row, which applied nothing by design, and on rows written before
    // it existed -- neither is a switch that did not take.
    applied: root.applied !== false,
    modelUsed: typeof root.model_used === 'string' ? root.model_used.trim() : '',
    error,
  }
}

/**
 * One recalled-memory decision, as the memory strip prints it.
 *
 * The two lists are `baseline_keys` and `jev_keys` on the wire, not `baseline`
 * and `jev`. That is the producer's choice and it is load-bearing here: those two
 * names are what `readDecisionStrip` requires, so a memory record spelling them
 * would be accepted by the skill reader and drawn as a skill selection holding
 * memory ids. Naming each list for what it holds makes the two records decline
 * each other on SHAPE as well as on `point`.
 *
 * `agree` is recomputed from the two lists for the reason `readDecisionStrip`
 * gives: a flag disagreeing with the lists beside it could only ever hide a real
 * divergence.
 */
export interface MemoryRecallRecord {
  /** Identifies the turn this decision belongs to; the feedback POST's subject. */
  turnId: string
  /** Always `memory.recall`. */
  point: typeof DECISIONS_MEMORY_POINT
  /**
   * The memory store the recalled ids belong to, `'default'` when the record
   * carried none. The id popover resolves each id in THIS store, so a member (V2)
   * recall's ids look up in their own store rather than misreporting as not found.
   */
  store: string
  /** The memories vector similarity recalled — every one Jev was offered. */
  baselineKeys: string[]
  /**
   * The memories Jev's answer KEPT — the decision's own output, not the set that
   * shipped. `boundedOmitted` says how many of these the response budget then
   * dropped, so the two together state what arrived and each stays attributable to
   * whoever removed it.
   */
  jevKeys: string[]
  /** Jev kept every recalled memory. */
  agree: boolean
  /**
   * The MEAN chance Jev gave one offered memory of being worth the prompt, or
   * `null` when the record carried none.
   *
   * A summary and nothing more: the request asked one question per memory, so
   * there is no single confidence to print. The producer says so too.
   */
  p: number | null
  /**
   * Characters of memory text JEV'S NARROWING removed, `0` when it removed none.
   *
   * Its two arms are measured on the same rows before redaction and before
   * bounding, so the scrubber's substitutions and the budget's clipping are no part
   * of it. It is the decision's own saving, not the reduction the response happened
   * to end up with.
   */
  charsSaved: number
  /** Memories the gate offered Jev to judge. */
  candidates: number
  /**
   * Characters of the message excerpt the question sent, or `null` when the
   * record does not state it. Nullable for the reason `DecisionStripRecord`
   * spells out: 0 is not a credible measurement of a turn that had text.
   */
  messageChars: number | null
  /** Milliseconds between asking Jev and its answer. */
  latencyMs: number
  /**
   * Memories Jev KEPT that the response budget then dropped, `0` when it dropped
   * none.
   *
   * The decision is not the last thing that shortens a recall: the payload is
   * bounded after it. Counted BESIDE `jevKeys` rather than subtracted from it, so
   * `jevKeys.length - boundedOmitted` is what arrived and neither number has to
   * absorb the other's removals.
   */
  boundedOmitted: number
  /** Why the decision failed, or `null` when it did not. */
  error: string | null
}

/**
 * Validate one raw recalled-memory record. `null` means "draw nothing".
 *
 * `point` is REQUIRED and must be the memory one. An absent point is the oldest
 * producer's shape and belongs to `readDecisionStrip`, so inferring this record
 * from the fields present would claim a decision about memory over a record that
 * never named one.
 */
export function readMemoryRecallRecord(raw: unknown): MemoryRecallRecord | null {
  const root = asRecord(raw)
  if (!root) return null
  if (root.point !== DECISIONS_MEMORY_POINT) return null
  const turnId = typeof root.turn_id === 'string' ? root.turn_id : ''
  if (!turnId) return null
  // Both lists whole before anything is drawn, the same rule the skill reader
  // states: the claim this strip makes is which memories were dropped, and it
  // cannot make it about a list it could not read.
  const baselineKeys = asNames(root.baseline_keys)
  const jevKeys = asNames(root.jev_keys)
  if (baselineKeys === null || jevKeys === null) return null
  // Absent (older producer, or a default-store recall) reads as the default store.
  const store = typeof root.store === 'string' && root.store.trim() ? root.store : 'default'
  const rawP = root.p
  const p = typeof rawP === 'number' && Number.isFinite(rawP) && rawP >= 0 && rawP <= 1 ? rawP : null
  const error = typeof root.error === 'string' && root.error.trim() ? root.error : null
  return {
    turnId,
    point: DECISIONS_MEMORY_POINT,
    store,
    baselineKeys,
    jevKeys,
    agree: sameSet(baselineKeys, jevKeys),
    p,
    charsSaved: asCount(root.chars_saved),
    boundedOmitted: asCount(root.bounded_omitted),
    candidates: asCount(root.candidates),
    messageChars: asCountOrNull(root.message_chars),
    latencyMs: asCount(root.latency_ms),
    error,
  }
}

/**
 * The recalled-memory record on one assistant row, or `null`.
 *
 * Found in the LIST rather than read from it: the memory record travels on
 * `decisions_strip` beside a skill or model record, and it is drawn by its own
 * component rather than by `DecisionStrip`, so `readDecisionRecords` declines it by
 * design. A bare object is read as a list of one, the way that reader does.
 */
export function readMemoryRecallInStrip(raw: unknown): MemoryRecallRecord | null {
  const list = Array.isArray(raw) ? raw : [raw]
  for (const entry of list.slice(0, MAX_RECORDS)) {
    const record = readMemoryRecallRecord(entry)
    if (record) return record
  }
  return null
}

/**
 * Validate one raw record of an assistant row's point. `null` means "draw nothing".
 *
 * Dispatched on the payload's own `point`. An absent point reads as
 * `skills.select`, which is what the older producer stamped and what the skill
 * reader already defaults to; any other named point draws nothing, because
 * rendering an unknown record through a known reader is how a row comes to print
 * a claim nobody made. `message.steer` is among the points that draw nothing
 * HERE: its record rides the user row and is read by `readSteerRecord`, so an
 * assistant row carrying one is a producer mistake rather than a row to render.
 */
export function readDecisionRecord(raw: unknown): DecisionRecord | null {
  const root = asRecord(raw)
  if (!root) return null
  const point = typeof root.point === 'string' && root.point ? root.point : DECISIONS_LIVE_POINT
  if (point === DECISIONS_MODEL_POINT) return readModelRecord(root)
  if (point !== DECISIONS_LIVE_POINT) return null
  return readDecisionStrip(root)
}

/**
 * Every record on one assistant row, in the order the gateway published them.
 *
 * Accepts the LIST the gateway stamps and the bare object an earlier release
 * stamped, so one reader covers a live frame and a reloaded transcript of any
 * age. An unreadable member is dropped rather than failing the row: the records
 * are independent decisions, and losing the receipt for one is not a reason to
 * lose the other's.
 */
export function readDecisionRecords(raw: unknown): DecisionRecord[] {
  const list = Array.isArray(raw) ? raw : [raw]
  const out: DecisionRecord[] = []
  for (const entry of list.slice(0, MAX_RECORDS)) {
    const record = readDecisionRecord(entry)
    if (record) out.push(record)
  }
  return out
}

/**
 * Thumbs already pressed in this page session, keyed by turn and side.
 *
 * The transcript is virtualised: a row leaving the window is unmounted and its
 * component state destroyed, which is the problem `rowDisclosure.ts` exists for.
 * That store holds booleans and a verdict is three-valued, so the answers live
 * here instead. Bounded by how many thumbs one reader presses before a reload,
 * and keyed by a turn id, so two sessions cannot collide.
 */
const verdicts = new Map<string, DecisionVerdictValue>()

const verdictKey = (turnId: string, side: DecisionFeedbackSide) => `${turnId}:${side}`

/** The answer this reader gave, or `null` when they have not answered. */
export function recordedVerdict(turnId: string, side: DecisionFeedbackSide): DecisionVerdictValue {
  return verdicts.get(verdictKey(turnId, side)) ?? null
}

/** Remember an answer the server accepted. */
export function rememberVerdict(turnId: string, side: DecisionFeedbackSide, verdict: DecisionVerdictValue): void {
  verdicts.set(verdictKey(turnId, side), verdict)
}

/** Drop every remembered answer, so one test cannot inherit another's. */
export function __resetVerdicts(): void {
  verdicts.clear()
}

/**
 * Pressing the thumb that is already lit takes the answer back, which is what
 * the `null` verdict is for. Pressing the other one replaces it.
 */
export function nextVerdict(current: DecisionVerdictValue, pressed: 'right' | 'wrong'): DecisionVerdictValue {
  return current === pressed ? null : pressed
}
