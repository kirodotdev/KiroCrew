// useDirectDeploy — the two-call confirm flow that actually deploys a webapp
// artifact, shared by the Artifact Deploy page's "Ready to deploy" row and the
// webapp artifact card (issue #12816).
//
// Before this, both of those buttons set `window.__mc_chat_launch` and navigated
// to /chat. Neither deployed. A human standing on the confirm surface, already
// cookie-authenticated, was sent to a chat session — and the only button in the
// product that executes a deploy lives on a card that renders nothing when the
// pending list is empty, so the chat sent them back to the button they started
// from.
//
// The flow mirrors what PublishHub already does for html/widget artifacts:
//
//   1. POST /api/deploy/deploy with no `confirm` — a preview. The backend
//      resolves the app's built static root, scans it, and answers
//      `requires_confirm` plus the content digest and resolved identity.
//   2. PublicPublishAckModal — the blocking public-by-link acknowledgment. This
//      is the last thing between a human and a world-readable URL, so it is
//      never skipped and never pre-focused.
//   3. The same POST with `confirm: true`, binding the previewed digest and
//      identity so a concurrent change is refused (409 stale_preview) rather
//      than silently publishing something else.
//
// Three refusals get their own affordance instead of a dead red banner:
//
//   • `webapp_root_unavailable` (400) — the app has no built static root, so
//     there is nothing this flow can publish. The caller falls back to the chat
//     hand-off, labelled "Deploy via agent" so the button is honest about who
//     does the work.
//   • `reaper_required` (409) — a finite TTL needs auto-cleanup infrastructure
//     the account does not have. Offers deploying as permanent (which needs no
//     infrastructure) or copying the exact install command.
//   • scan-blocked (409) — overridable findings get the explicit "Deploy anyway"
//     the backend requires; credential findings can never be overridden.
//
// The artifact's own metadata is back-filled SERVER-side after a successful
// deploy, so this hook only invalidates the queries that read it.
import { useCallback, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { i18nT } from '../i18n/t'

const BASE = '/api/deploy'
const _sk = { 'X-Session-Key': 'dashboard:ui' }

/** TTL choices offered on the direct path, in the order they are shown.
 *
 *  `0` leads deliberately. It is the only value that needs no auto-cleanup
 *  infrastructure, so it is the one choice that cannot fail on a fresh account —
 *  defaulting to 72 is what put every first deploy into the reaper 409. */
export const TTL_CHOICES = [0, 24, 72] as const

/** When to re-read `deploy-web/sites` after a successful deploy.
 *
 *  Resource Groups Tagging is eventually consistent and the dashboard's
 *  deployment list is a live tag read, so the site is invisible to it for a
 *  minute or two. These offsets span that window without polling tightly. */
export const SITES_REFETCH_DELAYS_MS = [5_000, 20_000, 45_000, 90_000, 120_000] as const

/** The row shape `deploy-web/sites` holds, mirrored here so a successful deploy
 *  can seed one before the live read can see it. */
export interface Site {
  site_id: string
  url?: string
  distribution_id?: string
  profile?: string
  status?: string
  bucket?: string
}

export interface DeployPreview {
  content_digest: string
  profile: string
  region: string
  bytes: number
  scan: string
  site_id: string
}

/** A refusal the UI answers with buttons rather than just red text. */
export interface DeployRefusal {
  /** Machine discriminator: `reaper_required`, `webapp_root_unavailable`, … */
  code: string
  /** The plain sentence for the banner. */
  error: string
  /** Stack/field names for the Details toggle. Absent on older gateways. */
  details?: string
  /** A runnable command, when the refusal has one. */
  remediation?: string
}

export interface ScanBlock {
  findings: string
  count: number
  credential: boolean
}

export type DeployPhase =
  | { kind: 'idle' }
  | { kind: 'checking' }
  | { kind: 'ack'; preview: DeployPreview; overrideScan: boolean }
  | { kind: 'deploying' }
  | { kind: 'scan-blocked'; block: ScanBlock; preview: DeployPreview | null }
  // `preview` rides along so a retry at a different TTL can re-acknowledge the
  // SAME previewed content instead of confirming blind: the exposure window is
  // part of what the human acknowledged, so changing it requires a fresh
  // acknowledgment, and dropping the bindings would let content that changed
  // since the preview be published under the old consent.
  | { kind: 'refused'; refusal: DeployRefusal; preview: DeployPreview | null }
  | { kind: 'failed'; message: string }
  | { kind: 'done'; url: string }

interface Body { [k: string]: unknown }

async function callDeploy(body: Body): Promise<{ status: number; data: Record<string, unknown> }> {
  const r = await fetch(BASE + '/deploy', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ..._sk },
    body: JSON.stringify(body),
  })
  let data: Record<string, unknown> = {}
  try {
    data = (await r.json()) as Record<string, unknown>
  } catch {
    // A body-less or non-JSON failure still has to become a reportable phase
    // rather than throwing past the caller's error handling.
    data = {}
  }
  return { status: r.status, data }
}

