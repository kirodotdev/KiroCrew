// Thin fetch wrapper for the Ops Mission Control backend.
//
// The backend registers its routes directly on the main gateway's aiohttp
// Application (backend/routes.py:register_routes), so the base path is
// /api/apps/ops-mission-control — the same convention as issue-radar and
// code-review-sage, NOT the /apps/{name}/api reverse-proxy prefix used by apps
// that run as a separate child process.
import { i18nT } from '../../i18n/t'

const API = '/api/apps/ops-mission-control'

export type Severity = 'critical' | 'warning' | 'info'
/**
 * `suppressed` means a HUMAN already parked this at the provider (an Alertmanager silence
 * or inhibition, a Zabbix maintenance window). It is distinct from `unknown` on purpose:
 * `unknown` is "we could not read the state", `suppressed` is "we read it and someone
 * parked it", and until the backend had a word for the second it stored it as the first.
 *
 * The dispatch loop claims only `firing`, so a suppressed signal is never investigated —
 * which is the point, and also why the UI must show it. A parked signal that silently
 * vanishes from the board makes "the app ignored my alarm" look identical to "someone
 * silenced it".
 */
export type SignalState = 'firing' | 'ok' | 'unknown' | 'suppressed'
export type IncidentStatus =
  | 'unclaimed'
  | 'dispatched'
  | 'investigating'
  | 'needs_human'
  | 'resolved'
  | 'escalated'
  | 'stale'
export type OperatingMode = 'observe' | 'propose' | 'act'
/**
 * `silence` is a TIME-BOXED suppression the app issues — the backend has carried it in
 * `models.VALID_ACTIONS` since the bounded-expiry verb landed, and this type simply
 * understated what the API accepts. Note the asymmetry with `SignalState`'s `suppressed`:
 * that is a suppression somebody ELSE applied and we read; this is one we apply.
 */
export type ActionKind = 'ack' | 'resolve' | 'comment' | 'silence'

/**
 * One operator-authored grant — the second key that `act` needs. App mode alone authorizes
 * nothing; `effective = min(app_mode, rule_mode)`.
 *
 * `resource_glob` OR `label_match` is REQUIRED for an `act` rule and the backend refuses a
 * submission that has neither with a 400: "act on everything this provider reports" is
 * exactly the blanket grant the two-key design exists to prevent. `actions` narrows further
 * — omitted means every verb the provider supports.
 */
export interface AutonomyRule {
  source: string
  mode: OperatingMode
  resource_glob?: string
  label_match?: Record<string, string>
  actions?: ActionKind[]
}

/**
 * React Query key for `/signals`, shared rather than re-declared per panel.
 *
 * Exported because three surfaces need the SAME cache entry: the Signals tab (which owns
 * the fetch, on an explicit button press) and the Board + Handover panels, which read it
 * with `enabled: false`. Polling every configured source costs real money and rate-limit
 * budget, so a second key would mean a second paid poll fired by merely looking at the
 * board — see `opsApi.signals`.
 */
export const SIGNALS_QUERY_KEY = ['ops-mission-control', 'signals'] as const

export interface Signal {
  id: string
  source: string
  title: string
  severity: Severity
  state: SignalState
  fired_at: string
  resource: string
  url: string
  labels: Record<string, string>
  /**
   * Heuristic identity for the KIND of failure, hashed from the rendered title and
   * resource. Bare numbers are stripped, so alarms differing only in a threshold share
   * one fingerprint — treat a fingerprint match as "looks like this", not "is this".
   */
  fingerprint: string
  /**
   * The provider's OWN stable identity, when it publishes one (an Alertmanager
   * fingerprint, a Datadog monitor id). Empty when it does not. A match on this is
   * exact, which is why the ledger prefers it over `fingerprint`.
   */
  provider_key: string
  /**
   * WHO parked this, in the provider's own words — an Alertmanager silence id from
   * `silencedBy`, or the alert named in `inhibitedBy`.
   *
   * Only meaningful when `state === 'suppressed'`, and EMPTY on any provider that
   * publishes no attribution. A UI must say so explicitly in that case rather than imply
   * we know who: an invented owner is worse than a blank, and the whole reason this field
   * exists is that without it "the app ignored my alarm" and "someone silenced it" look
   * identical to an operator.
   */
  suppressed_by: string
  /**
   * WHICH KIND of suppression: `'silenced'` (a person created a silence) or `'inhibited'`
   * (another, higher-ranked alert is masking this one). Empty when unknown.
   *
   * Worth rendering separately from `suppressed_by` because the operator's next move
   * differs — a silence is a decision to review or let expire, while an inhibition means
   * the thing to go look at is the OTHER alert, and this one is a symptom.
   */
  suppressed_reason: string
}

/**
 * What one signal source's LAST poll actually did.
 *
 * `configured` is a pure config check (registry.py `_provider_catalog`) and says nothing
 * about whether the source answered. This is the only field that does. Two subtleties the
 * UI must respect, both of them load-bearing:
 *
 * - `signals` is present ONLY on a success. `registry.poll_all` writes `{ok, detail, at}`
 *   on failure and adds the count on success, so `signals === undefined` is "we do not
 *   know", not "zero".
 * - A source id ABSENT from the map has NOT been polled — `poll_all` only records the
 *   sources it actually attempted, and returns early without touching health when nothing
 *   is configured. Absence is not health, and reading it as health is exactly the bug this
 *   type was added to kill.
 */
export interface SourcePollHealth {
  ok: boolean
  /** The provider's own failure text, verbatim. Empty on success. */
  detail: string
  /** ISO timestamp of the poll, so a stale failure can be shown with its age. */
  at: string
  signals?: number
  /**
   * Whether ABSENCE from this successful poll is evidence the signal recovered.
   *
   * `ok` answers "did we look"; this answers "did we see everything", and for one source
   * those differ. The webhook adapter is a PUSH SPOOL — `poll` drains the queue — so a
   * still-firing pushed signal appears in exactly one cycle's result and is missing from
   * every cycle after it whether or not anything changed at the sender. Recording that as
   * a plain success let the post-action recheck read absence as recovery through a
   * SUCCESSFUL poll, which is why the `ok` guard could not catch it.
   *
   * Present only on a success (the failure branch has nothing to say about completeness),
   * and ABSENT on an older gateway — so read it as `!== false`, i.e. default TRUE. Every
   * polled provider API is a snapshot; only a queue-draining source opts out.
   */
  snapshot?: boolean
}

/**
 * The `/signals` response. Named rather than inlined because the Board and Handover
 * panels read the same cached entry and need the type to talk about it.
 */
export interface SignalsResult {
  /** Every signal the poll returned, ANY state. Kept for compatibility; prefer `firing`. */
  signals: Signal[]
  /** State-filtered exactly as `dispatch.run_cycle` filters — what is wrong right now. */
  firing: Signal[]
  /**
   * Signals a provider POSITIVELY reports as recovered. Distinct from "gone": a caller may
   * resolve on these without consulting `poll_health`, because an explicit `ok` is evidence
   * while an absence is not.
   */
  cleared: Signal[]
  /**
   * Signals a HUMAN parked at the provider. The THIRD reason a signal can be absent from
   * `firing`, and it is neither of the other two: it did not clear, and we did look.
   *
   * So it must not be resolved on absence (nothing was fixed) and must not be treated
   * like `cleared` either (the provider is not reporting recovery — it is reporting that
   * somebody asked to stop hearing about it). Read `suppressed_by` for who.
   */
  suppressed: Signal[]
  unclaimed: Signal[]
  /** Per-source failure text, verbatim. Also carries the throttled-skip notice. */
  errors: Record<string, string>
  poll_health: Record<string, SourcePollHealth>
  all_sources_healthy: boolean
}

