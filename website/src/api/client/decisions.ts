/**
 * The decision seam (/api/decisions): the consent read, the scope-only and
 * history-budget writes, and per-decision verdict feedback.
 *
 * `saveDecisionsConsent` is defined in `api/client.ts`, which decisions.md
 * names as its home.
 */

import type { ClientTransport } from './transport'

/** Decision-seam consent as returned by GET/PUT /api/decisions/consent.
 *  `enabled` is the keystone; `endpoint` is the address it was given for;
 *  `configured_endpoint` is where config.json points now; `permits` is whether a
 *  decision would actually be sent (both true and equal). */
export interface DecisionsConsentData {
  enabled: boolean
  endpoint: string
  configured_endpoint: string
  permits: boolean
  /**
   * Whether the owner consented to sending TOOL-CALL ARGUMENTS — the extra egress
   * category `tool.risk` needs. Absent on a gateway older than the scope, which
   * reads as not consented; the keystone's own absent value reads the same way, so a
   * consent recorded before this existed authorizes only what its owner reviewed.
   */
  tool_args?: boolean
  /**
   * Whether the owner consented to sending a WHOLE SLOT TRANSCRIPT — the conversation
   * text and every tool-call input in it — which is what `compaction.keep` scores.
   * Absent on a gateway older than the scope and reads as not consented, the same way
   * the keystone's own absent value does, so neither of the narrower yeses above is
   * ever read as this one.
   */
  compaction?: boolean
  /**
   * Whether the owner consented to sending THE TEXT OF RECALLED MEMORIES — the extra
   * egress category `memory.recall` needs. Absent reads as not consented, on the same
   * terms as `tool_args`: a recalled memory is text the agent wrote down in an earlier
   * conversation, so consent recorded against a message excerpt cannot stand for it.
   */
  memory_text?: boolean
  /**
   * Whether the owner consented to sending WAKE EVIDENCE — the transcript tail and
   * pull-request readings the `nudge.wake` judge screens a tick against. Absent reads
   * as not consented, on the same terms as the three above: this evidence comes from
   * sessions the loop WATCHES rather than the one the owner is talking in, so none of
   * the narrower yeses stands for it.
   */
  nudge_evidence?: boolean
  /**
   * One row per decision point this GATEWAY ships, projected from the seam's own
   * registry (`decisions/gate.py`). The card lists these rather than an array
   * written here, so a build that ships another point lights up a row with no
   * frontend edit — the same arrangement the Agent Backend panel uses for its
   * capability lines.
   *
   * Absent on a gateway older than the projection, which reads as no rows: the
   * overview then says the points cannot be listed rather than inventing a list.
   */
  points?: DecisionPointData[]
}

/** One decision point as the gateway reports it. */
export interface DecisionPointData {
  /** The gateway's identifier, e.g. `skills.select`. Also the decision log's. */
  id: string
  /**
   * The keystone scope this point needs on top of consent itself (`tool_args`), or
   * null when consent alone is enough. The point's own panel draws its switch from
   * this, so a new scope needs no per-point branch here.
   */
  needs_scope: string | null
  /**
   * The EFFECTIVE answer, not a switch position: `active`, `needs_scope` (consent
   * stands but this point's egress category was never granted), or `off` (nothing
   * is sent at all — no consent, a moved endpoint, or a governance pin). Computed
   * server-side so the row and the gate cannot disagree.
   */
  status: string
}

/** Which side of a logged decision a reader's verdict is about. */
export type DecisionFeedbackSide = 'jev' | 'baseline'

/** A reader's verdict on one side. `null` retracts an earlier one. */
export type DecisionVerdictValue = 'right' | 'wrong' | null

/** One local System One model the card offers (`decisions/local_models.py`). */
export interface DecisionsLocalModel {
  id: string
  name: string
  model: string
  default_port: number
  /** Correct answers matched, as a percentage of Jev's, over all public items. */
  jev_relative_pct: number
  /** The same ratio on the hard tier alone. */
  hard_relative_pct: number
  peak_ram_gb: number
  /** Total machine memory at or above which the card recommends this preset. */
  recommended_total_ram_gb: number
  p50_secs: number
  p95_secs: number
  timeout_ms: number
  /** Bytes of weights the gateway downloads the first time the preset is chosen. */
  download_bytes: number
  /** Weights and environment are already on this machine. Absent on an older gateway. */
  installed?: boolean
}

/** What the gateway is doing with the local model it runs (`decisions/local_runtime.py`). */
export type DecisionsRuntimeState = 'idle' | 'downloading' | 'installing' | 'starting' | 'running' | 'error'