function str(v: unknown): string {
  return typeof v === 'string' ? v : ''
}

/** Read a refusal the UI has a dedicated affordance for, else null. */
function readRefusal(data: Record<string, unknown>): DeployRefusal | null {
  const code = str(data.code)
  if (code !== 'reaper_required' && code !== 'webapp_root_unavailable') return null
  return {
    code,
    error: str(data.error),
    ...(str(data.details) ? { details: str(data.details) } : {}),
    ...(str(data.remediation) ? { remediation: str(data.remediation) } : {}),
  }
}

export function useDirectDeploy(slug: string) {
  const qc = useQueryClient()
  const [phase, setPhase] = useState<DeployPhase>({ kind: 'idle' })
  const [ttlHours, setTtlHours] = useState<number>(0)
  // A confirmed deploy must happen AT MOST ONCE per acknowledgment. `phase`
  // alone cannot enforce it: the acknowledgment lives inside an
  // <AnimatePresence>, which keeps rendering the exiting subtree — an enabled,
  // still hit-testable confirm button — for the exit duration. A ref is the
  // latch because it is written synchronously, so it is already set for a second
  // click dispatched in the same render generation. Same reasoning as
  // PublishHub.confirmPublish.
  const inFlight = useRef(false)

  /** Settle the queries that read a deployment, and bridge the window where AWS
   *  has the site but the dashboard cannot see it yet.
   *
   *  Resource Groups Tagging is eventually consistent, so the live tag read
   *  behind `deploy-web/sites` returns nothing for a minute or two after a
   *  successful deploy. A single refetch therefore lands inside that window and
   *  renders "Deployments (0)" beside a URL the user can already open.
   *
   *  Two things close it: the returned site is written straight into the sites
   *  cache with status `deploying`, so the row exists immediately; and the query
   *  is refetched on a schedule across ~2 minutes, so the optimistic row is
   *  replaced by the authoritative read as soon as tags catch up. The artifact
   *  queries are invalidated too, because the server back-fills the artifact's
   *  own metadata and the card has to re-read it. */
  const settleAfterDeploy = useCallback((result: Record<string, unknown>) => {
    const siteId = str(result.site_id) || slug
    const url = str(result.url) || str(result.public_url)
    if (siteId) {
      qc.setQueryData(
        ['deploy-web', 'sites'],
        (prev: { sites?: Site[]; configured?: boolean } | undefined) => {
          const sites = prev?.sites ?? []
          // Never duplicate a row the live read already returned.
          if (sites.some((s) => s.site_id === siteId)) return prev
          const optimistic: Site = {
            site_id: siteId,
            url,
            distribution_id: str(result.distribution_id),
            profile: str(result.profile),
            // `deploying` rather than `live`: CloudFront is still spreading, and
            // claiming otherwise would promise a link that may 404 for minutes.
            status: 'deploying',
          }
          return { ...(prev ?? { configured: true }), sites: [...sites, optimistic] }
        },
      )
    }
    qc.invalidateQueries({ queryKey: ['deploy-web', 'webapps'] })
    qc.invalidateQueries({ queryKey: ['deploy-web', 'pending'] })
    qc.invalidateQueries({ queryKey: ['artifact', slug] })
    // Spread across the consistency window rather than hammering it. A refetch
    // on an unmounted query is a no-op in react-query, so an early navigation
    // costs at most a discarded response.
    for (const delay of SITES_REFETCH_DELAYS_MS) {
      setTimeout(() => {
        qc.invalidateQueries({ queryKey: ['deploy-web', 'sites'] })
      }, delay)
    }
  }, [qc, slug])

  /** Step 1: preview. Never publishes. */
  const start = useCallback(async (profile: string) => {
    setPhase({ kind: 'checking' })
    try {
      const { status, data } = await callDeploy({
        site_id: slug, artifact_slug: slug, profile, ttl_hours: ttlHours,
      })
      const refusal = readRefusal(data)
      if (refusal) {
        setPhase({ kind: 'refused', refusal, preview: null })
        return
      }
      if (status === 409 && data.blocked && data.reason === 'scan') {
        setPhase({
          kind: 'scan-blocked',
          block: {
            findings: str(data.findings),
            count: typeof data.count === 'number' ? data.count : 0,
            credential: data.credential === true,
          },
          // The scan-block 409 carries the preview bindings too, so an
          // override-confirm stays pinned to the content that was scanned.
          preview: str(data.content_digest)
            ? {
              content_digest: str(data.content_digest), profile: str(data.profile),
              region: str(data.region), bytes: 0, scan: str(data.findings), site_id: slug,
            }
            : null,
        })
        return
      }
      if (data.requires_confirm === true) {
        setPhase({
          kind: 'ack',
          overrideScan: false,
          preview: {
            content_digest: str(data.content_digest),
            profile: str(data.profile),
            region: str(data.region),
            bytes: typeof data.bytes === 'number' ? data.bytes : 0,
            scan: str(data.scan),
            site_id: str(data.site_id) || slug,
          },
        })
        return
      }
      setPhase({ kind: 'failed', message: str(data.error) || i18nT('components.directDeploy.unexpected_response') })
    } catch (e: unknown) {
      setPhase({ kind: 'failed', message: e instanceof Error ? e.message : i18nT('components.directDeploy.deploy_failed') })
    }
  }, [slug, ttlHours])

  /** Step 3: the acknowledged deploy. `ttlOverride` powers the reaper
   *  affordance's "deploy as permanent", which re-runs the SAME previewed
   *  content at ttl_hours=0 instead of making the user start over. */
  const confirm = useCallback(async (
    preview: DeployPreview, overrideScan: boolean, ttlOverride?: number,
  ) => {
    if (inFlight.current) return
    inFlight.current = true
    setPhase({ kind: 'deploying' })
    try {
      const body: Body = {
        site_id: slug,
        artifact_slug: slug,
        confirm: true,
        ttl_hours: ttlOverride ?? ttlHours,
      }
      if (preview.content_digest) body.expected_content_digest = preview.content_digest
      // Send the resolved profile as well as the expectation of it. With only
      // `expected_profile`, the backend re-resolves the REGISTRY DEFAULT for this
      // call, so a user who picked a non-default profile previews under their
      // choice and then confirms against someone else's account -- caught by the
      // identity bind as a confusing 409 rather than deploying, but the request
      // was wrong either way.
      if (preview.profile) {
        body.profile = preview.profile
        body.expected_profile = preview.profile
      }
      if (preview.region) body.expected_region = preview.region
      if (overrideScan) body.override_scan = true
      const { status, data } = await callDeploy(body)
      const refusal = readRefusal(data)
      if (refusal) {
        setPhase({ kind: 'refused', refusal, preview })
        return
      }
      if (str(data.code) === 'stale_preview') {
        setPhase({
          kind: 'failed',
          message: str(data.error) || i18nT('components.directDeploy.stale_preview_retry'),
        })
        return
      }
      if (status === 409 && data.blocked && data.reason === 'scan') {
        setPhase({
          kind: 'scan-blocked',
          block: {
            findings: str(data.findings),
            count: typeof data.count === 'number' ? data.count : 0,
            credential: data.credential === true,
          },
          preview,
        })
        return
      }
      if (data.error) {
        setPhase({ kind: 'failed', message: str(data.error) })
        return
      }
      // A non-2xx has to fail even with nothing to quote. `callDeploy` turns a
      // non-JSON body into `{}`, so an infra-level 5xx behind a proxy carries
      // neither `error` nor `code`; reading success as "no error key" then
      // reports a deploy that never happened AND writes the deployed state back
      // onto the artifact. The status is the only thing such a reply still has.
      if (status < 200 || status >= 300) {
        setPhase({ kind: 'failed', message: i18nT('components.directDeploy.deploy_failed') })
        return
      }
      const url = str(data.url) || str(data.public_url)
      // A deploy can succeed and expose no link yet, so success is the ABSENCE
      // of an error rather than a non-empty url — conflating the two is what
      // renders a working deploy as a blank failure.
      setPhase({ kind: 'done', url })
      settleAfterDeploy(data)
    } catch (e: unknown) {
      setPhase({ kind: 'failed', message: e instanceof Error ? e.message : i18nT('components.directDeploy.deploy_failed') })
    } finally {
      inFlight.current = false
    }
  }, [slug, ttlHours, settleAfterDeploy])

  const reset = useCallback(() => setPhase({ kind: 'idle' }), [])

  return {
    phase, setPhase, reset, settleAfterDeploy,
    ttlHours, setTtlHours,
    start, confirm,
    busy: phase.kind === 'checking' || phase.kind === 'deploying',
    /** True once the backend has said this app cannot be deployed from the UI at
     *  all — the caller swaps its button for the agent hand-off. */
    needsAgent: phase.kind === 'refused' && phase.refusal.code === 'webapp_root_unavailable',
  }
}
