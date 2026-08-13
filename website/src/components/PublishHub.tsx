// PublishHub — the single publish surface for an artifact.
//
// All publish destinations are *providers* rendered from one registry:
//   • App providers (e.g. deploy-web "Publish to public web (your AWS)") — fetched
//     from GET /api/publish-providers and rendered via the generic flow.
//
// Kinds supported: widget, html, markdown, svg, json, text (non-webapp).
// Webapp artifacts have their own deploy flow (Artifact Deploy page).

import { useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { AlertCircle, AlertTriangle, Check, ExternalLink, Globe, Settings, Upload, X } from 'lucide-react'
import { api, type AppPublishProvider } from '../api/client'
import { Card, Btn } from './ui'
import PublicPublishAckModal from './PublicPublishAckModal'
import SimpleSelect from './SimpleSelect'
import type { Artifact } from '../types'
import { safeHttpUrl } from '../lib/safeUrl'

import { i18nT } from '../i18n/t'
interface UnifiedProvider {
  id: string
  label: string
  icon: typeof Globe
  configured: boolean
  setupRoute: string
  app?: AppPublishProvider
}

const ICONS: Record<string, typeof Globe> = { Globe, Upload, Settings, ExternalLink }
function iconFor(name: string): typeof Globe {
  return ICONS[name] ?? Upload
}

/**
 * Read the OUTCOME of a publish response, across the two shapes an endpoint returns.
 *
 * * The deploy-style shape — `{url}` / `{public_url}` — used by `/api/deploy/deploy`.
 * * The artifact shape — the serialized artifact carrying a `publication` block —
 *   returned by `POST /api/artifacts/{slug}/publish`, which is where an app
 *   provider lands when it hands the confirmed publish to the core route (the
 *   supported way to reuse the core's single publish authorization + audit).
 *
 * Returns `null` for anything unrecognized so the caller reports an explicit
 * error instead of rendering a blank one.
 *
 * **HTTP 200 is not success on the artifact shape.** `publish_sync.publish()`
 * treats the version push as best-effort: on a RE-publish it captures the push
 * error, persists it as `publication.last_error` and returns normally, so the
 * route answers 200 with a publication whose remote content is stale. Reading
 * that as "Published!" would be the same class of lie as the blank error this
 * function replaces, in the opposite direction — so a non-empty `last_error` is
 * an error outcome, carrying the provider's own (already redacted) message.
 *
 * A success whose destination exposes no browsable URL yields `{url: ''}` —
 * success WITHOUT a link, which is why a caller must not infer success from a
 * non-empty url.
 */
export function readPublishOutcome(
  data: Record<string, unknown> | null | undefined,
): { url: string } | { error: string } | null {
  if (!data || typeof data !== 'object') return null
  const direct = data.url ?? data.public_url
  if (typeof direct === 'string' && direct) return { url: direct }
  const pub = data.publication
  // `publication: null` (an UNpublished artifact) is deliberately not success.
  if (pub && typeof pub === 'object') {
    const lastError = (pub as { last_error?: unknown }).last_error
    if (typeof lastError === 'string' && lastError.trim()) return { error: lastError }
    const viewUrl = (pub as { view_url?: unknown }).view_url
    return { url: typeof viewUrl === 'string' ? viewUrl : '' }
  }
  return null
}

export function buildProviderList(
  appProviders: AppPublishProvider[],
  kind: string,
): UnifiedProvider[] {
  const list: UnifiedProvider[] = []
  for (const p of appProviders) {
    if (p.kinds.length && !p.kinds.includes(kind)) continue
    list.push({
      id: p.id,
      label: p.label,
      icon: iconFor(p.icon),
      configured: p.configured,
      setupRoute: p.setupRoute,
      app: p,
    })
  }
  return list
}

export function PublishHub({
  artifact,
  onClose,
}: {
  artifact: Artifact
  onClose?: () => void
}) {
  const navigate = useNavigate()
  const providersQuery = useQuery({
    queryKey: ['publish-providers'],
    queryFn: () => api.publishProviders(),
    staleTime: 30_000,
  })
  const appProviders = providersQuery.data?.providers ?? []
  const unified = buildProviderList(appProviders, artifact.kind)

  const [selectedId, setSelectedId] = useState<string>('')
  const [preview, setPreview] = useState<Record<string, unknown> | null>(null)
  const [contentDigest, setContentDigest] = useState<string>('')
  const [previewIdentity, setPreviewIdentity] = useState<{ profile: string; region: string }>({ profile: '', region: '' })
  const [scanBlocked, setScanBlocked] = useState<{ findings: string; count: number; credential?: boolean } | null>(null)
  // `error` is the discriminator the render keys off, so a success is the ABSENCE
  // of an error rather than a non-empty `url`: a destination can publish
  // successfully and expose no browsable link, and conflating the two is what
  // rendered a succeeded publish as a blank error.
  const [result, setResult] = useState<{ url?: string; error?: string } | null>(null)
  const [busy, setBusy] = useState(false)
  // Non-null while the blocking public-exposure acknowledgment is on screen.
  // `overrideScan` remembers WHICH commit path opened it, so acknowledging
  // resumes that path instead of collapsing both into a plain publish.
  const [ack, setAck] = useState<{ overrideScan: boolean } | null>(null)
  /** Latch making a confirmed publish at-most-once; see `confirmPublish`. */
  const publishInFlight = useRef(false)
  const [ttlHours, setTtlHours] = useState<string>('Persistent (no expiry)')
  const selectedTtlHours = () => (ttlHours === '72 hours (requires reaper)' ? 72 : 0)
  // A TTL change invalidates an existing preview — the previewed TTL
  // must match the confirmed one, so force a fresh preview.
  const onTtlChange = (v: string) => { setTtlHours(v); setPreview(null) }

  const selected = unified.find(p => p.id === selectedId)

  /** First call: no confirm → get preview or scan-blocked. */
  const requestPreview = async () => {
    if (!selected) return
    setBusy(true)
    setScanBlocked(null)
    setPreview(null)
    try {
      const resp = await api.publishToProvider(artifact.slug, selected.id, selected.app, selectedTtlHours())
      const outcome = readPublishOutcome(resp)
      if (resp?.requires_confirm) {
        setPreview(resp)
        setContentDigest(typeof resp.content_digest === 'string' ? resp.content_digest : '')
        setPreviewIdentity({
          profile: typeof resp.profile === 'string' ? resp.profile : '',
          region: typeof resp.region === 'string' ? resp.region : '',
        })
      } else if (resp?.blocked && resp?.reason === 'scan') {
        // The scan-block 409 carries preview bindings too --
        // store them so an override-confirm is pinned to the scanned
        // content and the resolved identity.
        setContentDigest(typeof resp.content_digest === 'string' ? resp.content_digest : '')
        setPreviewIdentity({
          profile: typeof resp.profile === 'string' ? resp.profile : '',
          region: typeof resp.region === 'string' ? resp.region : '',
        })
        setScanBlocked({ findings: resp.findings as string, count: resp.count as number, credential: !!resp.credential })
      } else if (resp?.error) {
        setResult({ error: String(resp.error) })
      } else if (outcome) {
        // Immediate success (already deployed / no confirm needed), or a
        // persisted push failure the route reported with a 200.
        setResult('error' in outcome ? { error: outcome.error } : { url: outcome.url })
      } else {
        setResult({ error: i18nT('components.publishHub.unexpected_response') })
      }
    } catch (err: unknown) {
      setResult({ error: err instanceof Error ? err.message : i18nT('components.publishHub.publish_failed') })
    } finally {
      setBusy(false)
    }
  }

  /** Second call: confirm=true to proceed (+ optional override_scan). */
  const confirmPublish = async (overrideScan = false) => {
    if (!selected) return
    // A confirmed publish must happen AT MOST ONCE per acknowledgment. `busy`
    // alone cannot enforce that: the acknowledgment lives inside `Modal`'s
    // <AnimatePresence>, so on close framer-motion keeps rendering the exiting
    // subtree from the element it captured BEFORE `busy` flipped -- an enabled
    // `danger` button, still hit-testable for the exit duration. A second click
    // there would issue a second confirmed deploy of the same slug. A ref is
    // the latch because it is written synchronously, so it is already set for a
    // click dispatched in the same render generation (state would still read
    // stale). Released in `finally`, so a failed publish stays retryable.
    if (publishInFlight.current) return
    publishInFlight.current = true
    setBusy(true)
    try {
      const endpoint = selected.app?.endpoint || '/api/deploy/deploy'
      const payload: Record<string, unknown> = {
        site_id: artifact.slug,
        artifact_slug: artifact.slug,
        provider_id: selected.id,
        confirm: true,
        ttl_hours: selectedTtlHours(),
      }
      if (contentDigest) payload.expected_content_digest = contentDigest
      // Bind the previewed identity -- backend 409s (stale_preview) if
      // the resolved profile/region drifted between preview and confirm.
      if (previewIdentity.profile) payload.expected_profile = previewIdentity.profile
      if (previewIdentity.region) payload.expected_region = previewIdentity.region
      if (overrideScan) payload.override_scan = true
      const r = await fetch(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Session-Key': 'dashboard:ui' },
        body: JSON.stringify(payload),
      })
      const data = await r.json()
      const outcome = readPublishOutcome(data)
      if (data?.code === 'stale_preview') {
        // Content changed since preview — force re-preview
        setPreview(null)
        setContentDigest('')
        setResult({ error: i18nT('components.publishHub.content_changed_since_preview_re_run_publish_to') })
      } else if (data?.blocked && data?.reason === 'scan') {
        setScanBlocked({ findings: data.findings, count: data.count, credential: !!data.credential })
        setPreview(null)
      } else if (data?.error) {
        // Checked BEFORE the outcome: an error response is authoritative even if
        // it happens to carry other fields.
        setResult({ error: data.error })
      } else if (outcome) {
        setResult('error' in outcome ? { error: outcome.error } : { url: outcome.url })
      } else {
        // An unrecognized shape is a failure we cannot describe — say so.
        // Reporting it as `{url: ''}` (the previous shape) rendered the error
        // branch with an UNDEFINED message: a bare red icon and no text, on a
        // publish that had in fact succeeded.
        setResult({ error: i18nT('components.publishHub.unexpected_response') })
      }
    } catch (err: unknown) {
      setResult({ error: err instanceof Error ? err.message : i18nT('components.publishHub.publish_failed') })
    } finally {
      setBusy(false)
      publishInFlight.current = false
    }
  }

  if (unified.length === 0) {
    return (
      <Card>
        <div className="flex items-center justify-between mb-2">
          <span className="text-sm font-semibold text-text-strong">{i18nT('components.publishHub.publish')}</span>
          {onClose && <Btn onClick={onClose} aria-label={i18nT('components.publishHub.close_publish_panel')}><X size={12} /></Btn>}
        </div>
        <p className="text-sm text-muted">{i18nT('components.publishHub.no_publish_providers_available_for_this_artifact')}</p>
      </Card>
    )
  }

  return (
    <Card>
      <div className="flex items-center justify-between mb-3">
        <span className="text-sm font-semibold text-text-strong">{i18nT('components.publishHub.publish')}</span>
        {onClose && <Btn onClick={onClose} aria-label={i18nT('components.publishHub.close_publish_panel')}><X size={12} /></Btn>}
      </div>

      {/* Provider list */}
      {!selectedId && (
        <div className="space-y-2">
          {unified.map(p => {
            const Icon = p.icon
            return (
              <button
                key={p.id}
                type="button"
                className="w-full flex items-center gap-3 px-3 py-2.5 rounded-md border border-border hover:border-accent/40 hover:bg-accent-subtle transition-all text-left cursor-pointer"
                onClick={() => {
                  if (!p.configured) {
                    navigate(p.setupRoute || '/deploy')
                  } else {
                    setSelectedId(p.id)
                  }
                }}
              >
                <Icon size={16} className="text-accent shrink-0" />
                <div className="flex-1 min-w-0">
                  <div className="text-sm font-medium text-text">{p.label}</div>
                  {!p.configured && (
                    <div className="text-[11px] text-warn flex items-center gap-1">
                      <Settings size={10} /> {i18nT('components.publishHub.setup_required')}
                    </div>
                  )}
                </div>
              </button>
            )
          })}
        </div>
      )}

      {/* Preview — shows what will happen, user must confirm */}
      {selected && preview && !result && !scanBlocked && (
        <div className="space-y-3">
          <div className="text-sm text-muted">
            {i18nT('components.publishHub.publishing')} <span className="font-mono font-semibold text-text">{artifact.slug}</span> {i18nT('components.publishHub.via')}{' '}
            <span className="font-semibold text-text">{selected.label}</span>
          </div>
          <div className="text-[12px] text-muted space-y-1">
            {typeof preview.message === 'string' && <p>{preview.message}</p>}
            {typeof preview.bytes === 'number' && <p>{i18nT('components.publishHub.size')} {(preview.bytes / 1024).toFixed(1)} {i18nT('components.publishHub.kb')}</p>}
            {typeof preview.scan === 'string' && <p>{i18nT('components.publishHub.scan')} {preview.scan}</p>}
          </div>
          <div className="flex items-start gap-2 text-[12px] text-warn p-2 rounded border border-warn/30 bg-warn-subtle">
            <AlertTriangle className="lucide-inline shrink-0" />
            <span>{i18nT('components.publishHub.public_exposure_warning')}</span>
          </div>
          <div className="flex gap-2">
            <Btn primary onClick={() => setAck({ overrideScan: false })} disabled={busy}>
              {busy ? i18nT('components.publishHub.publishing_2') : <><Upload size={12} /> {i18nT('components.publishHub.confirm_publish')}</>}
            </Btn>
            <Btn onClick={() => { setPreview(null); setSelectedId('') }}>{i18nT('components.publishHub.back')}</Btn>
          </div>
        </div>
      )}

      {/* Scan blocked — user must acknowledge findings to override */}
      {selected && scanBlocked && !result && (
        <div className="space-y-3">
          <div className="flex items-center gap-2 text-sm text-warn">
            <AlertCircle size={14} /> {i18nT('components.publishHub.scan_blocked_finding', { count: scanBlocked.count })}
          </div>
          <div className="text-[12px] text-muted p-2 rounded border border-warn/30 bg-warn-subtle">
            {scanBlocked.findings}
          </div>
          {scanBlocked.credential ? (
            <>
              <p className="text-[12px] text-warn font-medium">
                {i18nT('components.publishHub.credential_security_findings_cannot_be_overridde')}
              </p>
              <div className="flex gap-2">
                <Btn onClick={() => { setScanBlocked(null); setSelectedId('') }}>{i18nT('components.publishHub.cancel')}</Btn>
              </div>
            </>
          ) : (
            <>
              <p className="text-[12px] text-muted">
                {i18nT('components.publishHub.publishing_is_blocked_until_scan_findings_are_re')}
              </p>
              <div className="flex items-start gap-2 text-[12px] text-warn p-2 rounded border border-warn/30 bg-warn-subtle">
                <AlertTriangle className="lucide-inline shrink-0" />
                <span>{i18nT('components.publishHub.public_exposure_warning')}</span>
              </div>
              <div className="flex gap-2">
                <Btn danger onClick={() => setAck({ overrideScan: true })} disabled={busy}>
                  {busy ? i18nT('components.publishHub.publishing_2') : i18nT('components.publishHub.override_publish_anyway')}
                </Btn>
                <Btn onClick={() => { setScanBlocked(null); setSelectedId('') }}>{i18nT('components.publishHub.cancel')}</Btn>
              </div>
            </>
          )}
        </div>
      )}

      {/* Initial confirm step — request preview */}
      {selected && !preview && !result && !scanBlocked && (
        <div className="space-y-3">
          <div className="text-sm text-muted">
            {i18nT('components.publishHub.publish')} <span className="font-mono font-semibold text-text">{artifact.slug}</span> {i18nT('components.publishHub.via')}{' '}
            <span className="font-semibold text-text">{selected.label}</span>?
          </div>
          <div>
            <label className="text-[11px] text-muted block mb-1">{i18nT('components.publishHub.ttl_time_to_live')}</label>
            <SimpleSelect
              options={['Persistent (no expiry)', '72 hours (requires reaper)']}
              value={ttlHours}
              onChange={onTtlChange}
              aria-label={i18nT('components.publishHub.ttl_time_to_live')}
            />
          </div>
          <div className="flex gap-2">
            <Btn primary onClick={requestPreview} disabled={busy}>
              {busy ? i18nT('components.publishHub.checking') : <><Upload size={12} /> {i18nT('components.publishHub.publish')}</>}
            </Btn>
            <Btn onClick={() => setSelectedId('')}>{i18nT('components.publishHub.back')}</Btn>
          </div>
        </div>
      )}

      {/* Result */}
      {result && (
        <div className="space-y-2">
          {result.error ? (
            <div className="flex items-center gap-2 text-sm text-danger">
              <AlertCircle size={14} /> {result.error}
            </div>
          ) : (
            <div className="flex items-center gap-2 text-sm text-ok">
              <Check size={14} /> {i18nT('components.publishHub.published')}
              {result.url && safeHttpUrl(result.url) && (
                <a href={safeHttpUrl(result.url)!} target="_blank" rel="noreferrer" className="text-accent hover:underline inline-flex items-center gap-1">
                  <ExternalLink size={12} /> {result.url}
                </a>
              )}
            </div>
          )}
          <Btn onClick={() => { setResult(null); setPreview(null); setScanBlocked(null); setSelectedId(''); onClose?.() }}>{i18nT('components.publishHub.done')}</Btn>
        </div>
      )}

      {/* Blocking public-exposure acknowledgment — the last thing between a
          human and a world-readable URL, for BOTH commit paths. */}
      <PublicPublishAckModal
        open={!!ack}
        target={artifact.slug}
        ttlHours={selectedTtlHours()}
        busy={busy}
        onCancel={() => setAck(null)}
        onConfirm={() => {
          const overrideScan = !!ack?.overrideScan
          if (overrideScan) setScanBlocked(null)
          // The acknowledgment stays MOUNTED and `busy`-disabled until the
          // publish settles, then closes. Closing first (the previous shape)
          // handed the exiting <AnimatePresence> subtree an enabled confirm
          // button for the exit duration; holding it open is what lets `busy`
          // actually reach the buttons. `confirmPublish` also latches, so the
          // at-most-once guarantee does not depend on this timing.
          void confirmPublish(overrideScan).finally(() => setAck(null))
        }}
      />
    </Card>
  )
}