export interface DecisionsRuntimeStatus {
  /** The preset being run, or `""` when none is. */
  preset: string
  state: DecisionsRuntimeState
  port: number
  bytes_done: number
  bytes_total: number
  /** Why it stopped, with the tail of the server log; set while `state` is `error`. */
  error: string
}

export interface DecisionsLocalRuntimeData {
  runtime: DecisionsRuntimeStatus
  /** Ids of the presets whose weights and environment are on this machine. */
  installed: string[]
}

export interface DecisionsProviderData {
  presets: DecisionsLocalModel[]
  /** `jev`, `none` (no decision model), a preset id, or `custom` for an address set by hand in config.json. */
  active: string
  configured_endpoint: string
  /** The configured address is a literal loopback one, so no Jev key is sent to it. */
  loopback?: boolean
  /** Fleet policy allows hosted Jev (`capabilities.decisions`). Absent on an older gateway. */
  hosted_permitted?: boolean
  /** Fleet policy allows a local preset (`capabilities.decisions_local`). Absent on an older gateway. */
  local_permitted?: boolean
  /** The gateway-run local server. Absent on an older gateway. */
  runtime?: DecisionsRuntimeStatus
}

export function createDecisionsEndpoints({ get, post, put, del, j }: ClientTransport) {
  const consentRead = {
    // Decision-seam consent (Settings > Developer > Feature Previews). The switch
    // is a KEYSTONE, not a config path: see decisionsPreview.ts. The PUT returns
    // the state written so the card re-renders from server truth.
    getDecisionsConsent: () => get('/api/decisions/consent').then(j) as Promise<DecisionsConsentData>,
    // Which System One server the seam asks, and the local presets on offer.
    getDecisionsProvider: () => get('/api/decisions/provider').then(j) as Promise<DecisionsProviderData>,
    // What the gateway is doing with its local model. Polled while a model is being
    // prepared: unlike the provider read it writes no audit row and asks no governance.
    getDecisionsLocalRuntime: () =>
      get('/api/decisions/local-models/status').then(j) as Promise<DecisionsLocalRuntimeData>,
  }

  const scopesAndFeedback = {
    // A SCOPE on its own, with `enabled` deliberately OMITTED and no endpoint echo.
    // A per-point scope switch is not a review of an address, so it must not restate
    // consent to one; the gateway preserves the recorded switch and endpoint for an
    // absent `enabled`, and refuses the write outright (409, or 403 under a
    // governance pin) unless consent is already in force for the address config
    // names. So the omission can only ever move a scope under a consent that
    // already stands.
    saveDecisionsScope: (scope: string, value: boolean) =>
      put('/api/decisions/consent', { [scope]: value }).then(j) as Promise<DecisionsConsentData>,
    // The prior-conversation CEILING, on the keystone beside the switch it belongs to
    // and NOT through the config route. What it bounds is how much of the conversation
    // leaves the machine, so it is consent, and consent does not live in an
    // agent-writable file: config.json carries what the seam ASKS for and this number
    // is the ceiling that request is clamped to. `enabled` is omitted for the reason a
    // scope omits it -- raising a ceiling is not a review of an address -- so the
    // gateway preserves the recorded switch and endpoint and refuses the write unless
    // consent already stands for the address config names.
    saveDecisionsHistoryBudget: (chars: number) =>
      put('/api/decisions/consent', { history_budget_chars: chars }).then(j) as Promise<DecisionsConsentData>,
    // One reader's verdict on one side of one decision, from the transcript's
    // decision strip. `verdict: null` takes an answer back, which is why the field
    // is nullable rather than absent — the server records the retraction.
    sendDecisionsFeedback: (turnId: string, verdict: DecisionVerdictValue, side: DecisionFeedbackSide) =>
      post('/api/decisions/feedback', { turn_id: turnId, verdict, side }).then(j) as Promise<unknown>,
    // Switch the provider to hosted Jev or a local preset. A preset id only, never a
    // URL or a port: the gateway picks the port and builds the address itself, which
    // is what keeps the dashboard from choosing an arbitrary destination for decision
    // state. A local preset is then downloaded, installed and started by the gateway.
    saveDecisionsProvider: (preset: string) =>
      put('/api/decisions/provider', { preset }).then(j) as Promise<DecisionsProviderData>,
    // Delete a downloaded preset that is not in use, to give its disk space back.
    removeDecisionsLocalModel: (id: string) =>
      del(`/api/decisions/local-models/${encodeURIComponent(id)}`).then(j) as Promise<DecisionsProviderData>,
  }

  return { consentRead, scopesAndFeedback }
}
