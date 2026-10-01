/**
 * Automation triggers: agent lifecycle hooks (including kiro hooks) and
 * inbound webhooks with their tokens, contexts, test delivery and on/off
 * switch.
 */

import { relocateRequestUrl } from '../../lib/dashboardRuntime'
import type { ClientTransport } from './transport'

/* ── Inbound webhooks (GET /api/webhooks) ──
 * Shapes mirror the pinned backend contract. Both one-time secrets — the bearer
 * token and the HMAC signing secret — only ever appear in
 * `WebhookTokenCreated`, from the create call; `GET /api/webhooks` never echoes
 * either one. */

export type WebhookFreshness = 'fresh' | 'stale' | 'expired'

export type WebhookOutcome =
  | 'completed' | 'timeout' | 'error' | 'rejected_capacity' | 'unauthorized' | 'disabled'

export interface WebhookTokenEntry {
  id: string
  label: string
  /** Leading, non-secret slice of the raw token, e.g. `kc_whk_4f2b`. */
  display_prefix: string
  last4: string
  created_at: number
  /** null / 0 until the token authorizes its first call. */
  last_used_at: number | null
  /** True when a caller using this token must also send a timestamp + HMAC
   *  signature of the raw body. The signing secret itself is never in this
   *  payload — it is returned once, from the create call. Legacy config tokens
   *  have no signing secret, so they report false. */
  require_signature: boolean
  /** True for the legacy `hooks.webhook_token` config scalar, which cannot be
   *  deleted from the dashboard. */
  legacy: boolean
  /** Operator-owned destination. Empty only for legacy or pre-routing rows. */
  agent: string
  /** Per-source admission switch; absent historical rows normalize to true. */
  enabled: boolean
}

export interface WebhookContextEntry {
  hook_id: string
  session_key: string
  registered_at: number
  age_seconds: number
  freshness: WebhookFreshness
  context_summary: string
  context_chars: number
}

export interface WebhookRunRecord {
  id: string
  /** null for a 401 — the caller is unknown at that point. */
  hook_id: string | null
  session_key: string
  name?: string
  outcome: WebhookOutcome
  started_at: number
  duration_ms: number
  result_chars: number
  token_id: string | null
  delivered: boolean
  detail?: string
}

export interface WebhooksView {
  /** Effective state: `has_tokens && switch_on`. */
  enabled: boolean
  /** The kill switch on its own. False ⇒ every inbound call is answered
   *  with 503 before any auth work, while tokens and history are kept. */
  switch_on: boolean
  /** True when at least one token exists (stored or legacy). */
  has_tokens: boolean
  url: string
  slots: { in_use: number; max: number }
  limits: {
    session_key_prefix: string
    message_max: number
    timeout_default: number
    timeout_max: number
    max_concurrent: number
    /** Raw request-body cap in bytes. Optional: a server predating the cap
     *  omits it, and the page falls back rather than rendering `undefined`. */
    body_max_bytes?: number
    /** Accepted clock skew, in seconds, for a signed request's timestamp. */
    signature_window_seconds: number
  }
  tokens: WebhookTokenEntry[]
  contexts: WebhookContextEntry[]
  runs: WebhookRunRecord[]
}

export interface WebhookTokenCreated {
  ok: boolean
  /** The raw secret — returned exactly once and unrecoverable afterwards. */
  token: string
  /** The HMAC signing secret — also returned exactly once. Absent when the
   *  token was minted bearer-only (`require_signature: false`). */
  signing_secret?: string
  entry: WebhookTokenEntry
}

export interface WebhookTestResult {
  ok: boolean
  status: number
  session_key?: string
  error?: string
}

export function createHooksEndpoints({ post, put, del, j }: ClientTransport) {
  const triggers = {
    // Hooks
    hooks: () => fetch(relocateRequestUrl('/api/hooks')).then(j),
    kiroHooks: () => fetch(relocateRequestUrl('/api/kiro-hooks')).then(j),
    createHook: (body: object) => post('/api/hooks', body).then(j),
    updateHook: (id: string, body: object) => put('/api/hooks/' + id, body).then(j),
    deleteHook: (id: string) => del('/api/hooks/' + id).then(j),
    toggleHook: (id: string) => post('/api/hooks/' + id + '/toggle', {}).then(j),
    testHook: (id: string, context?: string) => post('/api/hooks/' + id + '/test', { context: context || 'test' }).then(j),
    // Inbound webhooks (POST /api/hooks/agent) — token store, registered
    // contexts, run history. All dashboard-authed; the webhook bearer token is
    // never used from the browser.
    webhooks: () => fetch(relocateRequestUrl('/api/webhooks')).then(j),
    // `require_signature` defaults to true server-side; a destination is required
    // for every newly created first-class source.
    createWebhookToken: (label: string, requireSignature = true, agent = '') =>
      post('/api/webhooks/tokens', {
        label,
        require_signature: requireSignature,
        agent,
      }).then(j),
    updateWebhookToken: (
      id: string,
      patch: { agent?: string; enabled?: boolean; label?: string },
    ) => fetch(relocateRequestUrl('/api/webhooks/tokens/' + encodeURIComponent(id)), {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    }).then(j),
    deleteWebhookToken: (id: string) => del('/api/webhooks/tokens/' + encodeURIComponent(id)).then(j),
    deleteWebhookContext: (hookId: string) => del('/api/webhooks/contexts/' + encodeURIComponent(hookId)).then(j),
    testWebhook: (message?: string, agent?: string) => post('/api/webhooks/test', { message, agent }).then(j),
    setWebhooksEnabled: (enabled: boolean) => post('/api/webhooks/switch', { enabled }).then(j),
  }

  return { triggers }
}
