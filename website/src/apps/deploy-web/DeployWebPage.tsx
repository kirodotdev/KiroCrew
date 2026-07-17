import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Globe, Check, AlertTriangle, XCircle, ShieldCheck, Trash2, Eraser, ExternalLink, RefreshCw, Lock, ChevronDown, ChevronRight, Copy } from 'lucide-react'
import { api } from '../../api/client'
import Clickable from '../../components/Clickable'

// Mirrors deploy_web/handlers.py response shapes. deploy/recall/destroy use a
// two-call confirm-gate (200 + requires_confirm) and the deploy path adds a
// 409 pre-publish scan-block — both are normal control flow, surfaced as
// { status, data } by the api client rather than thrown.
interface Site { site_id: string; bucket: string; distribution_id: string }

// Response shapes from deploy_web/handlers.py + iam.py.
interface VerifyResult {
  reachable: boolean
  account?: string
  note?: string
  s3_reachable?: boolean
  cloudfront_reachable?: boolean
  detail?: string
}
// 200 + requires_confirm on the deploy path.
interface DeployPreview {
  requires_confirm: true
  public?: boolean
  site_id: string
  bytes: number
  scan: string
  message: string
}
// 409 pre-publish scan block.
interface ScanBlock {
  blocked: true
  reason: 'scan'
  findings: string
  count: number
}
// 200 deploy success.
interface DeployResult {
  url: string
  status: string
  reused: boolean
}
// 200 + requires_confirm on recall/destroy.
interface SiteActionConfirm {
  requires_confirm: true
  action: 'recall' | 'destroy'
  site_id: string
  message: string
}

const errMessage = (e: unknown, fallback: string) =>
  (e instanceof Error ? e.message : undefined) || fallback

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false)
  return (
    <button
      className="text-[10px] px-2 py-0.5 rounded bg-bg-elevated inline-flex items-center gap-1"
      onClick={() => { navigator.clipboard?.writeText(text); setCopied(true); setTimeout(() => setCopied(false), 1500) }}
    ><Copy size={10} /> {copied ? 'Copied!' : 'Copy'}</button>
  )
}

