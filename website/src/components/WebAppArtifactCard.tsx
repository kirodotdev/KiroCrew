import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Cloud,
  CloudOff,
  Copy,
  Database,
  ExternalLink,
  Globe,
  Infinity,
  Rocket,
  Server,
  Trash2,
} from 'lucide-react'
import { api } from '../api/client'
import { Badge } from '../components/ui'
import SimpleSelect from '../components/SimpleSelect'
import { framablePreviewUrl, safeHttpUrl } from '../lib/safeUrl'
import type { Artifact, WebAppMetadata } from '../types'

import { i18nT } from '../i18n/t'
function statusBadgeVariant(status: string): 'ok' | 'warn' | 'err' | 'aim' {
  switch (status) {
    case 'live': return 'ok'
    case 'deploying': return 'warn'
    case 'expired': return 'aim'
    case 'error': return 'err'
    default: return 'aim'
  }
}

function formatCountdown(expiresAt: string | null, persistent: boolean): string {
  if (persistent) return i18nT('components.webAppArtifactCard.persistent_2')
  if (!expiresAt) return i18nT('components.webAppArtifactCard.no_expiry_set')
  const diff = new Date(expiresAt).getTime() - Date.now()
  if (Number.isNaN(diff)) return i18nT('components.webAppArtifactCard.no_expiry_set')
  if (diff <= 0) return 'expired'
  const days = Math.floor(diff / 86400000)
  const hours = Math.floor((diff % 86400000) / 3600000)
  const mins = Math.floor((diff % 3600000) / 60000)
  const parts: string[] = []
  if (days > 0) parts.push(`${days}d`)
  if (hours > 0) parts.push(`${hours}h`)
  parts.push(`${mins}m`)
  return `expires in ${parts.join(' ')}`
}

function ttlProgressPct(expiresAt: string | null, ttlHours: number): number {
  if (!expiresAt || ttlHours <= 0) return 100
  const end = new Date(expiresAt).getTime()
  const start = end - ttlHours * 3600000
  const now = Date.now()
  if (now >= end) return 0
  if (now <= start) return 100
  return Math.round(((end - now) / (end - start)) * 100)
}

/** The three traffic-light dots of the mock browser chrome. Decorative. */
function ChromeDots() {
  return (
    <div className="flex gap-1.5 shrink-0" aria-hidden="true">
      <span className="w-2.5 h-2.5 rounded-full bg-danger/50" />
      <span className="w-2.5 h-2.5 rounded-full bg-warn/50" />
      <span className="w-2.5 h-2.5 rounded-full bg-ok/50" />
    </div>
  )
}

/** One architecture row: icon + label + human description + resource id. */
function ArchRow({
  icon,
  label,
  text,
  resourceId,
}: {
  icon: React.ReactNode
  label: string
  text: string
  resourceId?: string
}) {
  return (
    <div className="flex items-start gap-2.5">
      <span className="mt-0.5 shrink-0 text-muted" aria-hidden="true">{icon}</span>
      <div className="min-w-0">
        <span className="text-muted mr-1.5">{label}:</span>
        <span className="text-sm text-text">{text}</span>
        {resourceId && (
          <code className="ml-2 text-[11px] text-muted break-all">{resourceId}</code>
        )}
      </div>
    </div>
  )
}

/** Architecture rows with per-layer icons, shared by every card state. */
function ArchitectureRows({ architecture }: { architecture: WebAppMetadata['architecture'] }) {
  const res = (t: string) =>
    architecture.resources.find((r: { type: string; id: string }) => r.type === t)?.id
  return (
    <div className="space-y-1.5">
      {architecture.frontend && (
        <ArchRow icon={<Globe size={14} />} label={i18nT('components.webAppArtifactCard.frontend')} text={architecture.frontend} resourceId={res('frontend')} />
      )}
      {architecture.backend && (
        <ArchRow icon={<Server size={14} />} label={i18nT('components.webAppArtifactCard.backend')} text={architecture.backend} resourceId={res('backend')} />
      )}
      {architecture.state && (
        <ArchRow icon={<Database size={14} />} label={i18nT('components.webAppArtifactCard.state')} text={architecture.state} resourceId={res('state')} />
      )}
    </div>
  )
}

