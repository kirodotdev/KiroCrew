/**
 * The Decisions (Jev) card's read model.
 *
 * Two sources, deliberately, because the two values live in two different
 * places on the gateway and the split IS the security design:
 *
 * - **Consent** — whether Jev may be asked at all — is the KEYSTONE
 *   `decisions_consent.json`, read and written through `/api/decisions/consent`.
 *   It is not a config path: `config.json` is writable by an auto-approved agent
 *   shell, so a switch there could be flipped by a prompt-injected agent and the
 *   live config watcher would start sending message text off the machine. The
 *   keystone is mounted read-only in every sandbox and its only writer is the
 *   owner-only dashboard handler behind this card.
 * - **The sampling share** — `decisions.bucket` — comes from `config.json` through
 *   the ordinary config GET. It grants nothing on its own (it can only narrow what
 *   consent allows), so it stays a config value.
 *
 * Nothing is inferred from a legacy `decisions.preview` / `points.*.arm` section:
 * that experiment sampled for COMPARISON and is retired; reading its values as
 * consent would turn on egress from a value nobody wrote for it.
 */

/** The point that chooses which skill a message loads. */
export const DECISIONS_LIVE_POINT = 'skills.select'

/**
 * The point that chooses whether a message sent into a RUNNING turn steers it or
 * queues for the next one.
 *
 * Named here, beside the skill point, because both are identifiers the gateway
 * owns: the strip reader dispatches on them and the Decisions card names them, so
 * a second spelling in either place would be a record nobody renders.
 */
export const DECISIONS_STEER_POINT = 'message.steer'

/** The point that chooses which model tier a chat turn runs on.
 *
 *  Every DECIDING point consumes its answer, and each is reached only through a
 *  choice the owner makes somewhere else: a non-zero `skills.max_triggered` for
 *  the skill point, the send button's `Auto (Jev)` entry for the steer point, and
 *  the chat model picker's `Auto (Jev)` entry for this one. The switch on this
 *  card is what lets any of them be asked at all, never what arms one.
 */
export const DECISIONS_MODEL_POINT = 'model.route'

/**
 * The point that scores, at every AUTOMATIC compaction, which of the session's tool
 * calls would be worth keeping.
 *
 * Named beside the others because it is the gateway's own identifier: the compaction
 * card's record dispatches on it and the Decisions card names it, so a second spelling
 * in either place would be a record nobody renders.
 *
 * The one SHADOW point in the list. The four above are asked so their answer can be
 * used; this one is asked so the answer can be measured, and the compaction runs
 * identically whatever it says -- which is why it needs no arming choice anywhere, only
 * its own consent scope.
 */
export const DECISIONS_COMPACTION_POINT = 'compaction.keep'

/**
 * The point that chooses which of the memories vector similarity recalled reach
 * the prompt.
 *
 * Named here beside the others for the same reason: the strip reader dispatches on
 * it, so a second spelling anywhere would be a record nobody renders.
 */
export const DECISIONS_MEMORY_POINT = 'memory.recall'

/**
 * Config path of the sampling share. One of the four `decisions.*` values the
 * config PATCH accepts, beside the three `model_route` tiers; the address and the
 * credential are deliberately not among them.
 */
export const DECISIONS_BUCKET_PATH = 'decisions.bucket'

/**
 * Config path of the prior-conversation budget the seam ASKS for, in characters.
 *
 * Not what the card writes: the number that decides how much conversation may leave
 * is the CEILING on the keystone (`historyBudget` on the view,
 * `api.saveDecisionsHistoryBudget`), and `PATCH /api/config/kirocrew` does not
 * accept this path. Named here for the pointer line that tells a reader where the
 * asked-for value lives.
 */
export const DECISIONS_HISTORY_BUDGET_PATH = 'decisions.history_budget_chars'

/**
 * Config path of the address decisions are sent to.
 *
 * READ-ONLY from the dashboard, deliberately: `PATCH /api/config/kirocrew` excludes
 * `decisions.provider.*` so a dashboard caller cannot choose where the state a
 * decision point collects is sent, and `api_key` beside it is schema-sensitive, so
 * the masked read hands back a sentinel a write would clobber. The card shows the
 * address and names this path; an operator moves it in the file.
 */
export const DECISIONS_ENDPOINT_PATH = 'decisions.provider.endpoint'

/** Config path prefix of the tier-to-model map `model.route` reads. */
export const DECISIONS_MODEL_ROUTE_PATH = 'decisions.model_route'

/**
 * The three tiers `model.route` may answer with, in the order the panel draws
 * them. The point's CLOSED answer domain (`DECISION_MODEL_ROUTE_TIERS` in
 * `config/sections.py`), restated here because the panel needs one row per tier and
 * a tier the question never offers could never be answered.
 */
export const DECISIONS_MODEL_ROUTE_TIERS = ['simple', 'medium', 'complex'] as const

/**
 * The one vault entry the provider credential may come from. `provider.api_key`
 * honours exactly `secret://TYPESAFE_API_KEY` and nothing else, because
 * `config.json` is agent-writable — so the card writes this NAME into the secrets
 * vault and never a value into config.
 */
