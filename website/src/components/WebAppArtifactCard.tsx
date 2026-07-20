import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Cloud, Copy, ExternalLink, Infinity, Rocket, Trash2 } from 'lucide-react'
import { api } from '../api/client'
import { Badge } from '../components/ui'
import { safeHttpUrl } from '../lib/safeUrl'
import type { Artifact } from '../types'

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
  if (persistent) return '\u221e persistent'
  if (!expiresAt) return 'no expiry set'
  const diff = new Date(expiresAt).getTime() - Date.now()
  if (Number.isNaN(diff)) return 'no expiry set'
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
    if (arch.state) parts.push('Stateful app')
    else if (arch.backend) parts.push('API app')
    else parts.push('Static app')
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
    return <div className="text-muted text-sm p-4">No app metadata available.</div>
  }

  const { deploy_target, architecture, lifecycle, cost, origin_session } = meta
  const safeUrl = safeHttpUrl(deploy_target.public_url)
  const isExpired = lifecycle.status === 'expired' || teardownMut.isSuccess || countdown === 'expired'
  const isDeploying = lifecycle.status === 'deploying'
  // Not deployed yet: no live http(s) URL, not expired, not mid-deploy → show the
  // Deploy affordance instead of the infra control card (artifact-first model —
  // the app artifact exists before any deploy).
  const notDeployed = !deploy_target.public_url && !isExpired && !isDeploying
  const costLabel = cost.model === 'ttl-window'
    ? `Estimated cost \u2014 over ${cost.window_hours}h TTL window`
    : 'Estimated monthly cost'

  const handleTeardown = () => {
    const resourceList = architecture.resources
      .map((r: { type: string; id: string }) => `  ${r.type}: ${r.id}`)
      .join('\n')
    const msg = `This will tear down the deployed application and delete these resources:\n\n${resourceList}\n\nThis action is not reversible. Continue?`
    if (!window.confirm(msg)) return
    teardownMut.mutate()
  }

  if (notDeployed) {
    return (
      <div className="space-y-4">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Cloud className="lucide-inline text-accent" />
            <span className="font-semibold text-text-strong">{artifact.slug}</span>
            {tierSummary && <span className="text-sm text-muted">{tierSummary}</span>}
          </div>
          <Badge variant="aim">Not deployed</Badge>
        </div>
        <p className="text-sm text-muted">
          Not deployed yet &mdash; deploy to your own AWS account for a global public link. Deploying requires an AWS profile.
        </p>
        {(architecture.frontend || architecture.backend || architecture.state) && (
          <div>
            <div className="text-[12px] uppercase tracking-wide text-muted font-medium mb-1">Will provision</div>
            <div className="text-sm text-text-strong space-y-0.5">
              {architecture.frontend && <div>Frontend: {architecture.frontend}</div>}
              {architecture.backend && <div>Backend: {architecture.backend}</div>}
              {architecture.state && <div>State: {architecture.state}</div>}
            </div>
          </div>
        )}
        {cost.estimates.length > 0 && (
          <div>
            <div className="text-[12px] uppercase tracking-wide text-muted font-medium mb-1">
              Estimated cost once deployed {cost.model === 'ttl-window' ? `(over ${cost.window_hours}h)` : '(monthly)'}
            </div>
            <div className="flex flex-wrap gap-3 text-sm text-text-strong">
              {cost.estimates.map((e: { views: number; usd: number }) => (
                <span key={e.views}>{e.views.toLocaleString()} views: ${e.usd.toFixed(4)}</span>
              ))}
            </div>
            <div className="text-[11px] text-muted mt-1">{cost.note} &middot; idle &asymp; ${cost.idle_usd}</div>
          </div>
        )}
        <div className="flex items-center gap-2 pt-2 border-t border-border flex-wrap">
          {registeredProfiles.length > 0 && (
            <select
              value={deployProfile}
              onChange={(e) => setDeployProfile(e.target.value)}
              aria-label="AWS profile to deploy with"
              className="px-2 py-1.5 rounded-md text-[12px] bg-bg-elevated border border-border text-text cursor-pointer"
            >
              <option value="">
                {defaultProfile ? `profile: ${defaultProfile} (default)` : 'profile: default'}
              </option>
              {registeredProfiles.filter((p) => p.name !== defaultProfile).map((p) => (
                <option key={p.name} value={p.name}>profile: {p.name}</option>
              ))}
            </select>
          )}
          <button
            type="button"
            onClick={openDeployChat}
            className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-md text-[12px] font-medium border border-accent/40 text-accent hover:bg-accent/10 cursor-pointer transition-all bg-transparent"
            title="Deploy this app to your AWS account"
            aria-label="Deploy"
          >
            <Rocket className="lucide-inline" />
            Deploy
          </button>
          <span className="text-[10px] text-muted">
            {registeredProfiles.length > 0
              ? 'opens a new chat session to run the deploy'
              : 'opens a new chat session to run the deploy — add a profile in Artifact Deploy first'}
          </span>
        </div>
      </div>
    )
  }

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Cloud className="lucide-inline text-accent" />
          <span className="font-semibold text-text-strong">{artifact.slug}</span>
          <span className="text-sm text-muted">{tierSummary}</span>
        </div>
        <Badge variant={statusBadgeVariant(isExpired ? 'expired' : lifecycle.status)}>
          {isExpired ? 'Expired' : lifecycle.status}
        </Badge>
      </div>

      {/* Public link — FU-6: an expired/torn-down deployment must not render a
          live-looking link; the URL is dead (or already cleared server-side). */}
      <div className="rounded-lg border border-border bg-bg-elevated p-3">
        <div className="flex items-center gap-2 flex-wrap">
          {isExpired ? (
            <span className="text-sm text-muted">
              Deployment torn down &mdash; infrastructure is removed by the in-account reaper.
            </span>
          ) : safeUrl ? (
            <a
              href={safeUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="text-sm text-accent hover:underline break-all"
            >
              {deploy_target.public_url}
            </a>
          ) : (
            <span
              className="text-sm text-muted break-all"
              title="Non-http(s) URL blocked"
            >
              {deploy_target.public_url}
            </span>
          )}
          <button
            type="button"
            onClick={handleCopy}
            className="p-1 rounded text-muted hover:text-text transition-colors cursor-pointer bg-transparent border-none"
            title="Copy URL"
            aria-label="Copy URL"
          >
            <Copy className="lucide-inline" />
          </button>
          {safeUrl && (
            <a
              href={safeUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="p-1 rounded text-muted hover:text-text transition-colors"
              title="Open in new tab"
              aria-label="Open in new tab"
            >
              <ExternalLink className="lucide-inline" />
            </a>
          )}
        </div>
        <div className="flex gap-2 mt-2 flex-wrap">
          <span className="text-[11px] px-1.5 py-0.5 rounded bg-card border border-border text-muted uppercase">
            {deploy_target.provider}
          </span>
          <span className="text-[11px] px-1.5 py-0.5 rounded bg-card border border-border text-muted">
            acct {deploy_target.account}
          </span>
          <span className="text-[11px] px-1.5 py-0.5 rounded bg-card border border-border text-muted">
            {deploy_target.region}
          </span>
          {deploy_target.profile && (
            <span className="text-[11px] px-1.5 py-0.5 rounded bg-card border border-border text-muted">
              profile {deploy_target.profile}
            </span>
          )}
        </div>
      </div>

      {/* Architecture */}
      <div>
        <div className="text-[12px] uppercase tracking-wide text-muted font-medium mb-1">Architecture</div>
        <div className="space-y-1">
          {architecture.frontend && (
            <div className="text-sm text-text">
              <span className="text-muted mr-1.5">Frontend:</span>
              {architecture.frontend}
              {architecture.resources.find((r: { type: string; id: string }) => r.type === 'frontend') && (
                <code className="ml-2 text-[11px] text-muted">{architecture.resources.find((r: { type: string; id: string }) => r.type === 'frontend')!.id}</code>
              )}
            </div>
          )}
          {architecture.backend && (
            <div className="text-sm text-text">
              <span className="text-muted mr-1.5">Backend:</span>
              {architecture.backend}
              {architecture.resources.find((r: { type: string; id: string }) => r.type === 'backend') && (
                <code className="ml-2 text-[11px] text-muted">{architecture.resources.find((r: { type: string; id: string }) => r.type === 'backend')!.id}</code>
              )}
            </div>
          )}
          {architecture.state && (
            <div className="text-sm text-text">
              <span className="text-muted mr-1.5">State:</span>
              {architecture.state}
              {architecture.resources.find((r: { type: string; id: string }) => r.type === 'state') && (
                <code className="ml-2 text-[11px] text-muted">{architecture.resources.find((r: { type: string; id: string }) => r.type === 'state')!.id}</code>
              )}
            </div>
          )}
        </div>
      </div>

      {/* Estimated cost */}
      <div>
        <div className="text-[12px] uppercase tracking-wide text-muted font-medium mb-1">{costLabel}</div>
        <div className="flex flex-wrap gap-2">
          {cost.estimates.map((e: { views: number; usd: number }, i: number) => (
            <div key={i} className="rounded border border-border bg-bg-elevated px-2.5 py-1.5 text-center">
              <div className="text-[11px] text-muted">{Number(e.views ?? 0).toLocaleString()} views</div>
              <div className="text-sm font-medium text-text-strong">${Number(e.usd ?? 0).toFixed(4)}</div>
            </div>
          ))}
        </div>
        <div className="text-[11px] text-muted mt-1">
          {cost.note} &middot; idle &asymp; ${cost.idle_usd} &middot; billed to your account
        </div>
      </div>

      {/* TTL remaining */}
      <div>
        <div className="text-[12px] uppercase tracking-wide text-muted font-medium mb-1">TTL remaining</div>
        <div className="text-sm text-text-strong">
          {countdown.startsWith('\u221e') ? (
            <span className="inline-flex items-center gap-1"><Infinity size={14} aria-label="persistent" /> persistent</span>
          ) : countdown}
        </div>
        {!lifecycle.persistent && lifecycle.expires_at && !isExpired && (
          <>
            <div className="mt-1.5 h-1.5 rounded-full bg-bg-elevated overflow-hidden">
              <div
                className="h-full rounded-full bg-accent transition-all"
                style={{ width: `${progress}%` }}
              />
            </div>
            <div className="text-[11px] text-muted mt-1">
              expires {lifecycle.expires_at} &middot; then auto-reaped &rarr; tombstone
            </div>
          </>
        )}
      </div>

      {/* Footer */}
      <div className="flex items-center justify-between pt-2 border-t border-border">
        <div className="text-[12px] text-muted">
          Generated in {origin_session}
        </div>
        <div className="flex items-center gap-2">
          {isExpired && (
            <button
              type="button"
              onClick={openDeployChat}
              className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-md text-[12px] font-medium border border-accent/40 text-accent hover:bg-accent/10 cursor-pointer transition-all bg-transparent"
              title="Redeploy this app — opens a fresh deploy session (same flow as Deploy)"
              aria-label="Redeploy"
            >
              Redeploy
            </button>
          )}
          <button
            type="button"
            onClick={handleTeardown}
            disabled={teardownMut.isPending || isExpired}
            className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-md text-[12px] font-medium border border-danger/40 text-danger hover:bg-danger/10 cursor-pointer transition-all disabled:opacity-40 disabled:cursor-not-allowed bg-transparent"
            title="Cancel / Tear down — marks the deployment expired; infrastructure is removed by the in-account reaper or scripts/teardown.sh"
            aria-label="Cancel / Tear down"
          >
            <Trash2 className="lucide-inline" />
            {teardownMut.isPending ? 'Tearing down...' : 'Cancel / Tear down'}
          </button>
          <span className="text-[10px] text-muted">owner-only &middot; confirm-gated</span>
        </div>
      </div>

      {teardownMut.error && (
        <div className="px-3 py-2 rounded-md border border-danger/40 bg-danger-subtle text-[13px] text-danger">
          <strong>Teardown failed:</strong>{' '}
          {teardownMut.error instanceof Error
            ? teardownMut.error.message
            : String(teardownMut.error)}
        </div>
      )}
    </div>
  )
}