/** The five distinguishable states a signal source can be in, worst-first in severity. */
export type SourceHealthState = 'failed' | 'backing_off' | 'not_polled' | 'not_set_up' | 'ok'

export interface SourceHealth {
  state: SourceHealthState
  /** Short badge label. */
  label: string
  /** Badge variant, matching `components/ui` Badge. */
  variant: 'ok' | 'err' | 'warn' | 'muted'
  /** The backend's verbatim reason, when there is one. Never invented here. */
  detail: string
  /** ISO timestamp of the poll this describes, or '' when there was none. */
  at: string
  /** Whether this source contributed signals to the current poll result. */
  contributing: boolean
  /**
   * Whether absence from this source's poll can be read as recovery.
   *
   * FALSE only for a source that drains a queue on poll (the inbound webhook spool), where
   * a still-firing pushed signal is missing from every cycle after the one that delivered
   * it. Also false whenever the poll did not succeed, because nothing can be concluded from
   * a poll we did not get — so a caller can gate on this ONE boolean instead of remembering
   * both reasons.
   */
  absenceIsEvidence: boolean
}


/**
 * Whether a provider error is the throttle notice rather than a real failure.
 *
 * A PREDICATE, not a prefix constant. The literal it matches is the wire text
 * `registry.poll_all` emits for a skipped-because-throttled poll (`backend/registry.py`) —
 * text this code compares against and never renders, so translating it would break the
 * match. Holding it in an ALL-CAPS constant put it where `eslint-plugin-i18next` cannot see
 * it (those are exempt by default) and the strict wrapper then flagged it with no shape
 * narrow enough to exempt without also exempting real two-word copy. Inside `startsWith` it
 * is a comparison operand, which the callee exclusion covers by shape.
 */
function isBackoffNotice(err: string): boolean {
  return err.toLowerCase().startsWith('backing off')
}

/**
 * Collapse (`poll_health`, `errors`, `configured`) into the one state a row should show.
 *
 * Lives in api.ts rather than in a panel because the Board, the Signals tab and the
 * Handover digest all have to answer "is this source actually watching?" and must not word
 * it three different ways — the same argument the backend already makes by pre-rendering
 * `HandoverDigest.text`.
 *
 * `backing_off` is detected by sniffing the start of the error string, because the backend
 * reports a throttled skip only as prose (`registry.poll_all`'s `_poll_one` is the sole
 * producer). That is deliberately a soft check: if the message is ever reworded this
 * degrades to plain `failed`, which is still true and still red — it never degrades to
 * `ok`, which would be a lie.
 */
/**
 * Operator-facing wording for `blocked_reason`, shared by every surface that shows it.
 *
 * Lives here rather than in the board because the board and the handover digest render the
 * SAME field and had drifted: the board mapped `awaiting_approval` to "Approve to
 * continue", while the handover printed the raw enum with underscores swapped for spaces
 * ("awaiting approval"). An incoming responder reading the handover therefore could not
 * match a row to the board row they were inheriting — at a shift change, which is the one
 * moment the digest exists for.
 *
 * A shared constant rather than two correct-looking maps: the failure mode of duplication
 * here is silent, because each surface reads fine on its own and only the comparison is
 * wrong.
 */
export const BLOCKED_LABEL_KEY = {
  awaiting_approval: 'apps.opsMissionControl.opsMissionControlPage.waiting_awaiting_approval',
  awaiting_input: 'apps.opsMissionControl.opsMissionControlPage.waiting_awaiting_input',
  awaiting_diagnosis: 'apps.opsMissionControl.opsMissionControlPage.waiting_awaiting_diagnosis',
} as const

/** The reasons `BLOCKED_LABEL_KEY` has copy for — its own key type, for index narrowing. */
export type BlockedReason = keyof typeof BLOCKED_LABEL_KEY

/** `BLOCKED_LABEL_KEY` resolved, with a readable fallback so an unmapped future reason still
 * prints. Catalog keys rather than English literals: an ALL-CAPS module constant is exempt
 * from `eslint-plugin-i18next`, so the literals shipped untranslated with no gate to catch
 * it. Both call sites go through this function or the map, so resolution stays in one place. */
/** Whether `BLOCKED_LABEL_KEY` has real copy for this reason, as opposed to the
 * underscore-stripped fallback. Exported so a caller can branch on "is this a reason we
 * have wording for" without indexing the map across a module boundary — `check-i18n-keys`
 * resolves an `as const` map only in the file that declares it. */
export function isKnownBlockedReason(reason: string | undefined): reason is BlockedReason {
  return !!reason && reason in BLOCKED_LABEL_KEY
}

export function blockedLabel(reason: string | undefined): string {
  if (!reason) return ''
  // Indexed with the `as const` map's OWN key type, not a bare `string`: `tsc -b` rejects
  // `string` indexing a literal object (TS7053), and widening the map to
  // `Record<string, string>` instead would erase the literal values that let
  // `check-i18n-keys` verify every key exists. Narrowing at the index site keeps both.
  //
  // Indexed DIRECTLY inside `i18nT(...)` rather than via a local: that gate resolves element
  // access on an `as const` map and cannot follow a `const` indirection.
  if (!isKnownBlockedReason(reason)) return reason.replace(/_/g, ' ')
  return i18nT(BLOCKED_LABEL_KEY[reason])
}

export function describeSourceHealth(
  id: string,
  health: Record<string, SourcePollHealth> | undefined,
  errors: Record<string, string> | undefined,
  configured: boolean,
): SourceHealth {
  const err = errors?.[id] ?? ''
  const entry = health?.[id]
  if (isBackoffNotice(err)) {
    // Not an operator error, but the source contributed NOTHING this cycle, so it must
    // not read as ok. This is the state that used to render "ready / ok".
    return {
      state: 'backing_off',
      label: i18nT('apps.opsMissionControl.api.source_health_backing_off'),
      variant: 'warn',
      detail: err,
      at: entry?.at ?? '',
      contributing: false,
      absenceIsEvidence: false,
    }
  }
  if (err || entry?.ok === false) {
    return {
      state: 'failed',
      label: i18nT('apps.opsMissionControl.api.source_health_failed'),
      variant: 'err',
      detail: err || entry?.detail || i18nT('apps.opsMissionControl.api.the_last_poll_failed'),
      at: entry?.at ?? '',
      contributing: false,
      absenceIsEvidence: false,
    }
  }
  if (!configured) {
    return {
      state: 'not_set_up',
      label: i18nT('apps.opsMissionControl.api.source_health_not_set_up'),
      variant: 'muted',
      detail: '',
      at: '',
      contributing: false,
      absenceIsEvidence: false,
    }
  }
  if (!entry) {
    // Configured but never attempted: no poll has run this session, or the poll ran
    // before this source was configured. Muted, NOT ok.
    return {
      state: 'not_polled',
      label: i18nT('apps.opsMissionControl.api.source_health_not_polled_yet'),
      variant: 'muted',
      detail: '',
      at: '',
      contributing: false,
      absenceIsEvidence: false,
    }
  }
  return {
    state: 'ok',
    label: 'ok',
    variant: 'ok',
    detail: '',
    at: entry.at,
    contributing: true,
    // `!== false`, so an older gateway that sends no `snapshot` keeps the previous, correct
    // reading. Only a source that positively declares itself a drained queue opts out.
    absenceIsEvidence: entry.snapshot !== false,
  }
}

