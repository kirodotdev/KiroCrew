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
import { Card, Btn, ContentSkeleton } from './ui'
import PublicPublishAckModal from './PublicPublishAckModal'
import SimpleSelect from './SimpleSelect'
import type { Artifact, PublishProviderDescriptor } from '../types'
import { safeHttpUrl } from '../lib/safeUrl'

import { i18nT } from '../i18n/t'
interface UnifiedProvider {
  id: string
  label: string
  icon: typeof Globe
  configured: boolean
  setupRoute: string
  app?: AppPublishProvider
  /** Set for a row from the CORE registry (GET /api/artifacts/publish-providers).
   *  Its presence is what routes a publish at the artifact endpoint instead of the
   *  app row's declared endpoint -- see `requestPreview`. */
  core?: PublishProviderDescriptor
  /** The provider's own remedy text, rendered in place when `configured` is false.
   *  Only a provider knows WHICH action makes it available, so a core row explains
   *  itself here rather than sending the user to a generic setup page. */
  installHint?: string
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
 *
 * A non-empty `publication.notice` rides ALONGSIDE a success outcome, never as
 * an error: it means the publish succeeded but the link is not usable yet (e.g.
 * CloudFront still rolling out). It is deliberately NOT folded into `error` —
 * `last_error` is checked first and wins, so a real failure is still an error;
 * a notice-only publication is a success carrying a warn line beside the link.
 */
export function readPublishOutcome(
  data: Record<string, unknown> | null | undefined,
): { url: string; notice?: string; notice_code?: string } | { error: string } | null {
  if (!data || typeof data !== 'object') return null
  const direct = data.url ?? data.public_url
  if (typeof direct === 'string' && direct) return { url: direct }
  const pub = data.publication
  // `publication: null` (an UNpublished artifact) is deliberately not success.
  if (pub && typeof pub === 'object') {
    const lastError = (pub as { last_error?: unknown }).last_error
    if (typeof lastError === 'string' && lastError.trim()) return { error: lastError }
    const viewUrl = (pub as { view_url?: unknown }).view_url
    const noticeRaw = (pub as { notice?: unknown }).notice
    const notice = typeof noticeRaw === 'string' && noticeRaw.trim() ? noticeRaw : undefined
    // The discriminator travels WITH the notice: it is meaningless without one,
    // so it is only surfaced when a notice is actually present.
    const codeRaw = (pub as { notice_code?: unknown }).notice_code
    const notice_code = notice && typeof codeRaw === 'string' && codeRaw.trim() ? codeRaw : undefined
    return {
      url: typeof viewUrl === 'string' ? viewUrl : '',
      ...(notice ? { notice } : {}),
      ...(notice_code ? { notice_code } : {}),
    }
  }
  return null
}

/** The three catalog keys a render site offers for a publish notice: the
 *  existing rolling-out line, the disabled-distribution remedy, and the neutral
 *  fallback that promises no time. */
export interface PublishNoticeKeys {
  rolling_out: string
  distribution_disabled: string
  notice_generic: string
}

/**
 * Selects the catalog key for a publish notice from its machine `notice_code`.
 *
 * The backend used to hand back only free text, and the UI printed one fixed
 * "still rolling out — reachable in a few minutes" line for every notice. That
 * is a lie for a DISABLED distribution, whose links never resolve until a human
 * re-enables it — so a time promise there sends the user off to wait for
 * something that will not happen. The `notice_code` discriminator lets each case
 * get its own copy, and anything unrecognised (or an empty-but-present notice)
 * falls back to a neutral line that promises no time at all — never a time
 * promise the code does not justify.
 */
export function publishNoticeKey(keys: PublishNoticeKeys, noticeCode: string | undefined): string {
  switch (noticeCode) {
    case 'rolling_out':
      return keys.rolling_out
    case 'distribution_disabled':
      return keys.distribution_disabled
    default:
      // 'unknown', empty, or any value a newer backend introduces.
      return keys.notice_generic
  }
}

export const TTL_72 = '72 hours (requires reaper)'

/**
 * Whether a TTL may be chosen for this row at all.
 *
 * A core destination declaring no expiration support gets no TTL control: the core
 * publish route carries no TTL, so offering "72 hours" would hand back a persistent
 * public link while the user believed the exposure was time-boxed.
 */
export function ttlSelectableFor(selected: UnifiedProvider | undefined): boolean {
  return !(selected?.core && !selected.core.sharing_model.supports_expiration)
}

/**
 * The TTL that actually applies to the current selection.
 *
 * `ttlHours` is ONE piece of state shared across rows, so hiding the control was not
 * enough: choosing "72 hours" on a row that supports expiry and then switching to a
 * core row that does not left the choice standing underneath the hidden control, and
 * the acknowledgment modal went on promising a time-boxed exposure for a link that is
 * permanent. Deriving from the same predicate that hides the control -- rather than
 * resetting the state when the selection changes -- is what makes the two unable to
 * disagree; a missed reset or a re-render ordering cannot bring the promise back.
 */
export function effectiveTtlHours(
  ttlChoice: string,
  selected: UnifiedProvider | undefined,
): number {
  return ttlSelectableFor(selected) && ttlChoice === TTL_72 ? 72 : 0
}

export function buildProviderList(
  appProviders: AppPublishProvider[],
  kind: string,
  coreProviders: PublishProviderDescriptor[] = [],
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
  // Core-registry rows come SECOND, and an id already claimed by an app row is
  // skipped. An enabled app may declare a row under a core destination's id, and
  // the pre-existing resolution for that clash is app-first (test_publish_providers
  // asserts the APP's endpoint wins). Ordering the merge this way keeps that
  // behaviour rather than quietly reversing it.
  const claimed = new Set(list.map(p => p.id))
  for (const c of coreProviders) {
    if (!c.capable || claimed.has(c.name)) continue
    list.push({
      id: c.name,
      label: c.display_name,
      icon: Globe,
      // `available` is the core registry's word for the same thing `configured` means
      // to an app row: usable right now, without the user going and setting something
      // up first. Missing means available -- the field is documented as omitted by
      // older gateways, so treating absence as "needs setup" would mark every
      // destination on such a gateway unusable.
      configured: c.available !== false,
      setupRoute: '',
      core: c,
      installHint: c.install_hint,
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
  // The core registry is a SECOND source and the panel has to read both. The app
  // endpoint deliberately omits built-in destinations ("registered frontend-side and
  // are not returned here"), so a provider registered by the edition -- which is how
  // a stock build gets any publish destination at all -- appears nowhere without this.
  const coreQuery = useQuery({
    queryKey: ['artifact-publish-providers', artifact.kind],
    queryFn: () => api.getArtifactPublishProviders(artifact.kind),
    staleTime: 30_000,
  })
  const coreProviders = coreQuery.data?.providers ?? []
  const unified = buildProviderList(appProviders, artifact.kind, coreProviders)

  const [selectedId, setSelectedId] = useState<string>('')
  const [preview, setPreview] = useState<Record<string, unknown> | null>(null)
  const [contentDigest, setContentDigest] = useState<string>('')
  const [previewIdentity, setPreviewIdentity] = useState<{ profile: string; region: string }>({ profile: '', region: '' })
  const [scanBlocked, setScanBlocked] = useState<{ findings: string; count: number; credential?: boolean } | null>(null)
  // `error` is the discriminator the render keys off, so a success is the ABSENCE
  // of an error rather than a non-empty `url`: a destination can publish
  // successfully and expose no browsable link, and conflating the two is what
  // rendered a succeeded publish as a blank error.
  const [result, setResult] = useState<{ url?: string; error?: string; notice?: string; notice_code?: string } | null>(null)
  const [busy, setBusy] = useState(false)
  // Non-null while the blocking public-exposure acknowledgment is on screen.
  // `overrideScan` remembers WHICH commit path opened it, so acknowledging
  // resumes that path instead of collapsing both into a plain publish.
  const [ack, setAck] = useState<{ overrideScan: boolean } | null>(null)
  /** Latch making a confirmed publish at-most-once; see `confirmPublish`. */
  const publishInFlight = useRef(false)
  const [ttlHours, setTtlHours] = useState<string>('Persistent (no expiry)')
  // A TTL change invalidates an existing preview — the previewed TTL
  // must match the confirmed one, so force a fresh preview.
  const onTtlChange = (v: string) => { setTtlHours(v); setPreview(null) }

  const selected = unified.find(p => p.id === selectedId)
  // Whether a TTL may be chosen for the CURRENT selection. One condition, read by
  // both the control's visibility and the value everything downstream uses, because
  // the bug here was those two disagreeing: `ttlHours` is a single piece of state
  // shared across rows, so picking "72 hours" on an app row and then switching to a
  // core row that cannot expire hid the control while leaving the choice standing —
  // and the acknowledgment modal went on promising a time-boxed exposure for a link
  // that is permanent. Deriving the value rather than resetting the state on
  // selection means no ordering or missed reset can bring the promise back.
  const ttlSelectable = ttlSelectableFor(selected)
  const selectedTtlHours = () => effectiveTtlHours(ttlHours, selected)
  // A core row can be SELECTED while unconfigured (its remedy is its own hint, so
  // it is selected rather than routed away — see the provider-list onClick). But an
  // unconfigured destination cannot publish, so the confirm step must NOT offer a
  // live Publish CTA that only fails after the acknowledgment: show the remedy and a
  // link to set the destination up instead.
  const unconfiguredCore = !!(selected?.core && !selected.configured)

  /** First call: no confirm → get preview or scan-blocked. */
  const requestPreview = async () => {
    if (!selected) return
    setBusy(true)
    setScanBlocked(null)
    setPreview(null)
    try {
      if (selected.core) {
        // A core row does NOT publish here. PublicPublishAckModal is the blocking
        // acknowledgment in front of every action that creates a publicly accessible
        // website (#3599), and it is reached from the confirm step -- so posting on this
        // first click would make content world-readable with no consent shown at all.
        // The backend has no preview to return for this path, so the confirm step is
        // entered locally: consent is about what is ABOUT to happen, not about a digest.
        setPreview({ requires_confirm: true, core: true })
        return
      }
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
        setResult('error' in outcome ? { error: outcome.error } : { url: outcome.url, notice: outcome.notice, notice_code: outcome.notice_code })
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
      if (selected.core) {
        // Reached only from the acknowledgment, same as the app path. A core row MUST NOT
        // go through the deploy endpoint below: with no app endpoint that path falls back
        // to `/api/deploy/deploy`, the per-artifact deploy machinery this destination
        // exists to replace.
        const resp = await api.publishArtifactToCoreProvider(artifact.slug, selected.id)
        const outcome = readPublishOutcome(resp)
        if (!outcome) {
          // Same condition, same wording as the app path below: the response carried
          // neither a link nor a publication.
          setResult({ error: typeof resp?.error === 'string' ? resp.error : i18nT('components.publishHub.unexpected_response') })
        } else {
          setResult('error' in outcome ? { error: outcome.error } : { url: outcome.url, notice: outcome.notice, notice_code: outcome.notice_code })
        }
        return
      }
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
        // `notice_code` travels with `notice` here for the same reason as the two
        // sibling sites above: the catalog selector keys the remedy copy off the code,
        // so dropping it silently downgrades a `distribution_disabled` notice to the
        // generic warning -- the one case whose whole point is telling the user how to
        // re-enable. Every site that reads an outcome must copy BOTH fields.
        setResult('error' in outcome ? { error: outcome.error } : { url: outcome.url, notice: outcome.notice, notice_code: outcome.notice_code })
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

  // While EITHER provider source is still loading, hold a skeleton rather than the
  // empty state. `unified.length === 0` is true during that window too, so without
  // this gate a stock build flashes "No publish providers available" before the core
  // row arrives from its query.
  if (providersQuery.isLoading || coreQuery.isLoading) {
    return (
      <Card>
        <div className="flex items-center justify-between mb-2">
          <span className="text-sm font-semibold text-text-strong">{i18nT('components.publishHub.publish')}</span>
          {onClose && <Btn onClick={onClose} aria-label={i18nT('components.publishHub.close_publish_panel')}><X size={12} /></Btn>}
        </div>
        <ContentSkeleton rows={2} />
      </Card>
    )
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
                  if (p.configured) {
                    setSelectedId(p.id)
                  } else if (p.core) {
                    // Select it rather than navigating. A core destination's setup is
                    // described by its OWN hint, shown below; sending the user to the
                    // deploy setup page would explain a different destination's flow.
                    setSelectedId(p.id)
                  } else {
                    navigate(p.setupRoute || '/deploy')
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
                  {!p.configured && p.installHint && (
                    <div className="mt-1.5 text-[11px] leading-relaxed text-muted whitespace-pre-line">
                      {p.installHint}
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
          {/* Unconfigured core destination: no live Publish. Offering one here lets the
              user pass the acknowledgment and only THEN hit a failure. Show the remedy
              (the provider's own hint) plus a link to set it up, and no Publish CTA. */}
          {unconfiguredCore ? (
            <>
              {selected.installHint && (
                <div className="text-[12px] leading-relaxed text-muted whitespace-pre-line">
                  {selected.installHint}
                </div>
              )}
              <div className="flex gap-2">
                <Btn primary onClick={() => navigate('/deploy')}>
                  <Settings size={12} /> {i18nT('components.publishHub.open_artifact_deploy')}
                </Btn>
                <Btn onClick={() => setSelectedId('')}>{i18nT('components.publishHub.back')}</Btn>
              </div>
            </>
          ) : (
            <>
          {/* A core destination declaring no expiration support gets no TTL control at all.
              Offering it would be a lie: the core publish route carries no TTL, so choosing
              "72 hours" would hand back a persistent public link while the user believed the
              exposure was time-boxed. Same `ttlSelectable` the value derives from, so a
              hidden control can never leave a live choice behind it. */}
          {ttlSelectable && (
          <div>
            <label className="text-[11px] text-muted block mb-1">{i18nT('components.publishHub.ttl_time_to_live')}</label>
            <SimpleSelect
              options={['Persistent (no expiry)', '72 hours (requires reaper)']}
              value={ttlHours}
              onChange={onTtlChange}
              aria-label={i18nT('components.publishHub.ttl_time_to_live')}
            />
          </div>
          )}
          <div className="flex gap-2">
            <Btn primary onClick={requestPreview} disabled={busy}>
              {busy ? i18nT('components.publishHub.checking') : <><Upload size={12} /> {i18nT('components.publishHub.publish')}</>}
            </Btn>
            <Btn onClick={() => setSelectedId('')}>{i18nT('components.publishHub.back')}</Btn>
          </div>
            </>
          )}
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
            <div className="space-y-1.5">
              <div className="flex items-center gap-2 text-sm text-ok">
                <Check size={14} /> {i18nT('components.publishHub.published')}
                {result.url && safeHttpUrl(result.url) && (
                  <a href={safeHttpUrl(result.url)!} target="_blank" rel="noreferrer" className="text-accent hover:underline inline-flex items-center gap-1">
                    <ExternalLink size={12} /> {result.url}
                  </a>
                )}
              </div>
              {/* A notice rides WITH a success: the publish worked, the link just
                  is not reachable yet. Neutral/warn line beside the link, never
                  the danger surface `error` drives. */}
              {result.notice && (
                <div className="flex items-start gap-1.5 text-[12px] text-warn">
                  <AlertTriangle className="lucide-inline shrink-0" />
                  <span>{i18nT(publishNoticeKey({
                    rolling_out: 'components.publishHub.published_still_rolling_out',
                    distribution_disabled: 'components.publishHub.published_distribution_disabled',
                    notice_generic: 'components.publishHub.published_notice_generic',
                  }, result.notice_code))}</span>
                </div>
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
        // The default sentence names `recall` and `destroy` -- the deploy surface's
        // actions. A core destination has neither, so it would be telling the user their
        // way out is something that does not exist. Say what actually ends the exposure.
        persistentExposureNote={
          selected?.core
            ? i18nT('components.publicPublishAckModal.exposure_window_persistent_withdrawable')
            : undefined
        }
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