export const DECISIONS_API_KEY_SECRET = 'TYPESAFE_API_KEY'

/**
 * Status a point row carries, as the gateway spells it. The EFFECTIVE answer, never
 * a switch position: `off` is every reason nothing would be sent.
 */
export const POINT_ACTIVE = 'active'
/** Consent stands, but this point's own egress category was never granted. */
export const POINT_NEEDS_SCOPE = 'needs_scope'
/** Nothing is sent: no consent, a moved endpoint, or a governance pin. */
export const POINT_OFF = 'off'

/** One decision point, as the overview list and its detail panel read it. */
export interface DecisionPointRow {
  /** The gateway's identifier, also the string a reader greps the decision log for. */
  id: string
  /** The keystone scope this point needs beyond consent itself, or `null`. */
  needsScope: string | null
  /** One of the three statuses above. An id this build does not know reads as `off`. */
  status: string
}

/**
 * The point rows out of a `GET /api/decisions/consent` body.
 *
 * Empty for a body that carries no `points` — an older gateway — and the card then
 * says the points cannot be listed rather than drawing a list written here. That is
 * the whole reason the rows come from the server: a build that ships another point
 * must light up a row with no edit on this side, so any array here would be a
 * second, quietly diverging registry.
 *
 * A row missing its `id` is dropped: there is nothing to label, nothing to grep the
 * log for, and no panel to open. An unknown `status` is kept as-is and renders as
 * `off` by the chip's own fallback — the fail-closed direction, since the alternative
 * is claiming a point is running on a word this build cannot read.
 */
export function readPoints(body: unknown): DecisionPointRow[] {
  const root = asRecord(body)
  const raw = root?.points
  if (!Array.isArray(raw)) return []
  const rows: DecisionPointRow[] = []
  for (const entry of raw) {
    const row = asRecord(entry)
    const id = typeof row?.id === 'string' ? row.id : ''
    if (!id) continue
    rows.push({
      id,
      needsScope: typeof row?.needs_scope === 'string' && row.needs_scope ? row.needs_scope : null,
      status: typeof row?.status === 'string' ? row.status : POINT_OFF,
    })
  }
  return rows
}

/** Bounds the backend clamps the sampling bucket to, restated for the reader. */
const BUCKET_MIN = 0
const BUCKET_MAX = 100

export interface DecisionsView {
  /**
   * Whether this gateway has the consent endpoint at all. An older gateway
   * (404 on the consent GET, or a config carrying only the retired `preview`
   * section) renders the switch disabled with the update notice.
   */
  supported: boolean
  /** The keystone's answer. Only an exact `true` reads as on. */
  enabled: boolean
  /**
   * Where a decision would be sent: the endpoint `config.json` names now. Shown
   * so the reader consents to an ADDRESS, not just to "sending".
   */
  configuredEndpoint: string
  /**
   * Consent was given, but for a different address than the config names now
   * (`provider.endpoint` was edited afterwards). Nothing is sent in this state;
   * the card says so and asks the owner to consent again.
   */
  endpointMoved: boolean
  /**
   * Sampling percentage worth PRINTING, or `null` when there is nothing to say.
   *
   * `null` covers the configs that mean "do not print a rate": the section or
   * field is absent (an older or hand-trimmed config), or it is not a whole
   * number in 0–100 (so the backend's own clamp decides, and this reader must not
   * guess which way). 100 IS printed: it is the shipped default, and "every
   * session" is the one share a reader deciding whether to consent most needs to
   * see.
   */
  bucket: number | null
  /**
   * Whether the owner consented to sending TOOL-CALL ARGUMENTS — the extra egress
   * category `tool.risk` needs, and the only thing that lets it run.
   *
   * Read from the keystone's own answer rather than inferred from `enabled`: a
   * consent recorded before this scope existed reads `false` here, which is
   * exactly the state its owner agreed to, and the second switch must draw that
   * rather than a value it guessed.
   */
  toolArgs: boolean
  /**
   * Whether the owner consented to sending a WHOLE SLOT TRANSCRIPT — the conversation
   * text and every tool-call input in it — the category `compaction.keep` needs and
   * the only thing that lets it run.
   *
   * Read from the keystone's own answer and never inferred from `toolArgs`: that scope
   * was reviewed as the arguments of the one call about to run, so reading it as
   * permission for everything the session has run would widen egress with no new
   * choice.
   */
  compaction: boolean
  /**
   * Whether the owner consented to sending the text of recalled memories — the
   * `memory.recall` scope. Read on the same fail-closed terms as `toolArgs`: only a
   * literal `true` counts, so an older gateway and a keystone written before this
   * scope existed both read false, and the card draws the switch off for them.
   */
  memoryText: boolean
  /**
   * The prior-conversation CEILING the owner reviewed, in characters.
   *
   * From the KEYSTONE, not from `config.json`. The two differ exactly when an agent
   * has raised the config value, and the keystone is the number that decides how
   * much conversation may leave: config says what the seam asks for, this says what
   * it is clamped to. Showing the config value would tell a reader a budget was in
   * force that the gate refuses to honour.
   *
   * 0 is the shipped default and means the message alone, so it is a real answer
   * rather than "nothing to say"; an absent or unusable field reads as 0, the
   * fail-closed direction.
   */
  historyBudget: number
  /**
   * The points this gateway ships, in its own order. Empty on a gateway that does
   * not project them, which the overview reports rather than papering over.
   */
  points: DecisionPointRow[]
}

