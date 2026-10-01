import type { CronJob } from '../../types'

/** Private schedules use durable member_id. Legacy V1 schedules retain their
 * existing display attribution; showing one here never grants V2 memory.
 *
 * TWO identities, and they are not interchangeable. `memberId` is the crew's
 * immutable id — the slug the server allocated with its member memory — and it is
 * what a private schedule's `member_id` holds: the client's value is rewritten to
 * the canonical id before the record is persisted (`bind_cron_memory` →
 * `derive_execution`, whose `member_id` is `validate_slug`-constrained). `crew` is
 * the mutable DISPLAY name, and it is what the legacy branches below compare,
 * because `agent` and `agent_sequence` hold template and crew NAMES.
 *
 * Passing the name for both is the bug this signature exists to prevent: for any
 * crewmate whose name is not already its own slug ("Radar" → `radar`), every
 * private schedule fails the first comparison and the crewmate reads as having
 * none — including a job just created from the surface doing the asking. */
export function wakesCrew(
  job: CronJob,
  crew: string,
  isDefaultCrew: boolean,
  memberId: string,
): boolean {
  if (job.script || job.command) return false
  if (job.member_id) return job.member_id === memberId
  const seq = (job.agent_sequence || []).map(a => (a || '').trim()).filter(Boolean)
  if (seq.length > 1) return seq.includes(crew)
  const bound = (job.agent || '').trim()
  return bound ? bound === crew : isDefaultCrew
}

/** Query key shared by the wake pane and the rail's count, so one fetch serves
 *  both instead of the rail issuing a second identical request. */
export const crewWakeQueryKey = (crew: string) => ['crons', 'crew-wake', crew]

/** The crew editor's own entry under the `webhooks` prefix. Deliberately NOT
 *  the page's bare `['webhooks']`: the two have different queryFns — the page
 *  substitutes an empty view on failure so an old gateway renders as
 *  unconfigured, while the editor must THROW so a failure renders as unknown
 *  rather than "nothing wakes this crew" — and sharing one key would let
 *  whichever mounts first decide the other's shape. Mint/revoke on the page
 *  still reaches this cache, because invalidation matches keys by prefix. */
export const crewWebhooksQueryKey = ['webhooks', 'crew-editor']

/** A webhook token shape sufficient for the two predicates below. Structural
 *  rather than the api client's entry type, so this module stays type-only
 *  independent of the client. */
interface WebhookTokenLike {
  agent?: string
  enabled?: boolean
}

/** Whether `token` is bound to `crew`. Shared for the same reason wakesCrew is:
 *  the rail badge and the webhook pane both answer "whose token is this", and
 *  two spellings of the predicate would drift into disagreeing. */
export function webhookBoundToCrew(token: WebhookTokenLike, crew: string): boolean {
  return (token.agent || '') === crew && crew !== ''
}

/** Whether `token` can actually start a turn right now. Two switches silence a
 *  token the same way — its own admission switch and the store-wide kill
 *  switch — and every "live" claim (rail count, row dimming, the any-crew
 *  disclosure) must hold both, or the surfaces drift into contradicting each
 *  other about a security-relevant fact. */
export function webhookCanCallIn(token: WebhookTokenLike, switchOn: boolean): boolean {
  return switchOn && token.enabled !== false
}
