import type { CronJob } from '../../types'

/**
 * Whether `job` wakes `crew`, in the order the backend resolves it.
 *
 * 1. A script or command job opens no session, so it runs as NO crew — whatever
 *    `agent` it happens to carry. Checked first, so a stale `agent_id` on such a
 *    job cannot list it under a crew it never wakes.
 * 2. A sequence of MORE THAN ONE agent takes precedence over `agent_id` at run
 *    time (`len(agents) > 1` in the gateway's dispatch), so such a job belongs to
 *    the crews it names and to no others — in particular, an empty `agent_id` on
 *    one must NOT read as "the default crew". A one-element sequence does NOT
 *    take precedence, so it falls through to `agent_id` like any other job.
 * 3. Otherwise the bound `agent`, and an empty one means the default crew.
 *
 * Shared rather than private to the wake pane: the rail also counts these jobs,
 * and two copies of this precedence would drift into disagreeing about which
 * crew a sequence job belongs to.
 */
export function wakesCrew(job: CronJob, crew: string, isDefaultCrew: boolean): boolean {
  if (job.script || job.command) return false
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