const UNSUPPORTED: DecisionsView = {
  supported: false,
  enabled: false,
  configuredEndpoint: '',
  endpointMoved: false,
  bucket: null,
  toolArgs: false,
  compaction: false,
  memoryText: false,
  historyBudget: 0,
  points: [],
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null
}

/**
 * The sampling rate out of a `GET /api/config/kirocrew` body, or `null` when the
 * config gives nothing printable.
 *
 * A percentage of 0 IS printable and is kept: "on, and sampling nobody" is a
 * state an operator can otherwise only discover by waiting for a log line that
 * never comes.
 */
export function readBucket(config: unknown): number | null {
  const root = asRecord(config)
  const decisions = root ? asRecord(root.decisions) : null
  const raw = decisions?.bucket
  if (typeof raw !== 'number' || !Number.isInteger(raw)) return null
  if (raw < BUCKET_MIN || raw > BUCKET_MAX) return null
  return raw
}

/**
 * Read consent out of a `GET /api/decisions/consent` body.
 *
 * `undefined` — the query has not resolved, or it failed (a 404 on an older
 * gateway included) — reads as unsupported, which is also how the card renders
 * it: a switch offered against a keystone the dashboard has not read yet would
 * be guessing at its own current state.
 *
 * Only an exact `true` turns the preview on. The backend writes nothing else,
 * and a hand-edited `"true"` or `1` in the keystone is refused there too; this
 * reader mirrors that so the card never shows "on" for a value the gate reads
 * as off.
 */
export function readConsent(body: unknown): Omit<DecisionsView, 'bucket' | 'points'> {
  const root = asRecord(body)
  if (!root || !('enabled' in root)) {
    return {
      supported: false,
      enabled: false,
      configuredEndpoint: '',
      endpointMoved: false,
      toolArgs: false,
      compaction: false,
      memoryText: false,
      historyBudget: 0,
    }
  }
  const enabled = root.enabled === true
  const configuredEndpoint = typeof root.configured_endpoint === 'string' ? root.configured_endpoint : ''
  // `permits` is the server's own verdict (enabled AND same address). Read it
  // rather than re-deriving equality here, so the card and the gate cannot
  // disagree about whether anything is being sent.
  const endpointMoved = enabled && root.permits !== true
  // An exact `true`, like `enabled` above: this field decides whether a new
  // category of conversation content leaves the machine, so a truthy stand-in is
  // not a deliberate yes. An older gateway omits it entirely and reads as off.
  const toolArgs = root.tool_args === true
  // An exact `true` on the same terms, and read separately from `toolArgs`: this is
  // the widest of the three categories, so a truthy stand-in and a narrower yes are
  // both "no".
  const compaction = root.compaction === true
  // An exact `true` on the same terms again: a recalled memory is text the agent wrote
  // down in an earlier conversation, so neither scope beside this one stands for it.
  const memoryText = root.memory_text === true
  // A whole non-negative number or nothing: an older gateway omits the field, and a
  // value nobody can read back as a budget is not one. 0 either way, which is the
  // shipped default and the least that can leave.
  const rawBudget = root.history_budget_chars
  const historyBudget =
    typeof rawBudget === 'number' && Number.isInteger(rawBudget) && rawBudget >= 0 ? rawBudget : 0
  return {
    supported: true,
    enabled,
    configuredEndpoint,
    endpointMoved,
    toolArgs,
    compaction,
    memoryText,
    historyBudget,
  }
}

/** Combine the two reads into the card's one view. */
export function readDecisions(consentBody: unknown, config: unknown): DecisionsView {
  const consent = readConsent(consentBody)
  if (!consent.supported) return UNSUPPORTED
  return { ...consent, bucket: readBucket(config), points: readPoints(consentBody) }
}

/**
 * The tier-to-model map out of a `GET /api/config/kirocrew` body.
 *
 * Every tier is present in the result, and a tier the config does not name reads as
 * `''` — INHERIT, the turn keeps the model its session is already on. That is the
 * shipped default and the only default this file may carry: a concrete model id
 * would fail on the first prompt for every account not entitled to it, which is why
 * `code-review.yml` gates on one appearing in code at all.
 */
export function readModelRoute(config: unknown): Record<string, string> {
  const root = asRecord(config)
  const decisions = root ? asRecord(root.decisions) : null
  const route = decisions ? asRecord(decisions.model_route) : null
  const out: Record<string, string> = {}
  for (const tier of DECISIONS_MODEL_ROUTE_TIERS) {
    const raw = route?.[tier]
    out[tier] = typeof raw === 'string' ? raw : ''
  }
  return out
}