/** Cost estimate pills: `1,000 views · $0.05` as one compact chip each. */
function CostPills({ cost, label }: { cost: WebAppMetadata['cost']; label: string }) {
  if (cost.estimates.length === 0) return null
  return (
    <div>
      <div className="flex items-center gap-1.5 mb-1.5">
        <span className="text-[12px] text-muted font-medium">{label}</span>
        {/* Each pill is a what-if traffic scenario over the window — say so
            loudly, so "$45" isn't read as a monthly bill. */}
        <span className="text-[10px] px-1.5 py-px rounded-full bg-warn-subtle text-warn font-medium uppercase tracking-wide">
          {i18nT('components.webAppArtifactCard.estimate')}
        </span>
      </div>
      <div className="flex flex-wrap gap-1.5">
        {cost.estimates.map((e: { views: number; usd: number }, i: number) => (
          <span
            key={i}
            className="inline-flex items-baseline gap-1.5 rounded-full border border-border bg-bg-elevated px-2.5 py-1"
          >
            <span className="text-[11px] text-muted">{Number(e.views ?? 0).toLocaleString()} {i18nT('components.webAppArtifactCard.views')}</span>
            <span className="text-[12px] font-medium text-text-strong">${Number(e.usd ?? 0).toFixed(4)}</span>
          </span>
        ))}
      </div>
      <div className="text-[11px] text-muted mt-1.5">
        {i18nT('components.webAppArtifactCard.what_if_traffic_scenarios_you_pay_only_for_actua')} {cost.note} {i18nT('components.webAppArtifactCard.idle')}{cost.idle_usd} {i18nT('components.webAppArtifactCard.billed_to_your_account')}
      </div>
    </div>
  )
}

/** Deploy-target pills (provider / account / region / profile). */
function TargetPills({ dt }: { dt: WebAppMetadata['deploy_target'] }) {
  return (
    <div className="flex gap-1.5 flex-wrap">
      <span className="text-[11px] px-2 py-0.5 rounded-full bg-bg-elevated border border-border text-muted uppercase">
        {dt.provider}
      </span>
      <span className="text-[11px] px-2 py-0.5 rounded-full bg-bg-elevated border border-border text-muted">
        {i18nT('components.webAppArtifactCard.acct')} {dt.account}
      </span>
      <span className="text-[11px] px-2 py-0.5 rounded-full bg-bg-elevated border border-border text-muted">
        {dt.region}
      </span>
      {dt.profile && (
        <span className="text-[11px] px-2 py-0.5 rounded-full bg-bg-elevated border border-border text-muted">
          {i18nT('components.webAppArtifactCard.profile')} {dt.profile}
        </span>
      )}
    </div>
  )
}

/** Scaled-down live iframe of the deployed site, framed like a minified
 * desktop viewport (same trick as the gallery WidgetThumb): the frame lays
 * out at BASE_W so the app renders its desktop design, then the whole thing
 * is CSS-scaled to the card width. CloudFront-only (framablePreviewUrl) and
 * mirrored by the server CSP frame-src. */