/**
 * A drafted action awaiting a human decision — `Incident.proposed_action`, typed.
 *
 * The fields ARE the contract. `digest` is a hash over `action`/`sink`/`note`/
 * `duration_secs`, so an approval that echoes it is approving the exact terms rendered
 * and nothing else; the route refuses a stale one. Rendering `note` verbatim matters for
 * the same reason — it is the outbound text, stored rather than regenerated.
 */
export interface PendingProposal {
  state: 'pending' | 'approved' | 'rejected' | 'expired'
  action: ActionKind
  sink: string
  /** The verbatim outbound text. Display as stored; do not re-word it. */
  note: string
  /** `silence` only; `null` for every non-expiring verb. */
  duration_secs: number | null
  digest: string
  proposed_at: string
  expires_at: string
  decided_at: string
  /** Present on the queue route (`GET /proposals`), which joins the incident's identity. */
  incident_id?: string
  title?: string
  source?: string
  severity?: Severity
}

export interface Incident {
  incident_id: string
  signal: Signal
  status: IncidentStatus
  operating_mode: OperatingMode
  claimed_at: string
  updated_at: string
  slot_key: string
  slack_thread_ts: string
  ledger_matches: string[]
  diagnosis: string
  /** The drafted action awaiting a decision. See `PendingProposal`. */
  proposed_action: PendingProposal | null
  resolution: string
  /**
   * Why this incident is waiting on a person, derived from its investigation slot
   * (empty when it is not blocked). `needs_human` alone reads the same whether the
   * agent wants a decision or gave up, so the board shows this instead.
   */
  blocked_reason?: 'awaiting_approval' | 'awaiting_input' | 'awaiting_diagnosis' | ''
  /**
   * The last provider action this app executed for this incident, or '' when none has been.
   *
   * Optional because every incident written before verification existed carries neither
   * this nor the three fields below — and absent must read as "nothing was attempted",
   * never as "an action succeeded".
   */
  last_action?: ActionKind | ''
  last_action_at?: string
  /**
   * What CHECKED that the action landed, which is the thing a 2xx from a provider does
   * not tell you. Checkmk dispatches commands asynchronously and documents that its 2xx
   * "only indicates whether the request was successfully transmitted"; Nagios's command
   * pipe returns nothing at all.
   *
   * - `''` — no action was ever executed. The overwhelming majority. NOT "verified fine".
   * - `pending` — the recheck is scheduled and has not run.
   * - `cleared` — the recheck ran against a SUCCESSFUL poll and the signal is gone.
   * - `still_firing` — the recheck ran against a successful poll and it is still firing.
   *   The app previously reported this action as applied, so this outranks every other
   *   verification state for display.
   * - `unknown` — the recheck was due and the source could not be read. This is a
   *   statement about US, not about the world, so it is NOT terminal: a later cycle
   *   retries it. A UI must never paint it as either success or failure.
   * - `not_checkable` — the action was executed but its outcome is not observable here.
   *   An acknowledgement leaves an alert firing by design, so firing state says nothing
   *   about whether the ack landed. Say "not checked", never leave a blank that reads as
   *   success.
   */
  verification?: '' | 'pending' | 'cleared' | 'still_firing' | 'unknown' | 'not_checkable'
  /**
   * The recheck's own sentence, including WHICH source could not be read when the verdict
   * is `unknown`. Render it verbatim — re-wording it in the panel would re-lose the
   * reason the backend already has.
   */
  verification_detail?: string
  /** When the recheck becomes due. Empty for an action whose outcome is not observable. */
  verify_after?: string
}

/**
 * How to PRESENT an incident's post-action verdict, in one place.
 *
 * In api.ts rather than in a panel for the same reason `describeSourceHealth` is: the
 * Board, the postmortem card and any future surface must not word the same five states
 * three different ways, and one of those states (`unknown`) is specifically easy to word
 * as either success or failure when it is neither.
 *
 * Returns `null` when there is nothing to say — no action was executed, which is true of
 * almost every incident. A caller renders nothing at all in that case rather than a
 * "not applicable" row that would bury the cases that matter.
 */
export interface VerificationView {
  label: string
  variant: 'ok' | 'warn' | 'err' | 'muted'
  /** One sentence of OUR framing. The backend's own `verification_detail` is separate. */
  meaning: string
}

export function describeVerification(incident: Incident): VerificationView | null {
  const verdict = incident.verification || ''
  const verb = incident.last_action || 'action'
  switch (verdict) {
    case 'still_firing':
      return {
        label: i18nT('apps.opsMissionControl.api.verification_still_firing_label'),
        variant: 'err',
        meaning: i18nT('apps.opsMissionControl.api.verification_still_firing_meaning', { verb }),
      }
    case 'cleared':
      return {
        label: i18nT('apps.opsMissionControl.api.verification_confirmed_label'),
        variant: 'ok',
        meaning: i18nT('apps.opsMissionControl.api.verification_confirmed_meaning', { verb }),
      }
    case 'pending':
      return {
        label: i18nT('apps.opsMissionControl.api.verification_pending_label'),
        variant: 'muted',
        meaning: incident.verify_after
          ? i18nT('apps.opsMissionControl.api.verification_pending_meaning_at', {
              verb,
              when: incident.verify_after,
            })
          : i18nT('apps.opsMissionControl.api.verification_pending_meaning', { verb }),
      }
    case 'unknown':
      return {
        // Deliberately not 'failed'. The action may well have worked; we could not look.
        label: i18nT('apps.opsMissionControl.api.verification_could_not_check_label'),
        variant: 'warn',
        meaning: i18nT('apps.opsMissionControl.api.verification_could_not_check_meaning'),
      }
    case 'not_checkable':
      return {
        label: i18nT('apps.opsMissionControl.api.verification_not_checkable_label'),
        variant: 'warn',
        meaning: i18nT('apps.opsMissionControl.api.verification_not_checkable_meaning', { verb }),
      }
    default:
      return null
  }
}

export interface ProviderInfo {
  id: string
  display_name: string
  roles: string[]
  configured: boolean
  config_fields: string[]
  secret_fields: string[]
  detail: string
  config: Record<string, unknown>
  /** Set/unset only — the API never returns a stored secret value. */
  secrets: Record<string, string>
}

/** One member of a committed on-call schedule. */
export interface RosterMember {
  login: string
  /** How many shift windows this member holds — makes an unbalanced rotation visible. */
  shifts: number
  on_call_now: boolean
}

/**
 * The team behind a committed `rotation.yaml`, when that is the rotation source.
 *
 * `who` alone cannot answer the question an operator actually has: is my instance idle
 * because a teammate holds the pager, or because the schedule is broken? Under strict
 * gating an indeterminate schedule DISARMS, so a silently-idle instance is a real
 * failure mode — the roster is what makes it legible.
 */
export interface RotationRoster {
  members: RosterMember[]
  windows: { from: string; to: string; who: string[]; current: boolean }[]
  timezone: string
  /** This instance's resolved GitHub login, so the UI can mark "you". */
  me: string
  /** False when `me` appears nowhere in the schedule — a setup mistake, not a quiet shift. */
  me_on_roster: boolean
  strict_gating: boolean
  /**
   * The teammate who runs nightly ledger hygiene, from the schedule's `leader:` key.
   *
   * Worth displaying rather than leaving implicit: hygiene prunes the shared ledger, and
   * before this every instance claimed the job by default (`primary_instance` defaults to
   * true and is per-instance), so N agents pruned one ledger. Showing the owner makes
   * "exactly one" visible instead of assumed. Empty when the schedule names no leader.
   */
  leader: string
  error: string
}