// ── Setup: AWS profile + region, reachability check, IAM policy helper ──────
function SetupCard() {
  const qc = useQueryClient()
  const { data: config } = useQuery({ queryKey: ['deploy-web-config'], queryFn: () => api.deployWebConfig() })
  const [profile, setProfile] = useState<string | null>(null)
  const [region, setRegion] = useState<string | null>(null)
  const [verifyResult, setVerifyResult] = useState<VerifyResult | null>(null)
  const [showPolicy, setShowPolicy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const profileVal = profile ?? config?.profile ?? ''
  const regionVal = region ?? config?.region ?? 'us-west-2'

  const { data: policy } = useQuery({
    queryKey: ['deploy-web-iam-policy'],
    queryFn: () => api.deployWebIamPolicy(),
    enabled: showPolicy,
  })

  const saveMut = useMutation({
    mutationFn: () => api.deployWebSaveConfig({ profile: profileVal.trim(), region: regionVal.trim() }),
    onSuccess: () => { setError(null); qc.invalidateQueries({ queryKey: ['deploy-web-config'] }); qc.invalidateQueries({ queryKey: ['deploy-web-sites'] }) },
    onError: (e: unknown) => setError(errMessage(e, 'Failed to save config')),
  })

  const verifyMut = useMutation({
    mutationFn: () => api.deployWebVerify(),
    onSuccess: (r) => setVerifyResult(r.data as VerifyResult),
    onError: (e: unknown) => setVerifyResult({ reachable: false, note: errMessage(e, 'Verification failed') }),
  })

  return (
    <div className="border border-border rounded-md p-4 bg-card mb-4">
      <div className="text-sm font-medium flex items-center gap-2 mb-1"><ShieldCheck size={15} /> Setup</div>
      <div className="text-xs text-muted mb-3">
        Bring your own AWS account. KiroCrew never stores credentials — only the profile <em>name</em>, resolved by your local AWS CLI.
      </div>
      <div className="flex flex-wrap items-end gap-3">
        <label htmlFor="deploy-web-profile" className="text-sm">
          <div className="text-xs text-muted mb-0.5">AWS profile</div>
          <input id="deploy-web-profile" aria-label="AWS profile" className="text-sm p-1.5 rounded bg-bg border border-border w-48" placeholder="my-profile" value={profileVal} onChange={e => setProfile(e.target.value)} />
        </label>
        <label htmlFor="deploy-web-region" className="text-sm">
          <div className="text-xs text-muted mb-0.5">Region</div>
          <input id="deploy-web-region" aria-label="Region" className="text-sm p-1.5 rounded bg-bg border border-border w-36" placeholder="us-west-2" value={regionVal} onChange={e => setRegion(e.target.value)} />
        </label>
        <button className="text-sm px-3 py-1.5 rounded-md bg-accent text-white disabled:opacity-50" disabled={saveMut.isPending || !profileVal.trim()} onClick={() => saveMut.mutate()}>{saveMut.isPending ? 'Saving…' : 'Save'}</button>
        <button className="text-sm px-3 py-1.5 rounded-md bg-bg-elevated disabled:opacity-50" disabled={verifyMut.isPending || !config?.profile} onClick={() => verifyMut.mutate()}>{verifyMut.isPending ? 'Checking…' : 'Verify access'}</button>
      </div>
      {error && <div className="text-xs text-danger mt-2 flex items-center gap-1"><XCircle size={12} /> {error}</div>}
      {verifyResult && (
        <div className={`text-xs mt-3 flex items-start gap-1 ${verifyResult.reachable ? 'text-ok' : 'text-warn'}`}>
          {verifyResult.reachable ? <Check size={12} className="mt-0.5" /> : <AlertTriangle size={12} className="mt-0.5" />}
          <div>
            {verifyResult.reachable ? `Reachable${verifyResult.account ? ` (account ${verifyResult.account})` : ''}` : 'Not reachable'}
            {verifyResult.note && <div className="text-muted">{verifyResult.note}</div>}
          </div>
        </div>
      )}
      <div className="mt-3">
        <Clickable className="text-xs text-accent inline-flex items-center gap-1" onClick={() => setShowPolicy(v => !v)}>
          {showPolicy ? <ChevronDown size={12} /> : <ChevronRight size={12} />} Required IAM policy
        </Clickable>
        {showPolicy && (
          <div className="mt-2">
            <div className="flex items-center justify-between mb-1">
              <div className="text-xs text-muted">Attach this least-privilege policy to your profile's principal.</div>
              {policy?.policy && <CopyButton text={policy.policy} />}
            </div>
            <pre className="text-[11px] p-2 rounded bg-bg border border-border overflow-auto max-h-64">{policy?.policy ?? 'Loading…'}</pre>
          </div>
        )}
      </div>
    </div>
  )
}

// ── Publish: site_id + artifact_slug | local_dir, with scan + confirm gates ─
function PublishCard({ configured }: { configured: boolean }) {
  const qc = useQueryClient()
  const [siteId, setSiteId] = useState('')
  const [sourceKind, setSourceKind] = useState<'artifact' | 'local'>('artifact')
  const [artifactSlug, setArtifactSlug] = useState('')
  const [localDir, setLocalDir] = useState('')
  const [overrideScan, setOverrideScan] = useState(false)
  const [preview, setPreview] = useState<DeployPreview | null>(null)   // requires_confirm payload
  const [scanBlock, setScanBlock] = useState<ScanBlock | null>(null)  // 409 scan payload
  const [result, setResult] = useState<DeployResult | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const reset = () => { setPreview(null); setScanBlock(null); setResult(null); setError(null) }

  const buildBody = (extra: object = {}) => ({
    site_id: siteId.trim(),
    ...(sourceKind === 'artifact' ? { artifact_slug: artifactSlug.trim() } : { local_dir: localDir.trim() }),
    ...(overrideScan ? { override_scan: true } : {}),
    ...extra,
  })

  const run = async (extra: object = {}) => {
    setBusy(true); setError(null)
    try {
      const { status, data } = await api.deployWebDeploy(buildBody(extra))
      if (status === 409 && data?.reason === 'scan') { setScanBlock(data as ScanBlock); setPreview(null); setResult(null) }
      else if (status === 200 && data?.requires_confirm) { setPreview(data as DeployPreview); setScanBlock(null); setResult(null) }
      else if (status === 200) { setResult(data as DeployResult); setPreview(null); setScanBlock(null); qc.invalidateQueries({ queryKey: ['deploy-web-sites'] }) }
      else { setError(data?.error || `Deploy failed (HTTP ${status})`) }
    } catch (e: unknown) {
      setError(errMessage(e, 'Deploy failed'))
    } finally { setBusy(false) }
  }

  const canSubmit = configured && siteId.trim().length > 0 && (sourceKind === 'artifact' ? artifactSlug.trim().length > 0 : localDir.trim().length > 0)

  return (
    <div className="border border-border rounded-md p-4 bg-card mb-4">
      <div className="text-sm font-medium flex items-center gap-2 mb-1"><Globe size={15} /> Publish</div>
      <div className="text-xs text-muted mb-3">Publishes to a <strong>public</strong> HTTPS URL (private S3 + CloudFront + OAC) on your own AWS account.</div>
      {!configured && <div className="text-xs text-warn mb-3 flex items-center gap-1"><AlertTriangle size={12} /> Set an AWS profile in Setup first.</div>}
      <div className="space-y-3">
        <label htmlFor="deploy-web-site-id" className="text-sm block">
          <div className="text-xs text-muted mb-0.5">Site ID</div>
          <input id="deploy-web-site-id" aria-label="Site ID" className="text-sm p-1.5 rounded bg-bg border border-border w-64" placeholder="my-demo" value={siteId} onChange={e => { setSiteId(e.target.value); reset() }} />
        </label>
        <div className="flex items-center gap-4 text-sm">
          <label htmlFor="deploy-web-source-artifact" className="flex items-center gap-1.5"><input id="deploy-web-source-artifact" aria-label="Artifact" type="radio" checked={sourceKind === 'artifact'} onChange={() => { setSourceKind('artifact'); reset() }} /> Artifact</label>
          <label htmlFor="deploy-web-source-local" className="flex items-center gap-1.5"><input id="deploy-web-source-local" aria-label="Local directory" type="radio" checked={sourceKind === 'local'} onChange={() => { setSourceKind('local'); reset() }} /> Local directory</label>
        </div>
        {sourceKind === 'artifact'
          ? <input aria-label="Artifact slug" className="text-sm p-1.5 rounded bg-bg border border-border w-full" placeholder="artifact slug (e.g. my-dashboard)" value={artifactSlug} onChange={e => { setArtifactSlug(e.target.value); reset() }} />
          : <input aria-label="Local directory path" className="text-sm p-1.5 rounded bg-bg border border-border w-full" placeholder="/path/to/site (must be within home / a workspace dir)" value={localDir} onChange={e => { setLocalDir(e.target.value); reset() }} />}
      </div>

      {/* Pre-publish scan gate (409) */}
      {scanBlock && (
        <div className="p-3 rounded-md mt-3 border border-warn bg-warn/10">
          <div className="text-sm font-medium text-warn flex items-center gap-1"><AlertTriangle size={14} /> Pre-publish scan found {scanBlock.count} issue(s)</div>
          <pre className="text-[11px] mt-1 whitespace-pre-wrap">{scanBlock.findings}</pre>
          <label htmlFor="deploy-web-override-scan" className="flex items-center gap-2 text-xs mt-2">
            <input id="deploy-web-override-scan" aria-label="Publish anyway" type="checkbox" checked={overrideScan} onChange={e => setOverrideScan(e.target.checked)} /> Publish anyway (I've reviewed these)
          </label>
          <button className="text-xs px-2 py-1 mt-2 rounded bg-accent text-white disabled:opacity-50" disabled={!overrideScan || busy} onClick={() => run()}>Re-run with override</button>
        </div>
      )}

      {/* Confirm gate (200 + requires_confirm) */}
      {preview && (
        <div className="p-3 rounded-md mt-3 border border-accent bg-accent/5">
          <div className="text-sm font-medium flex items-center gap-1"><Lock size={14} /> Confirm public publish</div>
          <div className="text-xs mt-1 text-muted">{preview.message}</div>
          <div className="text-xs mt-1">Site <code>{preview.site_id}</code> · {preview.bytes} bytes · scan: {preview.scan}</div>
          <div className="flex gap-2 mt-2">
            <button className="text-xs px-2 py-1 rounded bg-accent text-white disabled:opacity-50" disabled={busy} onClick={() => run({ confirm: true })}>{busy ? 'Publishing…' : 'Confirm & publish'}</button>
            <button className="text-xs px-2 py-1 rounded bg-bg-elevated" onClick={reset}>Cancel</button>
          </div>
        </div>
      )}

      {/* Success */}
      {result?.url && (
        <div className="p-3 rounded-md mt-3 border border-ok bg-ok/10">
          <div className="text-sm font-medium text-ok flex items-center gap-1"><Check size={14} /> {result.reused ? 'Updated' : 'Published'}</div>
          <a href={result.url} target="_blank" rel="noopener noreferrer" className="text-xs text-accent inline-flex items-center gap-1 mt-1">{result.url} <ExternalLink size={10} /></a>
          <div className="text-[11px] text-muted mt-1">Status: {result.status} · CloudFront can take a few minutes to finish deploying.</div>
        </div>
      )}

      {error && <div className="text-xs text-danger mt-3 flex items-center gap-1"><XCircle size={12} /> {error}</div>}

      {!preview && !scanBlock && (
        <button className="text-sm px-3 py-1.5 mt-3 rounded-md bg-accent text-white disabled:opacity-50" disabled={!canSubmit || busy} onClick={() => run()}>{busy ? 'Working…' : 'Publish'}</button>
      )}
    </div>
  )
}

// ── Site row: recall (empty, reversible) + destroy (delete infra) ───────────
function SiteRow({ site }: { site: Site }) {
  const qc = useQueryClient()
  const [pending, setPending] = useState<{ action: 'recall' | 'destroy'; data: SiteActionConfirm } | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState<string | null>(null)

  const call = async (action: 'recall' | 'destroy', confirm: boolean) => {
    setBusy(true); setError(null)
    try {
      const fn = action === 'recall' ? api.deployWebRecall : api.deployWebDestroy
      const { status, data } = await fn({ site_id: site.site_id, ...(confirm ? { confirm: true } : {}) })
      if (status === 200 && data?.requires_confirm) setPending({ action, data: data as SiteActionConfirm })
      else if (status === 200) { setDone(action === 'recall' ? 'Recalled' : 'Destroyed'); setPending(null); qc.invalidateQueries({ queryKey: ['deploy-web-sites'] }) }
      else setError(data?.error || `Failed (HTTP ${status})`)
    } catch (e: unknown) { setError(errMessage(e, 'Request failed')) } finally { setBusy(false) }
  }

  return (
    <div className="border border-border rounded-md p-3 bg-card mb-2">
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-sm font-medium">{site.site_id}</span>
        {done && <span className="text-xs text-ok flex items-center gap-1"><Check size={11} /> {done}</span>}
        <div className="ml-auto flex gap-2">
          <button className="text-xs px-2 py-1 rounded bg-bg-elevated inline-flex items-center gap-1 disabled:opacity-50" disabled={busy} onClick={() => call('recall', false)}><Eraser size={11} /> Recall</button>
          <button className="text-xs px-2 py-1 rounded bg-bg-elevated text-danger inline-flex items-center gap-1 disabled:opacity-50" disabled={busy} onClick={() => call('destroy', false)}><Trash2 size={11} /> Destroy</button>
        </div>
      </div>
      <div className="text-[11px] text-muted mt-1">bucket: {site.bucket || '—'} · distribution: {site.distribution_id || '—'}</div>

      {pending && (
        <div className={`p-2 rounded mt-2 border ${pending.action === 'destroy' ? 'border-danger bg-danger/10' : 'border-warn bg-warn/10'}`}>
          <div className={`text-xs ${pending.action === 'destroy' ? 'text-danger' : 'text-warn'}`}>{pending.data.message}</div>
          <div className="flex gap-2 mt-2">
            <button className={`text-xs px-2 py-1 rounded text-white disabled:opacity-50 ${pending.action === 'destroy' ? 'bg-danger' : 'bg-accent'}`} disabled={busy} onClick={() => call(pending.action, true)}>{busy ? 'Working…' : `Confirm ${pending.action}`}</button>
            <button className="text-xs px-2 py-1 rounded bg-bg-elevated" onClick={() => setPending(null)}>Cancel</button>
          </div>
        </div>
      )}
      {error && <div className="text-xs text-danger mt-1 flex items-center gap-1"><XCircle size={11} /> {error}</div>}
    </div>
  )
}

export default function DeployWebPage() {
  const qc = useQueryClient()
  const { data: sitesData, isLoading } = useQuery({ queryKey: ['deploy-web-sites'], queryFn: () => api.deployWebSites() })
  const configured = !!sitesData?.configured
  const sites = sitesData?.sites ?? []

  return (
    <div className="px-6 py-4 max-w-3xl mx-auto">
      <div className="flex items-center justify-between mb-4">
        <h1 className="text-lg font-semibold flex items-center gap-2"><Globe size={20} /> Web Deploy</h1>
        <button className="text-xs px-2 py-1 rounded bg-bg-elevated inline-flex items-center gap-1" onClick={() => qc.invalidateQueries({ queryKey: ['deploy-web-sites'] })}><RefreshCw size={12} /> Refresh</button>
      </div>

      <SetupCard />
      <PublishCard configured={configured} />

      <div className="border border-border rounded-md p-4 bg-card">
        <div className="text-sm font-medium mb-2">Published sites</div>
        {isLoading ? <div className="text-sm text-muted">Loading…</div>
          : !configured ? <div className="text-sm text-muted">Configure an AWS profile to list your sites.</div>
          : sites.length === 0 ? <div className="text-sm text-muted">No sites published yet.</div>
          : sites.map(s => <SiteRow key={s.site_id} site={s} />)}
      </div>
    </div>
  )
}