function LiveSiteFrame({ url, slug }: { url: string; slug: string }) {
  const BASE_W = 1280
  const BASE_H = 720
  const wrapRef = useRef<HTMLDivElement>(null)
  const [w, setW] = useState(640)
  useEffect(() => {
    const el = wrapRef.current
    if (!el) return
    const measure = () => setW(el.clientWidth || 640)
    measure()
    if (typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [])
  const scale = w / BASE_W
  return (
    <div ref={wrapRef} className="relative w-full overflow-hidden bg-card" style={{ height: Math.round(BASE_H * scale) }}>
      <iframe
        src={url}
        // Remote-origin site preview: allow-same-origin here refers to the
        // SITE's own https://*.cloudfront.net origin (needed for its API
        // fetches), never the dashboard origin — the frame gets no access to
        // dashboard DOM/storage. No top-navigation, no downloads.
        sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox"
        referrerPolicy="no-referrer"
        loading="lazy"
        title={i18nT('components.webAppArtifactCard.live_preview', { slug })}
        tabIndex={-1}
        className="border-none bg-card block"
        style={{ width: BASE_W, height: BASE_H, transform: `scale(${scale})`, transformOrigin: 'top left' }}
      />
    </div>
  )
}

/** Query the gateway's local preview channel for this artifact. Returns the
 * token-gated base URL when the app's local copy is servable, null otherwise.
 * Shared react-query key with the gallery thumb so detail + gallery mint one
 * token per slug per TTL window. */
export function useAppPreview(slug: string, enabled: boolean) {
  const { data } = useQuery<{ available: boolean; base?: string; remote_framable?: boolean }>({
    queryKey: ['webapp-preview', slug],
    queryFn: async () => {
      const r = await fetch(`/api/artifacts/${encodeURIComponent(slug)}/app-preview`)
      if (!r.ok) return { available: false }
      return (await r.json()) as { available: boolean; base?: string; remote_framable?: boolean }
    },
    staleTime: 5 * 60_000,
    // The backend token expires after 15 minutes and staleTime alone never
    // refetches a mounted query — long-lived previews would start 404ing on
    // lazy-loaded assets. Re-mint before expiry; the iframe is keyed by base
    // so a fresh token reloads it cleanly.
    refetchInterval: 10 * 60_000,
    enabled,
  })
  return {
    base: data?.available && data.base ? data.base : null,
    // Only trust a remote CloudFront iframe when the gateway probed the
    // deployed site's headers and confirmed browsers will frame it —
    // pre-existing base stacks still send X-Frame-Options: SAMEORIGIN, which
    // renders as a silent blank. Anything short of an explicit "yes" (probe
    // failure, legacy response, still loading) falls to the hero.
    remoteFramable: data?.remote_framable === true,
  }
}

/** Scaled preview of the app's LOCAL copy, served by the gateway's
 * token-gated static channel. sandbox="allow-scripts" only: the document
 * runs with an opaque origin (double-enforced by the channel's own CSP
 * `sandbox` header) and can never reach dashboard cookies/DOM. Relative
 * subresources resolve under the token path automatically. */
function LocalAppFrame({ base, slug }: { base: string; slug: string }) {
  const BASE_W = 1280
  const BASE_H = 720
  const wrapRef = useRef<HTMLDivElement>(null)
  const [w, setW] = useState(640)
  useEffect(() => {
    const el = wrapRef.current
    if (!el) return
    const measure = () => setW(el.clientWidth || 640)
    measure()
    if (typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [])
  const scale = w / BASE_W
  return (
    <div ref={wrapRef} className="relative w-full overflow-hidden bg-card" style={{ height: Math.round(BASE_H * scale) }}>
      <iframe
        key={base}
        src={base}
        sandbox="allow-scripts"
        referrerPolicy="no-referrer"
        loading="lazy"
        title={i18nT('components.webAppArtifactCard.app_preview', { slug })}
        tabIndex={-1}
        className="border-none bg-card block"
        style={{ width: BASE_W, height: BASE_H, transform: `scale(${scale})`, transformOrigin: 'top left' }}
      />
    </div>
  )
}

export default function WebAppArtifactCard({
  artifact,
  onTornDown,
}: {
  artifact: Artifact
  onTornDown?: () => void
}) {
  const meta = artifact.webapp_metadata
  // All hooks run unconditionally (react-hooks/rules-of-hooks): derive
  // null-safe views of the metadata so the hook call order is stable even
  // when webapp_metadata tolerant-loads to null or is populated by a later
  // refetch. The "no metadata" guard lives below every hook.
  const lc = meta?.lifecycle
  const arch = meta?.architecture
  const dt = meta?.deploy_target

  const [countdown, setCountdown] = useState(() =>
    formatCountdown(lc?.expires_at ?? null, lc?.persistent ?? false),
  )
  const [progress, setProgress] = useState(() =>
    ttlProgressPct(lc?.expires_at ?? null, lc?.ttl_hours ?? 0),
  )
  const queryClient = useQueryClient()

  // Live countdown timer. Eagerly resync on every lifecycle change (e.g. a
  // post-teardown refetch flipping status to 'expired') and depend on stable
  // primitives so react-query returning a fresh lifecycle object per refetch
  // does not needlessly tear down / recreate the interval.
  useEffect(() => {
    const expiresAt = lc?.expires_at ?? null
    const persistent = lc?.persistent ?? false
    const ttlHours = lc?.ttl_hours ?? 0
    const status = lc?.status
    setCountdown(formatCountdown(expiresAt, persistent))
    setProgress(ttlProgressPct(expiresAt, ttlHours))
    // expiresAt is null whenever lc is missing, so !expiresAt already covers it.
    if (persistent || !expiresAt || status === 'expired') return
    const id = setInterval(() => {
      setCountdown(formatCountdown(expiresAt, persistent))
      setProgress(ttlProgressPct(expiresAt, ttlHours))
    }, 30000)
    return () => clearInterval(id)
  }, [lc?.expires_at, lc?.persistent, lc?.ttl_hours, lc?.status])

  const tierSummary = useMemo(() => {
    if (!arch) return ''
    const parts: string[] = []
    if (arch.state) parts.push(i18nT('components.webAppArtifactCard.stateful_app'))
    else if (arch.backend) parts.push(i18nT('components.webAppArtifactCard.api_app'))
    else parts.push(i18nT('components.webAppArtifactCard.static_app'))
    parts.push(`${arch.tier}-tier`)
    return parts.join(' \u00b7 ')
  }, [arch])

  const teardownMut = useMutation({
    mutationFn: () => api.artifactTeardown(artifact.slug),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['artifact', artifact.slug] })
      onTornDown?.()
    },
  })

  const handleCopy = useCallback(() => {
    const safe = dt ? safeHttpUrl(dt.public_url) : null
    if (safe) navigator.clipboard.writeText(safe)
  }, [dt])

  const navigate = useNavigate()
  // Deploy-time profile picker: registered profiles from the Artifact Deploy
  // app's control plane. Empty selection = the registry default; the choice is
  // baked into the seed prompt so the skill runs `--profile <choice>` and
  // back-fills deploy_target.profile with it.
  const [deployProfile, setDeployProfile] = useState('')
  const { data: profilesResp } = useQuery<{ profiles: { name: string }[]; default: string }>({
    queryKey: ['deploy-web', 'profiles'],
    queryFn: async () => {
      const r = await fetch('/api/deploy/profiles')
      if (!r.ok) return { profiles: [], default: '' }
      return (await r.json()) as { profiles: { name: string }[]; default: string }
    },
    staleTime: 30000,
  })
  const registeredProfiles = profilesResp?.profiles ?? []
  const defaultProfile = profilesResp?.default ?? ''
  // Derived once so the picker's value array and label array stay in lockstep.
  const pickableProfiles = registeredProfiles.filter((p) => p.name !== defaultProfile)
  // Local-first preview: the app's local copy (like html/widget artifacts)
  // beats iframing the remote deployment — no dependency on remote frame
  // headers, CDN propagation, or the deployment even existing.
  const { base: previewBase, remoteFramable } = useAppPreview(artifact.slug, !!meta)
  // Deploy launches a FRESH chat session that auto-runs the artifact-deploy skill
  // on this artifact — the same __mc_chat_launch mechanism ChatPage consumes (new
  // session + auto-send). A fresh session is the isolation boundary, so no
  // subagent is needed: the agent adapts + deploys + debugs inline there. The
  // prompt is phrased to trigger the artifact-deploy skill.
  const openDeployChat = useCallback(() => {
    const chosen = deployProfile || meta?.deploy_target?.profile || defaultProfile
    ;(window as unknown as { __mc_chat_launch?: { message: string; ts: number } }).__mc_chat_launch = {
      message:
        `Deploy the app artifact "${artifact.slug}" to my AWS account using the ` +
        `artifact-deploy skill: adapt it to the deploy contract, ship it, and give me the public link.` +
        (chosen ? ` Use the AWS profile "${chosen}".` : ''),
      ts: Date.now(),
    }
    navigate('/chat')
  }, [navigate, artifact.slug, deployProfile, defaultProfile, meta?.deploy_target?.profile])

  // Guard AFTER all hooks so the hook call order never changes between renders.
  if (!meta) {
    return <div className="text-muted text-sm p-4">{i18nT('components.webAppArtifactCard.no_app_metadata_available')}</div>
  }

  const { deploy_target, architecture, lifecycle, cost, origin_session } = meta
  const safeUrl = safeHttpUrl(deploy_target.public_url)
  const isExpired = lifecycle.status === 'expired' || teardownMut.isSuccess || countdown === 'expired'
  const isDeploying = lifecycle.status === 'deploying'
  // Not deployed yet: no live http(s) URL, not expired, not mid-deploy → show the
  // Deploy affordance instead of the infra control card (artifact-first model —
  // the app artifact exists before any deploy).
  const notDeployed = !deploy_target.public_url && !isExpired && !isDeploying
  const frameUrl = !isExpired && !isDeploying && remoteFramable ? framablePreviewUrl(deploy_target.public_url) : null
  const costLabel = cost.model === 'ttl-window'
    ? i18nT('components.webAppArtifactCard.estimated_cost_over_ttl_window', { hours: cost.window_hours })
    : i18nT('components.webAppArtifactCard.estimated_monthly_cost')

  const handleTeardown = () => {
    const resourceList = architecture.resources
      .map((r: { type: string; id: string }) => `  ${r.type}: ${r.id}`)
      .join('\n')
    const msg = i18nT('components.webAppArtifactCard.tear_down_confirm', { resources: resourceList })
    if (!window.confirm(msg)) return
    teardownMut.mutate()
  }

  // ---------------------------------------------------------------- not deployed
  if (notDeployed) {
    return (
      <div className="space-y-4">
        {/* Hero CTA: the artifact exists, the infra doesn't — invite the deploy. */}
        <div className="rounded-xl border border-border bg-gradient-to-br from-accent-subtle via-card to-card p-5">
          <div className="flex items-start justify-between gap-3">
            <div className="flex items-center gap-3 min-w-0">
              <div className="shrink-0 w-10 h-10 rounded-lg bg-accent/10 border border-accent/30 flex items-center justify-center">
                <Rocket size={18} className="text-accent" aria-hidden="true" />
              </div>
              <div className="min-w-0">
                <div className="font-semibold text-text-strong truncate">{artifact.slug}</div>
                {tierSummary && <div className="text-[12px] text-muted">{tierSummary}</div>}
              </div>
            </div>
            <Badge variant="aim">{i18nT('components.webAppArtifactCard.not_deployed')}</Badge>
          </div>
          <p className="text-sm text-muted mt-3 mb-4">
            {i18nT('components.webAppArtifactCard.not_deployed_yet_deploy_to_your_own_aws_account')}
          </p>
          <div className="flex items-center gap-2 flex-wrap">
            {registeredProfiles.length > 0 && (
              <SimpleSelect
                value={deployProfile}
                onChange={setDeployProfile}
                // '' is a REAL choice here ("deploy with the registry default"),
                // not a placeholder header — `clearLabel` is SimpleSelect's
                // channel for a selectable empty row.
                clearLabel={defaultProfile ? `profile: ${defaultProfile} (default)` : 'profile: default'}
                options={pickableProfiles.map((p) => p.name)}
                optionLabels={pickableProfiles.map((p) => `${i18nT('components.webAppArtifactCard.profile_2')} ${p.name}`)}
                aria-label={i18nT('components.webAppArtifactCard.aws_profile_to_deploy_with')}
              />
            )}
            <button
              type="button"
              onClick={openDeployChat}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md text-[13px] font-medium bg-accent text-accent-fg hover:bg-accent-hover cursor-pointer transition-all border-none"
              title={i18nT('components.webAppArtifactCard.deploy_this_app_to_your_aws_account')}
              aria-label={i18nT('components.webAppArtifactCard.deploy')}
            >
              <Rocket size={14} aria-hidden="true" />
              {i18nT('components.webAppArtifactCard.deploy')}
            </button>
            <span className="text-[10px] text-muted">
              {registeredProfiles.length > 0
                ? i18nT('components.webAppArtifactCard.opens_a_new_chat_session_to_run_the_deploy')
                : i18nT('components.webAppArtifactCard.opens_a_new_chat_session_to_run_the_deploy_add_a')}
            </span>
          </div>
        </div>

        {previewBase && (
          <div className="rounded-xl border border-border overflow-hidden bg-card">
            <div className="flex items-center gap-2.5 px-3 py-2 bg-bg-elevated border-b border-border">
              <ChromeDots />
              <div className="flex-1 min-w-0 flex items-center gap-1.5 px-2.5 py-1 rounded-md bg-card border border-border">
                <Globe size={12} className="text-muted shrink-0" aria-hidden="true" />
                <span className="text-[12px] text-muted truncate">{i18nT('components.webAppArtifactCard.local_preview_not_deployed_yet')}</span>
              </div>
            </div>
            <LocalAppFrame base={previewBase} slug={artifact.slug} />
          </div>
        )}
        {(architecture.frontend || architecture.backend || architecture.state) && (
          <div className="rounded-xl border border-border bg-card p-4">
            <div className="text-[12px] text-muted font-medium mb-2">{i18nT('components.webAppArtifactCard.will_provision')}</div>
            <ArchitectureRows architecture={architecture} />
          </div>
        )}
        {cost.estimates.length > 0 && (
          <div className="rounded-xl border border-border bg-card p-4">
            <CostPills
              cost={cost}
              label={cost.model === 'ttl-window'
                ? i18nT('components.webAppArtifactCard.estimated_cost_once_deployed_over', { hours: cost.window_hours })
                : i18nT('components.webAppArtifactCard.estimated_cost_once_deployed_monthly')}
            />
          </div>
        )}
      </div>
    )
  }

  // ------------------------------------------------------- deployed / expired
  return (
    <div className="space-y-4">
      {/* Browser-framed hero: chrome bar (URL + actions) over the live site
          preview. Expired deployments show a tombstone instead — a dead URL
          must never render as something that looks alive. */}
      <div className="rounded-xl border border-border overflow-hidden bg-card">
        <div className="flex items-center gap-2.5 px-3 py-2 bg-bg-elevated border-b border-border">
          <ChromeDots />
          <div className="flex-1 min-w-0 flex items-center gap-1.5 px-2.5 py-1 rounded-md bg-card border border-border">
            {isExpired ? (
              <>
                <CloudOff size={12} className="text-muted shrink-0" aria-hidden="true" />
                <span className="text-[12px] text-muted truncate">
                  {i18nT('components.webAppArtifactCard.deployment_torn_down_infrastructure_is_removed_b')}
                </span>
              </>
            ) : (
              <>
                <Globe size={12} className="text-muted shrink-0" aria-hidden="true" />
                {safeUrl ? (
                  <a
                    href={safeUrl}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-[12px] text-accent hover:underline truncate"
                  >
                    {deploy_target.public_url}
                  </a>
                ) : (
                  <span className="text-[12px] text-muted truncate" title={i18nT('components.webAppArtifactCard.non_http_s_url_blocked')}>
                    {deploy_target.public_url}
                  </span>
                )}
              </>
            )}
          </div>
          {!isExpired && (
            <>
              <button
                type="button"
                onClick={handleCopy}
                className="p-1 rounded text-muted hover:text-text transition-colors cursor-pointer bg-transparent border-none shrink-0"
                title={i18nT('components.webAppArtifactCard.copy_url')}
                aria-label={i18nT('components.webAppArtifactCard.copy_url')}
              >
                <Copy className="lucide-inline" />
              </button>
              {safeUrl && (
                <a
                  href={safeUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="p-1 rounded text-muted hover:text-text transition-colors shrink-0"
                  title={i18nT('components.webAppArtifactCard.open_in_new_tab')}
                  aria-label={i18nT('components.webAppArtifactCard.open_in_new_tab')}
                >
                  <ExternalLink className="lucide-inline" />
                </a>
              )}
            </>
          )}
        </div>

        {previewBase ? (
          <LocalAppFrame base={previewBase} slug={artifact.slug} />
        ) : isExpired ? (
          <div className="flex flex-col items-center justify-center gap-3 py-10 bg-bg-elevated/50">
            <CloudOff size={28} className="text-muted" aria-hidden="true" />
            <div className="text-sm text-muted">{i18nT('components.webAppArtifactCard.this_deployment_has_expired')}</div>
          </div>
        ) : isDeploying ? (
          <div className="flex flex-col items-center justify-center gap-2 py-10 bg-bg-elevated/50">
            <Cloud size={28} className="text-warn animate-pulse" aria-hidden="true" />
            <div className="text-sm text-muted">{i18nT('components.webAppArtifactCard.deploying')}</div>
          </div>
        ) : frameUrl ? (
          <LiveSiteFrame url={frameUrl} slug={artifact.slug} />
        ) : (
          <div className="flex flex-col items-center justify-center gap-2 py-10 bg-bg-elevated/50">
            <Globe size={28} className="text-muted" aria-hidden="true" />
            <div className="text-[12px] text-muted">{i18nT('components.webAppArtifactCard.preview_unavailable_for_this_host_open_the_link')}</div>
          </div>
        )}
      </div>

      {/* Status strip: identity + state + target pills */}
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-2 min-w-0">
          <Cloud className="lucide-inline text-accent shrink-0" aria-hidden="true" />
          <span className="font-semibold text-text-strong truncate">{artifact.slug}</span>
          <span className="text-sm text-muted shrink-0">{tierSummary}</span>
        </div>
        <div className="flex items-center gap-2.5 flex-wrap">
          <TargetPills dt={deploy_target} />
          <Badge variant={statusBadgeVariant(isExpired ? 'expired' : lifecycle.status)}>
            {isExpired ? i18nT('components.webAppArtifactCard.expired') : lifecycle.status}
          </Badge>
        </div>
      </div>

      {/* Info panels: architecture | lifecycle + cost */}
      <div className="grid gap-3 sm:grid-cols-2">
        <div className="rounded-xl border border-border bg-card p-4">
          <div className="text-[12px] text-muted font-medium mb-2">{i18nT('components.webAppArtifactCard.architecture')}</div>
          <ArchitectureRows architecture={architecture} />
        </div>
        <div className="rounded-xl border border-border bg-card p-4 space-y-4">
          {/* An expired card renders no countdown at all — legacy tombstones
              may still carry a stale expires_at, and a ticking clock on a
              dead deployment reads as alive. */}
          {!isExpired && (
            <div>
              <div className="text-[12px] text-muted font-medium mb-1.5">{i18nT('components.webAppArtifactCard.time_to_live')}</div>
              <div className="text-sm text-text-strong">
                {countdown.startsWith('\u221e') ? (
                  <span className="inline-flex items-center gap-1"><Infinity size={14} aria-label={i18nT('components.webAppArtifactCard.persistent')} /> {i18nT('components.webAppArtifactCard.persistent')}</span>
                ) : countdown}
              </div>
              {!lifecycle.persistent && lifecycle.expires_at && (
                <>
                  <div className="mt-1.5 h-1.5 rounded-full bg-bg-elevated overflow-hidden">
                    <div
                      className="h-full rounded-full bg-accent transition-all"
                      style={{ width: `${progress}%` }}
                    />
                  </div>
                  <div className="text-[11px] text-muted mt-1">
                    {i18nT('components.webAppArtifactCard.expires')} {lifecycle.expires_at} {i18nT('components.webAppArtifactCard.then_auto_reaped_tombstone')}
                  </div>
                </>
              )}
            </div>
          )}
          <CostPills cost={cost} label={costLabel} />
        </div>
      </div>

      {/* Footer */}
      <div className="flex items-center justify-between pt-2 border-t border-border">
        <div className="text-[12px] text-muted">
          {i18nT('components.webAppArtifactCard.generated_in')} {origin_session}
        </div>
        <div className="flex items-center gap-2">
          {isExpired && (
            <button
              type="button"
              onClick={openDeployChat}
              className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-md text-[12px] font-medium border border-accent/40 text-accent hover:bg-accent/10 cursor-pointer transition-all bg-transparent"
              title={i18nT('components.webAppArtifactCard.redeploy_this_app_opens_a_fresh_deploy_session_s')}
              aria-label={i18nT('components.webAppArtifactCard.redeploy')}
            >
              <Rocket size={12} aria-hidden="true" />
              {i18nT('components.webAppArtifactCard.redeploy')}
            </button>
          )}
          <button
            type="button"
            onClick={handleTeardown}
            disabled={teardownMut.isPending || isExpired}
            className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-md text-[12px] font-medium border border-danger/40 text-danger hover:bg-danger/10 cursor-pointer transition-all disabled:opacity-40 disabled:cursor-not-allowed bg-transparent"
            title={i18nT('components.webAppArtifactCard.cancel_tear_down_marks_the_deployment_expired_in')}
            aria-label={i18nT('components.webAppArtifactCard.cancel_tear_down')}
          >
            <Trash2 className="lucide-inline" />
            {teardownMut.isPending ? i18nT('components.webAppArtifactCard.tearing_down') : i18nT('components.webAppArtifactCard.cancel_tear_down')}
          </button>
          <span className="text-[10px] text-muted">{i18nT('components.webAppArtifactCard.owner_only_confirm_gated')}</span>
        </div>
      </div>

      {teardownMut.error && (
        <div className="px-3 py-2 rounded-md border border-danger/40 bg-danger-subtle text-[13px] text-danger">
          <strong>{i18nT('components.webAppArtifactCard.teardown_failed')}</strong>{' '}
          {teardownMut.error instanceof Error
            ? teardownMut.error.message
            : String(teardownMut.error)}
        </div>
      )}
    </div>
  )
}