/**
 * The two rotation IDENTITIES, read off the keystone floor.
 *
 * Reported on the rotation response rather than in the provider catalog because both moved off
 * `config_fields`: they are inputs to the off-shift refusal, and provider config is
 * agent-writable and served unauthenticated, so an agent that could write them could claim to
 * be whoever is on call. `PUT /settings` is their only writer.
 *
 * Optional: an older gateway omits the key entirely.
 */
export interface RotationIdentities {
  /** This operator's GitHub login, matched against `who:` in `rotation.yaml`. */
  schedule_github_login: string
  /** This operator's PagerDuty user id, matched against the `oncalls` response. */
  pagerduty_user_id: string
}

export interface RotationInfo {
  identities?: RotationIdentities
  on_shift: boolean
  who: string
  /**
   * When the current shift ends, ISO-8601, or `''` when the source publishes none.
   *
   * Only the schedule-file provider sets it (`ShiftStatus.until`), so it is blank on a solo
   * install. Rendered into the header's shift badge when present, because it answers the
   * question a badge saying "on shift: octocat" leaves open — how long that holds, which is
   * what a responder needs before starting anything long.
   */
  until: string
  /**
   * The rotation source could not say whether THIS operator is on call.
   *
   * Under strict gating (the default for a committed schedule) this comes back with
   * `on_shift: false` — the tier is DISARMED, because with a file every instance reads,
   * "cannot tell" means the schedule is wrong, and arming would make every instance in
   * the team pick up the same work. Read `on_shift` for arming; `unknown` only explains
   * WHY, so the UI can distinguish "someone else has it" from "the file is broken".
   */
  unknown: boolean
  /**
   * Which automation tiers are armed, `{tier: armed}`.
   *
   * DELIBERATELY NOT RENDERED, and this is the one omission on this payload worth arguing
   * for rather than assuming. A tier is an implementation of the shift decision, not a
   * separate fact: `tier_states` derives `on_shift` straight from `ShiftStatus.on_shift`,
   * which the header badge already reports in the operator's own terms ("off shift — octocat
   * is on call"). A second row of raw tier booleans would be the same answer in vocabulary
   * only the SOPs use, and the failure mode is real — the two can never disagree, so the
   * only thing a reader can do with a discrepancy is doubt the badge.
   */
  tiers: Record<string, boolean>
  /**
   * Cron names running right now, flattened across armed tiers. AGENT-FACING, not rendered.
   *
   * The rotation-check SOP is told to pause exactly `tier_crons.on_shift` and never this
   * union — off shift the union still contains the `always`-tier `rotation-check` cron, and
   * pausing that one strands the instance unable to re-arm. Showing either list to an
   * operator would be showing them Kiro Crew's cron plumbing on an ops board, and the
   * actionable half of it (is this instance picking up work) is the shift badge.
   */
  armed_crons: string[]
  /**
   * Cron names PER TIER, `{tier: [cron, …]}` — armed or not. Also agent-facing.
   *
   * Declared even though nothing renders it, because the distinction from `armed_crons` is
   * load-bearing and undeclared it was invisible to anyone reading this file: the
   * rotation-check SOP must pause exactly `tier_crons.on_shift`, and a future panel reaching
   * for "the cron list" would find only the flat union above, which off shift still contains
   * the `always`-tier `rotation-check` job. Pausing that one permanently disables incident
   * response, because it is the job that re-arms the instance.
   */
  tier_crons: Record<string, string[]>
  mode: OperatingMode
  rules: number
  /**
   * The act-rules themselves, in the SAME shape `putSettings({autonomy_rules})` accepts —
   * read, edit, PUT back. Only `rules` (a count) used to be sent, which is why Settings
   * dead-ended at "No rules defined yet." with nothing to click and no way to see an
   * existing grant: a number cannot be rendered, edited or verified.
   *
   * Serialized from the PARSED rules, so what is listed is what the gate will actually use —
   * an entry that failed validation is absent rather than shown as if it were live.
   *
   * Optional: an older gateway sends only the count.
   */
  rules_detail?: AutonomyRule[]
  primary: boolean
  /**
   * The modes the backend accepts for `mode`. Not rendered as data: Settings' segmented
   * control is the render, and its three segments are declared statically there because each
   * needs an icon and a paragraph of consequence copy that no server list can carry. Kept
   * declared so a future fourth mode is a type error at the segment list rather than a
   * silently missing button.
   */
  modes_available: OperatingMode[]
  /** Empty object when no committed schedule is in use. */
  roster?: RotationRoster
  /**
   * Optional because `rotation.describe` is its only producer: an older gateway sends
   * nothing, and the panel must then say "not reported" rather than invent the defaults —
   * printing 2 h against a gateway that might be running 30 m is the same class of
   * confident-but-wrong claim this app treats as a defect.
   */
  sweep?: SweepWindows
}

/**
 * How fast the heartbeat claims, and how long it lets work sit before releasing it.
 *
 * These three were accepted by `PUT /settings` and returned by NO read path, so an
 * operator could set them and never see them again — including the defaults they were
 * living under. Every value here is the one `dispatch.run_cycle` will actually apply.
 */
export interface SweepWindows {
  /** Ceiling on new claims per heartbeat, so one alarm storm cannot claim the whole board. */
  max_claims_per_cycle: number
  /** Idle seconds before a claimed investigation is released for re-pickup. */
  stale_after_secs: number
  /**
   * The RESOLVED window for `needs_human`, never the raw config value.
   *
   * Unset does not mean "never released": `store.sweep_stale` derives this from
   * `stale_after_secs` by a multiplier. So the backend resolves it before sending, and a
   * UI must render this number as the real answer rather than as "default".
   */
  needs_human_stale_after_secs: number
  /**
   * True when the number above was computed from `stale_after_secs` rather than set by the
   * operator. The two are worth distinguishing: a derived window MOVES when the working
   * threshold changes, and an explicit one does not.
   */
  needs_human_derived: boolean
}

export interface LedgerEntry {
  entry_id: string
  pattern: string
  fix: string
  fingerprints: string[]
  /** Provider-computed identities this entry has matched. Empty on older entries. */
  provider_keys: string[]
  confidence: 'high' | 'medium' | 'low'
  trust: 'verified' | 'observed'
  /**
   * Times a real firing signal matched this entry and it was handed to an investigation.
   *
   * Counted at CLAIM time, before any outcome exists — so on its own it means "was shown
   * to somebody", not "worked". That is why `miss_count` exists beside it and why neither
   * number should be rendered without the other.
   */
  use_count: number
  /**
   * Times this fix was cited and the failure came back anyway.
   *
   * Written only by an observed post-action recheck (`dispatch.verify_pending_actions`
   * saw the signal STILL FIRING against a source whose poll succeeded), never by
   * inference and never from a POST body. 0 on every entry written before this existed,
   * which reads correctly as "never contradicted".
   *
   * This is the ledger's only mechanical evidence AGAINST an entry, and it is a louder
   * fact than a low confidence: an untested hypothesis is worth more than a refuted one,
   * so a UI must not render `miss_count > 0` the same way it renders "not proven".
   */
  miss_count: number
  /** When the most recent miss was recorded. Empty until there is one. */
  last_miss: string
  first_seen: string
  last_used: string
  source: string
}

/**
 * Whether an entry has cleared the engine's fast-path bar, computed the SAME way
 * `ledger.entry_unlocks_fast_path` computes it.
 *
 * Exported and used by every surface rather than each panel restating "verified and
 * high": that restatement is exactly what went stale in `handover.recurring_patterns`
 * when the bar gained a use floor and a miss ceiling, and a UI that disagrees with the
 * brief the agent was handed leaves the operator no way to tell which is lying.
 *
 * Four conditions. `use_count >= 2` rather than `>= 1` because the backend calls
 * `record_use` BEFORE judging, so every match whatsoever has at least 1 — a floor of 1
 * would admit a hand-authored entry matching for the very first time, which is the case
 * the floor exists to exclude.
 */
export const MIN_USES_FOR_FAST_PATH = 2

export function entryIsProven(entry: LedgerEntry): boolean {
  return (
    entry.trust === 'verified' &&
    entry.confidence === 'high' &&
    entry.use_count >= MIN_USES_FOR_FAST_PATH &&
    entry.miss_count === 0
  )
}

/**
 * Ledger rollups from `ledger.stats()`.
 *
 * NOT ALL OF THESE ARE RENDERED, and that is a decision rather than an oversight — so it
 * is recorded here, beside the fields, rather than left for the next reader to rediscover:
 *
 * - `total` and `proven` are the two Board stat cards. They are the pair that answers the
 *   only aggregate question an operator has ("how much do we know, and how much of it would
 *   an agent act on without checking"), and the GAP between them is the honest picture.
 * - `demoted` joins them, but only when non-zero (see the Board's third card).
 * - `verified`, `high_confidence`, `total_uses` and `total_misses` are deliberately NOT
 *   rendered anywhere. Each is one component of a judgement `proven` and `demoted` already
 *   make correctly, and showing a component beside the verdict invites reading it AS the
 *   verdict — which is the precise mistake `proven` was added to stop. "12 verified" reads
 *   as authority; it can be 12 entries nobody has ever successfully applied, and the fast
 *   path admits none of them. The per-entry breakdown IS shown, in the Knowledge ledger
 *   table (trust / confidence / used / failed columns), where a number sits next to the
 *   pattern it describes and cannot be mistaken for a fleet-wide score.
 *   `total_uses` in particular is a sum over a counter that increments at CLAIM time,
 *   before any outcome exists, so as a headline it would be a large number meaning
 *   "how often we showed somebody something".
 */
export interface LedgerStats {
  total: number
  verified: number
  high_confidence: number
  total_uses: number
  /**
   * Entries that clear the whole fast-path bar.
   *
   * `verified` and `high_confidence` are each one HALF of it, so neither answers "how
   * much of this ledger would an agent propose without checking" — an entry can be
   * counted in both while being something nobody has ever successfully applied. Showing
   * only those two overstated the ledger's authority, which is why this exists and why
   * it can be strictly smaller than either.
   */
  proven: number
  /** Entries carrying recorded evidence their fix did not hold. */
  demoted: number
  total_misses: number
}

/**
 * Slack output-channel state. `ready` is the only field the UI should gate on;
 * the three booleans exist so Settings can name WHICH half is missing, since the
 * fixes differ (flip a toggle / enter a channel / configure Kiro Crew's Slack).
 */
export interface SlackOutStatus {
  enabled: boolean
  channel: string
  /** Whether Kiro Crew's OWN Slack client exists — this app stores no token. */
  slack_available: boolean
  ready: boolean
  detail: string
}

/** One channel this app's manifest declares, as the backend reads it back off disk. */
export interface NotifyChannel {
  /** Kebab-case id; the bus namespaces it as `ops-mission-control.<id>`. */
  id: string
  name: string
  /** Lucide icon name, or '' when the manifest names none. Never an emoji. */
  icon: string
  /** 'critical' | 'default' | 'passive' — typed as string because the manifest is on disk. */
  default_priority: string
}

/**
 * Local desktop notifications — the one push channel that needs no credential.
 *
 * `ready` is the only field to gate the happy path on, the same rule as
 * `SlackOutStatus.ready`. The two booleans before it exist because the fixes are
 * different and only one of them is the operator's to make:
 *
 * - `enabled` false means flip the toggle in this app's Settings.
 * - `bus_available` false means this is not the gateway process. The bus lives on
 *   `DashboardState`, so a CLI or test run has none — no toggle would help, and telling
 *   someone to flip one would be advice that cannot work.
 *
 * `channels` is the manifest's DECLARATION, not the bus registry, and that distinction is
 * the reason it is here at all: registration is lazy (a channel is registered on its
 * first push), so the central Settings → Notifications rail — which lists registered
 * channels — shows NOTHING for a freshly installed app until a notification actually
 * fires. Declaring the list on this payload is what lets an operator see which channels
 * exist before any of them has ever spoken.
 *
 * `detail` is the backend's own sentence for whichever state applies; render it verbatim
 * rather than re-deriving the wording here.
 */
export interface NotifyOutStatus {
  enabled: boolean
  /** Whether a notification bus exists in the process serving this request. */
  bus_available: boolean
  ready: boolean
  detail: string
  channels: NotifyChannel[]
}

/**
 * Shared-ledger git sync — the team's memory-exchange repo.
 *
 * `ready` is the only field to gate the happy path on. The three that precede it exist so
 * Settings can name WHICH half is missing, because the fixes are different and unrelated:
 * `enabled` false means flip the toggle, an empty `remote` means enter a git URL, and
 * `initialized` false is not a fault at all — the repo is created on the first sync, so a
 * panel that painted it red would send an operator hunting for a problem they do not have.
 *
 * The two conflict flags are NOT the same severity, and conflating them would be wrong in
 * both directions:
 *
 * - `conflict` (the ledger) is reconcilable. Entries are content-addressed, `read_entries`
 *   skips the markers, and the next sync rewrites the file from the union — so sync keeps
 *   working. A note, not an error.
 * - `schedule_conflict` (`rotation.yaml`) makes `ledger_sync.push` REFUSE outright, so
 *   nothing new reaches the team at all until a human resolves the file. It is the one
 *   state that must read as an error, and until the backend started reporting it the card
 *   would have said "Syncing …" through an indefinite publishing outage.
 *
 * `detail` is the backend's own sentence and names the fix for whichever state applies;
 * render it verbatim rather than re-deriving the wording here.
 */
export interface LedgerSyncStatus {
  enabled: boolean
  /** The git remote, as the operator typed it. Not a credential — see `putSettings`. */
  remote: string
  /**
   * The branch the operator CONFIGURED (`ledger_sync_branch`, or `main`). This is what
   * sync fetches, merges and pushes — always, through explicit refspecs.
   *
   * Do not confuse this with `local_branch`. Confusing them IS the bug this pair exists to
   * expose: on the author's live install config said `main` while `.git/HEAD` said
   * `master`, because `git init` picked its own default and nothing ever moved HEAD. Sync
   * still worked, so nothing surfaced — while a plain `git pull` in that directory failed
   * outright with "no tracking information for the current branch".
   */
  branch: string
  /**
   * The branch `.git/HEAD` actually points at. `''` when the repo is not initialized yet
   * or when HEAD is detached (a bare sha is not a branch name).
   *
   * Exists to name WHICH branch the repo drifted onto, so the operator can run the one
   * `git` command that fixes it by hand if they want to.
   */
  local_branch: string
  /**
   * The ONLY field a card should gate its branch warning on — the same rule as
   * `SlackOutStatus.ready` above. True when `local_branch === branch`, and also true for a
   * repo that is not initialized yet, because there is nothing there to disagree with and a
   * warning on the ordinary pre-first-sync state teaches the operator to ignore this field.
   */
  branch_matches: boolean
  /**
   * HEAD is a raw sha with no branch checked out. Separate from a plain mismatch because
   * the REMEDIES differ, which is the whole reason both exist: a mismatch the next sync
   * repairs by itself, while a detached HEAD is deliberately left alone (it means a merge
   * or rebase went sideways, and moving refs under it can lose work in progress) — so the
   * operator has to finish or abort it themselves.
   */
  detached: boolean
  /** Whether `git init` has run in the ledger directory yet. */
  initialized: boolean
  ready: boolean
  conflict: boolean
  schedule_conflict: boolean
  detail: string
}

/**
 * One piece of provider context the gateway gathered about a signal, on the claim and
 * dispatch responses.
 *
 * Exists because THE AGENT HAS NO CREDENTIALS: the gateway is the only thing that can read
 * the alarm history or the logs an investigation needs, so it brokers them at claim time
 * and hands them over as text. `body` has already been through both redaction passes.
 *
 * Note the shape is narrower than the backend's `Evidence` dataclass, which also carries a
 * `url` — `ClaimedIncident.to_dict` projects only these four fields, so declaring `url` here
 * would be this file asserting a link the response does not contain.
 *
 * Not rendered. It is investigation input, and the surface for it is the embedded chat
 * (`IncidentChat`), where the agent has already summarised it against the diagnosis it drew
 * — a raw dump of gathered log text on the board would be the same bytes without the
 * reasoning, and this app's whole claim is that the investigation happens for you.
 */
export interface Evidence {
  source: string
  /** What kind of context this is (alarm history, recent logs, …), in the adapter's words. */
  kind: string
  title: string
  /** Redacted at the adapter boundary — both the core pass and the provider-token pass. */
  body: string
}

/**
 * An installed companion adapter package. Reported from what is *installed*, which
 * is a different question from what was admitted at boot — so "none installed" is
 * distinguishable from "installed but rejected", which need different fixes.
 */
export interface CompanionInfo {
  name: string
  target: string
}

/** Shift handover digest — a read-only projection, computed fresh per request. */
export interface HandoverDigest {
  /** One sentence for someone who reads nothing else. */
  headline: string
  open_work: {
    total_open: number
    waiting_on_you: HandoverIncident[]
    escalated: HandoverIncident[]
    stalled_without_diagnosis: HandoverIncident[]
    progressing: number
  }
  recurring_patterns: {
    pattern: string
    fix: string
    uses: number
    /**
     * Times this fix was cited and the failure came back.
     *
     * In the digest because at shift change "the obvious answer to this one has already
     * failed twice" is the single most valuable thing the incoming responder can be told
     * about a recurring pattern — and without it they reach for the top-ranked fix
     * precisely BECAUSE it recurs.
     */
    misses: number
    confidence: string
    trust: string
    /**
     * Matches the ledger's fast-path bar, computed by `ledger.entry_unlocks_fast_path`
     * itself rather than restated here. The digest used to restate "verified and high",
     * which went stale the moment the bar gained a use floor and a miss ceiling.
     */
    proven: boolean
    /**
     * Recorded evidence the fix did not hold — louder than "not proven", and a different
     * fact. An untested hypothesis is worth more than a refuted one, so these must not
     * render identically.
     */
    demoted: boolean
  }[]
  coverage: { watching: string[]; not_configured: string[]; any_watching: boolean }
  autonomy: { mode: string; rules: number; on_shift?: boolean | null }
  /** Pre-rendered text, so a Slack paste and this UI cannot word things differently. */
  text: string
}

export interface HandoverIncident {
  id: string
  title: string
  status: IncidentStatus
  blocked_reason: string
  severity: string
  source: string
  /**
   * What claimed it — `'heartbeat'`, `'operator'`, or `''` for anything claimed before
   * the field existed.
   *
   * Optional, and `''` must render as unrecorded rather than being defaulted to a path:
   * every incident already on disk lacks the key, and inventing "heartbeat" for them
   * would state a fact nobody recorded. At a shift change the distinction changes the
   * incoming reader's next step — "the agent picked this up" and "the outgoing responder
   * picked this up by hand" mean different things, and the second says a person already
   * judged it worth attention.
   */
  claimed_by?: string
  has_diagnosis: boolean
}

export interface BoardState {
  incidents: Incident[]
  counts: Record<string, number>
  /** Count of incidents waiting on a person, keyed by blocked_reason. */
  blocked?: Record<string, number>
  providers: ProviderInfo[]
  rotation: RotationInfo
  ledger: LedgerStats
  slack?: SlackOutStatus
  /**
   * Optional for the same reason `slack` is: `/state` is its only producer, so an older
   * gateway sends nothing and Settings must degrade to "no status yet" rather than to a
   * false "off" — which would invite an operator to enable something already enabled.
   */
  notify?: NotifyOutStatus
  /**
   * Optional because `/state` is its only producer: an older gateway will not send it,
   * and Settings must degrade to "no status yet" rather than to a false "off".
   */
  ledger_sync?: LedgerSyncStatus
  companions?: CompanionInfo[]
  /**
   * Inbound webhook signals delivered but not yet drained by a dispatch cycle.
   *
   * The spool is drained BY the poll (`WebhookSignalSource.poll` calls `drain`), which is
   * why this number needs its own display and cannot be inferred from anything on
   * `/signals`: polling to look at it is what empties it. Between a sender's 200 and the
   * next heartbeat, this is the only evidence a delivery landed.
   *
   * Bounded — see `WEBHOOK_QUEUE_LIMIT`. At the cap, deliveries are being dropped.
   */
  webhook_queue: number
}

/**
 * Size of the backend's inbound-webhook spool (`webhook.MAX_QUEUED_SIGNALS`).
 *
 * Mirrored rather than served, and that is a deliberate trade with a bound on how wrong it
 * can be: the queue is a `deque(maxlen=...)`, so at the cap the OLDEST delivery is silently
 * discarded to make room, and an operator seeing "200 queued" with no note would read a
 * healthy backlog. The number is only used to decide whether to add that note. If the
 * backend's cap grows, this under-warns (we say "full" slightly early) — never the reverse,
 * because `webhook_queue` can never exceed the real cap.
 */
export const WEBHOOK_QUEUE_LIMIT = 200

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(`${API}${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  })
  if (!resp.ok) {
    // Surface the backend's reason — a 403 here is usually the autonomy gate
    // explaining that no rule grants this action, which the user needs to read.
    let detail = `HTTP ${resp.status}`
    try {
      const body = (await resp.json()) as { error?: string }
      if (body?.error) detail = body.error
    } catch {
      /* non-JSON error body — keep the status text */
    }
    throw new Error(detail)
  }
  return (await resp.json()) as T
}

export const opsApi = {
  state: () => req<BoardState>('/state'),

  /**
   * The board's incident list. Server-capped at 200 — `truncated` and `total` are
   * present ONLY when there was more, so the UI can say "showing 200 of 640" instead
   * of implying the list is complete. Typed rather than ignored: a silently clipped
   * board is how someone concludes an incident vanished.
   *
   * Its caller is the Board's "Closed — postmortems" section. `/state` returns only OPEN
   * incidents, so a closed one leaves the board entirely and this is the only route that
   * can find it again — which is also why the section exists: without it, the artifact
   * written at close had nowhere to be read. The `status` filter takes ONE status while
   * "closed" is two (resolved and escalated), so that caller passes none and filters
   * client-side; the cap therefore applies across all statuses, which is why `truncated`
   * has to be rendered rather than assumed away.
   */
  incidents: (status?: IncidentStatus) =>
    req<{ incidents: Incident[]; truncated?: boolean; total?: number }>(
      // `URLSearchParams` rather than string concatenation: it encodes the value and
      // keeps this line free of a bare literal, which the i18n gate reads as
      // user-facing text even in a URL.
      `/incidents?${new URLSearchParams(status ? { status } : {}).toString()}`,
    ),

  /**
   * One incident plus its rendered postmortem.
   *
   * `log` is the Markdown artifact the backend writes when the incident reaches a terminal
   * status, redacted at the write (both the core credential/exfil pass and the app's
   * provider-token pass). It is the only thing this app produces that a reader who does
   * not run Kiro Crew can be handed, which is why it is worth a round trip.
   *
   * It is EMPTY in two different situations and the UI must not render them the same way:
   * an incident that is still open has no artifact yet, and one that closed before this
   * shipped never got one. Neither means "the investigation found nothing", so a blank
   * `<pre>` would be the most misleading possible rendering.
   *
   * `log_path` is where that file lives on disk, because handing a colleague an artifact
   * usually means handing over a FILE and not a clipboard. It is empty whenever no file
   * exists, and the UI must never synthesize it: `KIROCREW_HOME` moves the data directory,
   * so a guessed path would be this app asserting a file the backend does not have.
   */
  incident: (id: string) =>
    req<{ incident: Incident; log: string; log_path: string }>(
      `/incident?id=${encodeURIComponent(id)}`,
    ),

  /**
   * Move an incident to a new status.
   *
   * `slack_thread_replyable` says whether a reply typed into the incident's Slack thread
   * will actually reach the investigation. It is MUTATION-ONLY: the backend computes it
   * from `slack_out.link_thread_to_investigation` at transition time and no GET returns
   * it, so treat it as transient feedback and do not try to re-read it later.
   *
   * `Incident.slack_thread_ts` is NOT a substitute. Linking also requires Slack output to
   * be configured AND a live investigation slot to exist, so a non-empty ts with a missing
   * slot is exactly the false positive `test_slack_out.py` pins — it would promise the
   * operator their reply lands when it will not.
   */
  transition: (id: string, status: IncidentStatus, extra?: Record<string, string>) =>
    req<{ incident: Incident; slack_thread_replyable: boolean }>('/incident/transition', {
      method: 'POST',
      body: JSON.stringify({ id, status, ...extra }),
    }),

  /**
   * Hand-claim a signal off the Signals tab.
   *
   * Returns the same claim envelope `dispatch` does (routes.py `_handle_claim` spreads
   * `ClaimedIncident.to_dict()`), so `exact_match_ids` IS available here — and this is the
   * only read path that carries it, since the Board's payloads do not. See `dispatch` for
   * why it must not be re-derived from the incident.
   */
  claim: (signal: Signal) =>
    req<{
      incident: Incident
      matches: LedgerEntry[]
      similar: LedgerEntry[]
      exact_match_ids: string[]
      /** Provider context the gateway read on the agent's behalf — see `Evidence`. */
      evidence: Evidence[]
      fast_path: boolean
      /** Ready-to-use investigation brief, so the agent's first turn is not re-fetching. */
      brief: string
    }>('/incident/claim', {
      method: 'POST',
      body: JSON.stringify({ signal }),
    }),

  /**
   * Ask a provider sink to act on an incident, subject to the autonomy gate (a 403 here
   * is the gate explaining that no rule grants this action — surface its text).
   *
   * `duration_secs` applies to `silence` only, and the route CLAMPS it: unparseable,
   * missing or non-positive becomes a default window and anything over the ceiling is
   * reduced, because a suppression with no end is the one outcome that verb exists to
   * prevent. The echoed `duration_secs` is therefore the window ACTUALLY applied and may
   * be smaller than the one requested — read it back rather than displaying the request.
   * It is `null` for every non-expiring action.
   *
   * **`ok` means the provider returned 2xx and nothing more**, which is why the response
   * now also carries `verification`. Never present `ok: true` to an operator as "applied":
   * read `verification` and say which of the two happened —
   *
   * - `pending` with a `verify_after`: the request was accepted and the heartbeat will
   *   re-read the signal at that time. For a `silence` that time is the END of the
   *   window, because a suppression that expires straight back into the same firing
   *   condition is the strongest available evidence nothing was fixed.
   * - `not_checkable`: accepted, and this app cannot observe whether it took effect (an
   *   ack leaves an alert firing by design). Say "sent", not "confirmed".
   * - `''`: nothing was scheduled — either the call failed, or the bookkeeping write did.
   *   The provider write already happened and cannot be undone, so that degradation is
   *   deliberate rather than a 500.
   */
  action: (
    id: string,
    action: ActionKind,
    opts?: { sink?: string; note?: string; duration_secs?: number },
  ) =>
    req<{
      ok: boolean
      action: string
      detail: string
      error: string
      duration_secs: number | null
      verification: '' | 'pending' | 'not_checkable'
      verify_after: string
    }>('/incident/action', {
      method: 'POST',
      body: JSON.stringify({ id, action, ...opts }),
    }),

  /**
   * The pending-proposal queue: what the agent has DRAFTED and is waiting on a person for.
   *
   * In `propose` mode this is the only surface that shows the STORED terms. Approving from
   * the agent's chat paraphrase approves whatever is in the store, which is precisely what
   * the digest binding exists to prevent — so the panel reads the proposal from here and
   * echoes the `digest` it displayed back on the decision.
   */
  proposals: () => req<{ proposals: PendingProposal[]; total: number }>('/proposals'),

  /**
   * Approve or reject a drafted action.
   *
   * `digest` is REQUIRED for an approval and must be the one rendered to the operator: the
   * route refuses a mismatch (409) rather than executing terms nobody read. A rejection
   * carries no digest because it authorizes nothing.
   *
   * A 403 is the autonomy gate, exactly as on `action()` — surface its text.
   */
  decideProposal: (id: string, approve: boolean, digest?: string) =>
    req<{
      ok: boolean
      proposal: PendingProposal | null
      executed: boolean
      error?: string
      code?: string
      authorized?: boolean
    }>('/incident/proposal/decide', {
      method: 'POST',
      body: JSON.stringify({ id, approve, ...(digest ? { digest } : {}) }),
    }),

  /** Fresh each call: a cached handover goes stale between shifts. */
  handover: () => req<HandoverDigest>('/handover'),

  /**
   * Current provider state.
   *
   * `firing` is state-filtered the way the dispatch loop filters; `signals` is the raw
   * list and includes anything a provider reported as recovered, so prefer `firing`
   * when showing "what is wrong right now".
   *
   * `poll_health` says, per source, whether the LAST poll actually succeeded. It is not
   * decoration: a signal missing from `firing` means either "it cleared" or "we could
   * not look", and only `poll_health` distinguishes them.
   *
   * `suppressed` is the third bucket and the third reason a signal is absent from
   * `firing`: a human parked it at the provider. Do NOT resolve on it — nothing was
   * fixed — and do not fold it into `cleared`, which asserts recovery. It is also why a
   * per-source "firing" count must be derived from `firing` and never from the raw
   * `signals` array: a parked signal counted as firing produces the exact contradiction
   * of "3 firing" above an empty queue, with no explanation.
   *
   * `all_sources_healthy` is `bool(health) and all(ok)`, so it is FALSE on a fresh install
   * with nothing configured — zero sources means an empty health map. A UI must branch on
   * three cases (nothing configured / healthy / a source is failing), never two, or it
   * tells a brand-new user that something is broken.
   */
  signals: () =>
    req<SignalsResult>('/signals'),

  providers: () => req<{ providers: ProviderInfo[] }>('/providers'),

  /**
   * Run one dispatch cycle: poll, claim, match the ledger, release stale work.
   *
   * `matches` and `similar` are deliberately separate and must stay that way in any UI
   * that renders them. A `matches` entry means this exact failure fingerprint recurred;
   * a `similar` entry is a semantic near-miss whose fingerprint does NOT match. Showing
   * them together would let a near-miss inherit the "verified, used 4x" authority it has
   * not earned — the backend keeps them apart for the same reason (see
   * `dispatch.attach_similar_lessons`).
   *
   * `exact_match_ids` records which of THIS lookup's `matches` were decided by the
   * provider's own identity rather than by our shape hash — a distinction that matters
   * because the shape hash provably over-merges (with bare digits stripped, a 4xx and a 5xx
   * alarm on one resource hash identically), so a shape match can hand a responder a fix
   * learned from a different failure. It is a property of the lookup, not of the stored
   * entry, which is why the backend keeps it here and NOT on `Incident`.
   *
   * Consequently it is absent from `/state`, `/incidents` and `/incident` — the payloads the
   * Board reads — and must NOT be re-derived there. `dispatch.attach_ledger_matches`
   * captures it BEFORE `ledger.record_use` binds the provider key to the entry, so a
   * client-side `provider_key in entry.provider_keys` check would report "exact" for every
   * shape match from the second occurrence onward.
   */
  dispatch: () =>
    req<{
      claimed: {
        incident: Incident
        matches: LedgerEntry[]
        similar: LedgerEntry[]
        exact_match_ids: string[]
        /** Provider context the gateway read on the agent's behalf — see `Evidence`. */
        evidence: Evidence[]
        fast_path: boolean
      }[]
      released: string[]
      polled: number
      unclaimed_remaining: number
      errors: Record<string, string>
      changed: boolean
      skipped_reason: string
      briefs: Record<string, string>
      /**
       * How many signals this cycle saw parked at the provider and deliberately did not
       * claim. Deliberately NOT reflected in `changed` — a suppression is the least
       * newsworthy thing a cycle can find, and the heartbeat stays silent on it.
       *
       * `polled` counts firing signals only, so without this number a cycle's report
       * describes a smaller world than the cycle actually saw: "Polled 0 firing signal(s)"
       * reads as a quiet estate even when three alarms are parked.
       */
      suppressed: number
      /**
       * Post-action verdicts this cycle REACHED, `{incident_id: verdict}`.
       *
       * Only incidents whose recheck came due appear, so `{}` is the normal case — and it
       * means "nothing was due", NOT "every action worked". A UI must not summarise this
       * map as a success rate.
       *
       * Only `still_firing` feeds `changed`: the app discovering that a claim it made was
       * untrue is the most newsworthy thing a cycle can find, while announcing `cleared`
       * would make the heartbeat congratulate itself and `unknown` is a non-finding a
       * later cycle retries.
       */
      verifications: Record<
        string,
        'pending' | 'cleared' | 'still_firing' | 'unknown' | 'not_checkable'
      >
    }>('/dispatch', { method: 'POST' }),

  /** Non-secret provider config. Secrets are refused here by the backend. */
  putProviderConfig: (providerId: string, updates: Record<string, unknown>) =>
    req<{ ok: boolean; provider: string; config: Record<string, unknown> }>(
      `/providers/${encodeURIComponent(providerId)}/config`,
      { method: 'PUT', body: JSON.stringify(updates) },
    ),

  /**
   * App-level settings: autonomy mode, primary flag, cycle tuning.
   *
   * `notify_enabled` turns local desktop notifications on. There is nothing else to set
   * for that channel — no destination and no credential — which is the whole reason it
   * exists beside the Slack one. Per-channel muting is NOT here: Kiro Crew owns that
   * centrally at Settings → Notifications, and duplicating it would give an operator two
   * controls that can disagree.
   *
   * Also the ONLY way to point this instance at the team's shared-memory repo:
   * `ledger_sync_enabled`, `ledger_sync_remote` and `ledger_sync_branch`. The backend
   * length-caps the remote and refuses a branch that is not a plain ref.
   *
   * A remote URL is NOT a credential — auth is the operator's own SSH key, git credential
   * helper or `gh` login — which is why it may live in `config.json` at all. That file is
   * served UNAUTHENTICATED, so the flip side holds too: a URL with an embedded token would
   * be a secret written to a public file, and nothing on the write path strips one. The
   * panel must therefore never invite a token-bearing URL, and must not display one back.
   */
  putSettings: (updates: Record<string, unknown>) =>
    req<{ ok: boolean; applied: Record<string, unknown> }>('/settings', {
      method: 'PUT',
      body: JSON.stringify(updates),
    }),

  putSecret: (providerId: string, field: string, value: string) =>
    req<{ ok: boolean }>(`/providers/${encodeURIComponent(providerId)}/secret`, {
      method: 'PUT',
      body: JSON.stringify({ field, value }),
    }),

  deleteSecret: (providerId: string) =>
    req<{ ok: boolean; removed: boolean }>(
      `/providers/${encodeURIComponent(providerId)}/secret`,
      { method: 'DELETE' },
    ),

  rotation: () => req<RotationInfo>('/rotation'),

  ledger: () => req<{ entries: LedgerEntry[]; stats: LedgerStats }>('/ledger'),

  /**
   * The nightly maintenance pass: pull → hygiene → index → prune closed → push.
   *
   * AGENT-FACING. Its caller is the `ledger-hygiene` cron (via the SOP's curl), not a
   * button, and there is deliberately none here: the pass rewrites the shared ledger and
   * pushes to the team's repo, so it belongs to the one instance that owns it (see
   * `RotationRoster.leader`) rather than to whoever has Settings open. An operator-triggered
   * copy would be a second writer against the invariant that exactly one instance prunes.
   *
   * The full response is typed even though nothing reads it, because the previous
   * declaration listed 2 of its 5 keys — and an understated type is how a caller concludes
   * a stage does not report anything. `sync` carries the git outcome (empty strings when
   * sync is unconfigured, the common single-user case), `index` the vector-store counts, and
   * `incidents_pruned` the closed incidents retired. `changed` is wider than `summary`: it
   * is also true for a pull that brought in a teammate's lesson, which changes what the
   * agent knows tomorrow without changing any local count.
   */
  ledgerHygiene: () =>
    req<{
      summary: Record<string, number>
      sync: { pull: string; push: string }
      index: { scanned: number; written: number; skipped: number; embedded: number }
      incidents_pruned: number
      changed: boolean
    }>('/ledger/hygiene', { method: 'POST' }),

  /**
   * Entry pairs claiming DIFFERENT fixes for the same fingerprint.
   *
   * Also agent-facing — the hygiene SOP is told to resolve contradictions and this is how it
   * finds them deterministically instead of by eye. Declared rather than left off because
   * omission from this file is what makes a route invisible to the next reader; not rendered
   * because resolving a contradiction means splitting two patterns so each names its own
   * cause, which is a judgement, and a panel that only listed pairs would hand the operator
   * a problem with no control to fix it.
   */
  ledgerContradictions: () =>
    req<{
      contradictions: {
        fingerprint: string
        /** Exactly two — the pair that disagrees. `uses` is their combined use count. */
        entries: LedgerEntry[]
        uses: number
      }[]
      count: number
    }>('/ledger/contradictions'),

  addLedgerEntry: (entry: {
    pattern: string
    fix: string
    fingerprints?: string[]
    confidence?: string
    trust?: string
  }) =>
    req<{ entry: LedgerEntry }>('/ledger', {
      method: 'POST',
      body: JSON.stringify(entry),
    }),

  removeLedgerEntry: (id: string) =>
    req<{ ok: boolean; removed: boolean }>(`/ledger?id=${encodeURIComponent(id)}`, {
      method: 'DELETE',
    }),
}
