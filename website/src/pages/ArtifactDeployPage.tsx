import { useState } from 'react'
import Clickable from '../components/Clickable'
import { Link, useNavigate } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, Globe, Copy, ExternalLink, RefreshCw, Trash2, Undo2, ShieldCheck, Terminal, ChevronDown, ChevronRight, Lock, CheckCircle, XCircle, Rocket, Plus, Star } from 'lucide-react'
import type { Artifact } from '../types'
import { PageHeader, Card, CardTitle, StatCard, Btn, Input, Toggle , Badge} from '../components/ui'
import StyledSelect from '../components/StyledSelect'
import InfoTip from '../components/InfoTip'
import { safeHttpUrl } from '../lib/safeUrl'

const BASE = '/api/deploy'

interface ProfileEntry { name: string; region: string; account: string; verified_at: string; note: string }
interface ProfilesResp { profiles: ProfileEntry[]; default: string; available: string[] }
interface Site { site_id: string; bucket: string; distribution_id: string; status?: string; url?: string; profile?: string }
interface Reach { reachable: boolean; account?: string; s3_reachable?: boolean; cloudfront_reachable?: boolean; note?: string; detail?: string; profile?: string; error?: string }

// Route all fetches through proper X-Session-Key header (client.ts pattern).
const _sk = { 'X-Session-Key': 'dashboard:ui' }
async function jget<T>(path: string): Promise<T> {
  const r = await fetch(BASE + path, { headers: { ..._sk } })
  return (await r.json()) as T
}
async function jsend<T>(path: string, body: unknown, method = 'POST'): Promise<{ status: number; data: T }> {
  const r = await fetch(BASE + path, { method, headers: { 'Content-Type': 'application/json', ..._sk }, body: JSON.stringify(body) })
  return { status: r.status, data: (await r.json()) as T }
}

const chip: React.CSSProperties = { background: 'var(--bg)', border: '1px solid var(--border)', color: 'var(--muted)', padding: '1px 7px', borderRadius: 9999, fontSize: 10.5, fontFamily: 'ui-monospace,Menlo,monospace' }
const cmd: React.CSSProperties = { background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 6, padding: '8px 10px', fontFamily: 'ui-monospace,Menlo,monospace', fontSize: 12, display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8 }
const label: React.CSSProperties = { fontSize: 11, color: 'var(--muted)', marginBottom: 4, display: 'block' }
const linkBtn: React.CSSProperties = { background: 'transparent', color: 'var(--accent)', border: '1px solid var(--accent-subtle)', padding: '6px 13px', borderRadius: 9999, fontSize: 12, fontWeight: 500, display: 'inline-flex', alignItems: 'center', gap: 5, textDecoration: 'none' }

// R16 F4: badge() helper removed — replaced with Badge component from ui.tsx

export default function ArtifactDeployPage() {
  const qc = useQueryClient()
  const [reach, setReach] = useState<Reach | null>(null)
  const [policy, setPolicy] = useState('')
  const [boundaryPolicy, setBoundaryPolicy] = useState('')
  const [boundaryNote, setBoundaryNote] = useState('')
  const [policyTier, setPolicyTier] = useState<'static' | 'fullstack'>('static')
  const [notice, setNotice] = useState<string | null>(null)
  const [showGuide, setShowGuide] = useState(true)
  const [showSecurity, setShowSecurity] = useState(false)
  const [showNewProfile, setShowNewProfile] = useState(false)
  const [npName, setNpName] = useState('')
  const [npRegion, setNpRegion] = useState('us-west-2')
  const [npAccount, setNpAccount] = useState('')
  const [npRole, setNpRole] = useState('')
  const [npCreate, setNpCreate] = useState(false)

  const { data: profilesResp } = useQuery<ProfilesResp>({
    queryKey: ['deploy-web', 'profiles'],
    queryFn: () => jget('/profiles'),
  })
  const profiles = profilesResp?.profiles || []
  const defaultProfile = profilesResp?.default || ''
  const availableProfiles = profilesResp?.available || []

  const { data: sitesResp } = useQuery<{ sites: Site[]; configured: boolean; profile_errors?: string[] }>({
    queryKey: ['deploy-web', 'sites'],
    queryFn: () => jget('/list'),
    refetchInterval: 30000,
  })
  const sites = sitesResp?.sites || []

  const { data: webappResp } = useQuery<{ artifacts: Artifact[] }>({
    queryKey: ['deploy-web', 'webapps'],
    queryFn: async () => {
      const r = await fetch('/api/artifacts?kind=webapp')
      return (await r.json()) as { artifacts: Artifact[] }
    },
    refetchInterval: 30000,
  })
  const webapps = (webappResp?.artifacts || []).filter((a) => a.webapp_metadata)
  const webappCost = (a: Artifact): number => {
    const est = a.webapp_metadata?.cost?.estimates || []
    return est.length ? Math.max(...est.map((e: { usd?: number }) => Number(e.usd) || 0)) : 0
  }
  const deployedWebapps = webapps.filter((a) => a.webapp_metadata?.deploy_target?.public_url)
  const draftWebapps = webapps.filter(
    (a) => !a.webapp_metadata?.deploy_target?.public_url && a.webapp_metadata?.lifecycle?.status !== 'expired')
  const navigate = useNavigate()
  const [draftProfiles, setDraftProfiles] = useState<Record<string, string>>({})
  const deployDraft = (slug: string) => {
    const chosen = draftProfiles[slug] || defaultProfile
    ;(window as unknown as { __mc_chat_launch?: { message: string; ts: number } }).__mc_chat_launch = {
      message:
        `Deploy the app artifact "${slug}" to my AWS account using the ` +
        `artifact-deploy skill: adapt it to the deploy contract, ship it, and give me the public link.` +
        (chosen ? ` Use the AWS profile "${chosen}".` : ''),
      ts: Date.now(),
    }
    navigate('/chat')
  }
  const totalWebappUsd = deployedWebapps.reduce((s, a) => s + webappCost(a), 0)

  const refreshProfiles = () => {
    qc.invalidateQueries({ queryKey: ['deploy-web', 'profiles'] })
    qc.invalidateQueries({ queryKey: ['deploy-web', 'sites'] })
  }
  const addProfile = useMutation({
    mutationFn: (p: { name: string; region: string; create?: boolean; account?: string; role?: string; default?: boolean }) =>
      jsend<{ error?: string }>('/profiles', p),
    onSuccess: ({ status, data }, p) => {
      if (status >= 400) { setNotice(`Error: ${data?.error || 'add failed'}`); return }
      setNotice(p.create ? `Created + registered profile '${p.name}'.` : `Registered profile '${p.name}'.`)
      setShowNewProfile(false); setNpName(''); setNpAccount(''); setNpRole(''); setNpCreate(false)
      refreshProfiles()
    },
  })
  const setDefaultProfile = useMutation({
    mutationFn: (name: string) => jsend<{ error?: string }>(`/profiles/${encodeURIComponent(name)}`, { default: true }, 'PUT'),
    onSuccess: ({ status, data }) => {
      if (status >= 400) { setNotice(`Error: ${data?.error || 'update failed'}`); return }
      refreshProfiles()
    },
  })
  const removeProfile = useMutation({
    mutationFn: (name: string) => jsend<{ error?: string }>(`/profiles/${encodeURIComponent(name)}`, {}, 'DELETE'),
    onSuccess: ({ status, data }) => {
      if (status >= 400) { setNotice(`Error: ${data?.error || 'remove failed'}`); return }
      setNotice('Removed from registry (your ~/.aws/config is untouched).')
      refreshProfiles()
    },
  })
  const verify = useMutation({
    mutationFn: (name: string) => jsend<Reach>('/verify', { profile: name }),
    onSuccess: ({ data }) => { setReach(data); refreshProfiles() },
  })

  const loadPolicyMut = useMutation({
    mutationFn: () => jget<{ policy: string; boundary_policy?: string; boundary_policy_name?: string; boundary_note?: string }>(`/iam-policy?tier=${policyTier}`),
    onSuccess: (data) => {
      setPolicy(data.policy)
      // R25 F2: fullstack also requires the permissions-boundary policy —
      // iam:CreateRole is conditioned on it, so first deploy fails without it.
      setBoundaryPolicy(data.boundary_policy || '')
      setBoundaryNote(data.boundary_note ? `${data.boundary_note} (name: ${data.boundary_policy_name || ''})` : '')
    },
  })

  const recallMut = useMutation({
    // R26 F1: two-call guard mirroring destroy (R25) — preview resolves the
    // LIVE resources, the dialog names them, and the confirmed call binds to
    // them so a recreated site is refused (409) instead of being emptied.
    mutationFn: async (s: Site) => {
      const prev = await jsend<any>('/recall', { site_id: s.site_id, profile: s.profile || '' })
      if (prev.status !== 200) throw new Error(prev.data?.error || `Recall preview failed (${prev.status})`)
      const r = prev.data.resources || {}
      const ok = window.confirm(`Recall '${s.site_id}'? Empties bucket ${r.bucket || '?'} (URL → 404, reversible). Edge caches may serve briefly; already-downloaded content can't be recalled.`)
      if (!ok) return { status: 0, data: { cancelled: true } }
      return jsend<any>('/recall', {
        site_id: s.site_id, confirm: true, profile: s.profile || '',
        expected_bucket: r.bucket || '', expected_distribution_id: r.distribution_id || '',
      })
    },
    onSuccess: ({ status, data }, s) => {
      if (status === 0) return
      setNotice(status === 200 ? `Recalled '${s.site_id}'.` : `Error: ${data?.error}`)
      qc.invalidateQueries({ queryKey: ['deploy-web', 'sites'] })
    },
  })

  const destroyMut = useMutation({
    // R25 F1: two-call guard on the irreversible path. The preview call
    // resolves the LIVE resources; the dialog names those; the confirmed
    // call binds to them so a site recreated since preview is refused (409).
    mutationFn: async (s: Site) => {
      const prev = await jsend<any>('/destroy', { site_id: s.site_id, profile: s.profile || '' })
      if (prev.status !== 200) throw new Error(prev.data?.error || `Destroy preview failed (${prev.status})`)
      const r = prev.data.resources || {}
      const ok = window.confirm(`DESTROY '${s.site_id}'? Permanently deletes bucket ${r.bucket || '?'} and distribution ${r.distribution_id || '?'}. Cannot be undone.`)
      if (!ok) return { status: 0, data: { cancelled: true } }
      return jsend<any>('/destroy', {
        site_id: s.site_id, confirm: true, profile: s.profile || '',
        expected_bucket: r.bucket || '', expected_distribution_id: r.distribution_id || '',
      })
    },
    onSuccess: ({ status, data }, s) => {
      if (status === 0) return
      setNotice(status === 200 ? `Destroying '${s.site_id}' (CloudFront disable can take 5–15 min).` : `Error: ${data?.error}`)
      qc.invalidateQueries({ queryKey: ['deploy-web', 'sites'] })
    },
  })

  function loadPolicy() { loadPolicyMut.mutate() }
  function recall(s: Site) {
    // Confirmation happens inside the mutation using LIVE previewed resources.
    recallMut.mutate(s)
  }
  function destroy(s: Site) {
    // Confirmation happens inside the mutation using LIVE previewed resources.
    destroyMut.mutate(s)
  }

  const CmdRow = ({ text }: { text: string }) => (
    <div style={cmd}>
      <code style={{ overflow: 'auto', whiteSpace: 'nowrap' }}>{text}</code>
      <Btn onClick={() => navigator.clipboard.writeText(text)}><Copy size={11} /> Copy</Btn>
    </div>
  )

  // Computed stats for the StatCard row
  const totalDeployments = sites.length + deployedWebapps.length
  const estCost = totalWebappUsd

  return (
    <>
      {/* Deploy is a sub-surface of Artifacts (Joe R1): always give the way
          back to the gallery so the console never feels like a dead end. */}
      <div className="px-6 pt-4">
        <button
          type="button"
          onClick={() => navigate('/artifacts')}
          className="inline-flex items-center gap-1.5 text-[13px] text-muted hover:text-text transition-colors cursor-pointer bg-transparent border-none px-0"
          aria-label="Back to Artifacts"
        >
          <ArrowLeft size={14} aria-hidden="true" />
          Back to Artifacts
        </button>
      </div>
      <PageHeader title="Artifact Deploy" subtitle="One console for deploying artifacts to your own AWS — set up access, check health, and manage everything deployed." />
      <div className="px-6 pb-8 overflow-y-auto flex-1 min-h-0" style={{ color: 'var(--text)' }}>

      {/* StatCard row — mirrors AgentsPage/ArtifactsPage pattern */}
      <div className="grid gap-3.5 grid-cols-[repeat(auto-fit,minmax(150px,1fr))] mb-6">
        <StatCard label="Profiles" value={profiles.length} />
        <StatCard label="Active Deployments" value={totalDeployments} accent />
        <StatCard label="Ready to Deploy" value={draftWebapps.length} delay={60} />
        <StatCard label="Est. Cost (not a bill)" value={estCost > 0 ? `≤ $${estCost.toFixed(4)}` : '~$0'} delay={120} />
      </div>

      {notice && (
        <Card style={{ whiteSpace: 'pre-wrap', borderColor: 'var(--accent)', fontSize: 12 }}>{notice}</Card>
      )}

      {/* Getting started guide */}
      <Card>
        <Clickable style={{ display: 'flex', alignItems: 'center', gap: 6, cursor: 'pointer', marginBottom: showGuide ? 12 : 0 }}
             onClick={() => setShowGuide((v) => !v)}>
          {showGuide ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
          <CardTitle className="!mb-0"><Terminal size={15} /> Getting started (one-time AWS setup)</CardTitle>
        </Clickable>
        {showGuide && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 12, fontSize: 13 }}>
            <div>
              <b>1. Authenticate to AWS</b> in your terminal (KiroCrew never sees your keys). Pick one:
              <div style={{ marginTop: 6, display: 'flex', flexDirection: 'column', gap: 6 }}>
                <CmdRow text="aws configure sso        # recommended — short-lived, auto-refreshing" />
                <CmdRow text="aws configure --profile myweb   # or a long-lived named profile" />
              </div>
              <div style={{ marginTop: 8, padding: '8px 10px', borderRadius: 6, border: '1px solid var(--warn-border, #fde68a)', background: 'var(--warn-subtle, #fffbeb)', color: 'var(--warn)', fontSize: 11.5, lineHeight: 1.5 }}>
                <b>Two things that trip people up:</b>
                <ul style={{ margin: '4px 0 0', paddingLeft: 16 }}>
                  <li>Configure the profile on the <b>machine running the gateway</b> (your host), not your laptop — Artifact Deploy shells to <code>aws</code> from the gateway process.</li>
                  <li>SSO needs <b>AWS CLI v2</b>; v1 fails with <code>missing … sso_start_url, sso_region</code>. Make sure the gateway&apos;s <code>PATH</code> resolves v2 before any v1.</li>
                </ul>
              </div>
            </div>
            <div><b>2. Enter the profile name + region below</b> and click <b>Save</b>, then <b>Verify access</b>.</div>
            <div>
              <b>3. Apply the IAM policy</b> — click <b>Get IAM policy</b>, then apply it yourself to a dedicated
              role/identity (console or your own <code>aws iam</code> command). KiroCrew never edits your IAM.
              The first deploy reports the exact missing permission if anything&apos;s off.
            </div>
            <span style={{ color: 'var(--accent)', fontSize: 12, cursor: 'default' }}>
              Full setup guide (profile, AWS CLI v2, troubleshooting) →
            </span>
          </div>
        )}
      </Card>

      {/* Security model (collapsible) */}
      <Card>
        <Clickable style={{ display: 'flex', alignItems: 'center', gap: 6, cursor: 'pointer', marginBottom: showSecurity ? 12 : 0 }}
             onClick={() => setShowSecurity((v) => !v)}>
          {showSecurity ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
          <CardTitle className="!mb-0"><Lock size={15} /> How this is secured</CardTitle>
        </Clickable>
        {showSecurity && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 10, fontSize: 12.5, lineHeight: 1.55 }}>
            <div>
              <b>Your credentials never touch KiroCrew.</b> Only the <b>profile name</b> is stored;
              every AWS call runs through the <code>aws</code> CLI with <code>--profile</code> (never boto3),
              so credential resolution stays in your OS credential store. KiroCrew never writes IAM and
              never manages accounts, users, or roles — you apply the generated least-privilege policy yourself.
            </div>
            <div>
              <b>The origin bucket is private.</b> It is created with Block Public Access on,
              <code>BucketOwnerEnforced</code> ownership, and SSE-AES256 — with <b>no public bucket policy</b>.
              Only CloudFront can read it, via an Origin Access Control (OAC) policy whose
              <code>AWS:SourceArn</code> pins your specific distribution. The bucket name is random/opaque
              and hidden from the public URL.
            </div>
            <div>
              <b>The published URL is public-by-link.</b> Content is served at a random
              <code>*.cloudfront.net</code> domain — <b>anyone with the link can view it</b> (world-readable;
              no auth in v1). Don&apos;t publish anything you wouldn&apos;t put on the open internet.
            </div>
            <div>
              <b>Pre-publish scan + sensitive-path guard.</b> Content is scanned for secrets and internal-data
              signals (internal hostnames, account IDs/ARNs) and publishing is blocked-and-warned
              until you explicitly override. Local directories are checked against sensitive credential paths
              (<code>~/.aws</code>, <code>~/.ssh</code>, …) before any upload.
            </div>
            <div>
              <b>Confirm-gate + audit.</b> Deploy / Recall / Destroy each require explicit confirmation
              (never auto-approved) and emit a SEL audit event. <b>Recall</b> takes a site down fast
              (URL → 404, reversible); <b>Destroy</b> tears down all infra (irreversible).
            </div>
            <span style={{ color: 'var(--accent)', fontSize: 12, cursor: 'default' }}>
              Full setup &amp; security docs →
            </span>
          </div>
        )}
      </Card>

      {/* Profiles section — CardTitle + InfoTip pattern */}
      <Card>
        <div className="flex justify-between items-center">
          <CardTitle>
            AWS Profiles ({profiles.length}) <InfoTip text="Every deploy runs as a registered profile. Register your existing AWS CLI profiles or create new ones — KiroCrew stores only the name, never credentials." />
          </CardTitle>
          <span style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
            <Btn onClick={() => setShowNewProfile((v) => !v)}><Plus size={12} /> New profile</Btn>
            <StyledSelect
              options={['static', 'fullstack']}
              value={policyTier}
              onChange={(v) => setPolicyTier(v as 'static' | 'fullstack')}
              placeholder="Policy tier"
              style={{ minWidth: 120 }}
            />
            <Btn onClick={loadPolicy}>Get IAM policy</Btn>
          </span>
        </div>
        {profiles.length === 0 && (
          <div style={{ fontSize: 13, color: 'var(--muted)', marginBottom: 8 }}>
            No profiles yet — register one below. Every deploy runs as a registered profile.
          </div>
        )}
        {/* Profiles table — table-striped pattern */}
        {profiles.length > 0 && (
          <table className="w-full border-collapse table-striped">
            <thead>
              <tr>
                {['', 'Name', 'Account', 'Region', 'Status', 'Actions'].map(h => (
                  <th key={h} className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {profiles.map((p) => (
                <tr key={p.name} className="hover:bg-bg-hover transition-colors">
                  <td className="px-2.5 py-2 border-b border-border">
                    <Btn
                      title={p.name === defaultProfile ? 'Default profile' : 'Make default'}
                      aria-label={p.name === defaultProfile ? `${p.name} is the default profile` : `Make ${p.name} the default profile`}
                      onClick={() => p.name !== defaultProfile && setDefaultProfile.mutate(p.name)}
                      style={{ background: 'transparent', border: 'none', padding: 0, display: 'inline-flex' }}
                      className="!px-0 !py-0 !border-0">
                      <Star size={14} fill={p.name === defaultProfile ? 'var(--accent)' : 'none'} stroke={p.name === defaultProfile ? 'var(--accent)' : 'var(--muted)'} />
                    </Btn>
                  </td>
                  <td className="px-2.5 py-2 border-b border-border text-sm font-mono font-semibold">{p.name}</td>
                  <td className="px-2.5 py-2 border-b border-border text-sm text-muted">{p.account ? `acct ${p.account}` : '—'}</td>
                  <td className="px-2.5 py-2 border-b border-border text-sm text-muted">{p.region}</td>
                  <td className="px-2.5 py-2 border-b border-border text-sm">
                    {p.verified_at
                      ? <span style={{ color: 'var(--ok)', fontSize: 11, display: 'inline-flex', alignItems: 'center', gap: 3 }}><CheckCircle size={11} /> verified</span>
                      : <span style={{ color: 'var(--muted)', fontSize: 11 }}>unverified</span>}
                  </td>
                  <td className="px-2.5 py-2 border-b border-border text-sm">
                    <span style={{ display: 'flex', gap: 6 }}>
                      <Btn onClick={() => verify.mutate(p.name)}><ShieldCheck size={11} /> Verify</Btn>
                      <Btn aria-label={`Remove ${p.name} from registry`}
                        onClick={() => window.confirm(`Remove '${p.name}' from the registry? Your ~/.aws/config is NOT touched.`) && removeProfile.mutate(p.name)}>
                        <Trash2 size={11} /> Remove
                      </Btn>
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {availableProfiles.length > 0 && (
          <div style={{ marginTop: 10, fontSize: 12, color: 'var(--muted)', display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
            <span>Found in your AWS config:</span>
            {availableProfiles.slice(0, 8).map((n) => (
              <Btn key={n} onClick={() => addProfile.mutate({ name: n, region: 'us-west-2' })}>
                <Plus size={10} /> {n}
              </Btn>
            ))}
            {availableProfiles.length > 8 && <span>+{availableProfiles.length - 8} more</span>}
          </div>
        )}
        {showNewProfile && (
          <div style={{ marginTop: 12, padding: 12, background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 6 }}>
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 8 }}>
              <span style={{ flex: 1, minWidth: 160 }}>
                <label style={label} htmlFor="np-name">Profile name</label>
                <Input id="np-name" style={{width: '100%' }} placeholder="e.g. my-sandbox" value={npName} onChange={(e) => setNpName(e.target.value)} />
              </span>
              <span style={{ minWidth: 140 }}>
                <label style={label} htmlFor="np-region">Region</label>
                <Input id="np-region" style={{width: '100%' }} placeholder="us-west-2" value={npRegion} onChange={(e) => setNpRegion(e.target.value)} />
              </span>
            </div>
            <div style={{ fontSize: 12, display: 'inline-flex', alignItems: 'center', gap: 6, marginBottom: 8 }}>
              <Toggle checked={npCreate} onChange={setNpCreate} label="Also create in AWS config" />
              <span style={{ color: 'var(--muted)', fontSize: 11 }}>(writes only region / credential_process via <code>aws configure set</code> — never credentials)</span>
            </div>
            {npCreate && (
              <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 8 }}>
                <span style={{ minWidth: 160 }}>
                  <label style={label} htmlFor="np-account">Account (12 digits, optional — iam-identity-style)</label>
                  <Input id="np-account" style={{width: '100%' }} placeholder="123456789012" value={npAccount} onChange={(e) => setNpAccount(e.target.value)} />
                </span>
                <span style={{ minWidth: 140 }}>
                  <label style={label} htmlFor="np-role">Role (optional)</label>
                  <Input id="np-role" style={{width: '100%' }} placeholder="Admin" value={npRole} onChange={(e) => setNpRole(e.target.value)} />
                </span>
              </div>
            )}
            <Btn primary disabled={!npName.trim()}
              onClick={() => addProfile.mutate({ name: npName.trim(), region: npRegion.trim() || 'us-west-2', create: npCreate, account: npAccount.trim(), role: npRole.trim() })}>
              {npCreate ? 'Create + register' : 'Register'}
            </Btn>
          </div>
        )}
        {reach && (
          <div style={{ marginTop: 10, fontSize: 12, color: reach.reachable ? 'var(--ok)' : 'var(--danger)' }}>
            <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
              {reach.reachable
                ? <><CheckCircle size={12} /> {reach.profile}: access reachable{reach.account ? ` (account ${reach.account})` : ''}</>
                : <><XCircle size={12} /> {reach.detail || reach.error || 'not reachable'}</>}
            </span>
            <div style={{ color: 'var(--muted)', fontSize: 11 }}>{reach.note}</div>
          </div>
        )}
        {policy && (
          <div style={{ marginTop: 10 }}>
            <div style={{ fontSize: 11, color: 'var(--muted)', marginBottom: 4 }}>
              Apply this policy yourself (KiroCrew never edits your IAM).
              {policyTier === 'fullstack' && <span style={{ color: 'var(--accent)' }}> Fullstack tier: includes Lambda, API Gateway, DynamoDB, IAM PassRole — scoped to kirocrew-deploy-app-* resources.</span>}
            </div>
            <pre style={{ background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 6, padding: 10, fontSize: 11, maxHeight: 240, overflow: 'auto' }}>{policy}</pre>
            <Btn onClick={() => navigator.clipboard.writeText(policy)}><Copy size={12} /> Copy policy</Btn>
            {boundaryPolicy && (
              <div style={{ marginTop: 10 }}>
                <div style={{ fontSize: 11, color: 'var(--warn)', marginBottom: 4 }}>
                  {boundaryNote || 'Fullstack also requires the permissions-boundary policy below — create it BEFORE the first deploy (role creation is conditioned on it).'}
                </div>
                <pre style={{ background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 6, padding: 10, fontSize: 11, maxHeight: 200, overflow: 'auto' }}>{boundaryPolicy}</pre>
                <Btn onClick={() => navigator.clipboard.writeText(boundaryPolicy)}><Copy size={12} /> Copy boundary policy</Btn>
              </div>
            )}
          </div>
        )}
        <div style={{ marginTop: 12, fontSize: 12, color: 'var(--muted)', display: 'flex', alignItems: 'center', gap: 6 }}>
          <Globe size={13} stroke={'var(--accent)'} />
          To publish, open an artifact and choose <b style={{ color: 'var(--text)' }}>&nbsp;Publish&nbsp;→&nbsp;Publish to public web (your AWS)</b>.
        </div>
      </Card>

      {/* Pending confirmations (F6) — deploy previews awaiting human confirm */}
      <PendingConfirmations qc={qc} />

      {/* Ready to deploy — CardTitle + InfoTip */}
      {draftWebapps.length > 0 && (
        <Card>
          <CardTitle>
            Ready to deploy ({draftWebapps.length}) <InfoTip text="Webapp artifacts that haven't been deployed yet. Deploy opens a skill-loaded chat session that adapts the app and ships it." />
          </CardTitle>
          <table className="w-full border-collapse table-striped">
            <thead>
              <tr>
                {['Name', 'Status', 'Est. Cost', 'Profile', ''].map(h => (
                  <th key={h} className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {draftWebapps.map((a) => {
                const cost = webappCost(a)
                return (
                  <tr key={a.slug} className="hover:bg-bg-hover transition-colors">
                    <td className="px-2.5 py-2 border-b border-border text-sm font-semibold">
                      <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
                        <Rocket size={13} stroke={'var(--accent)'} /> {a.slug}
                      </span>
                    </td>
                    <td className="px-2.5 py-2 border-b border-border text-sm"><Badge variant="warn">not deployed</Badge></td>
                    <td className="px-2.5 py-2 border-b border-border text-sm text-muted">{cost > 0 ? `≤ $${cost.toFixed(4)}` : '~$0.00'}</td>
                    <td className="px-2.5 py-2 border-b border-border text-sm">
                      {profiles.length > 0 && (
                        <StyledSelect
                          options={profiles.map((p) => p.name)}
                          value={draftProfiles[a.slug] || defaultProfile || ''}
                          onChange={(v) => setDraftProfiles((m) => ({ ...m, [a.slug]: v }))}
                          placeholder={defaultProfile ? `${defaultProfile} (default)` : 'default'}
                          style={{ minWidth: 100 }}
                        />
                      )}
                    </td>
                    <td className="px-2.5 py-2 border-b border-border text-sm text-right">
                      <span style={{ display: 'flex', gap: 6, justifyContent: 'flex-end' }}>
                        <Btn primary onClick={() => deployDraft(a.slug)} aria-label={`Deploy ${a.slug}`}>
                          <Rocket size={11} /> Deploy
                        </Btn>
                        <Link to={`/artifacts/${encodeURIComponent(a.slug)}`} style={linkBtn}>
                          <ExternalLink size={11} /> Details
                        </Link>
                      </span>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
          <div style={{ paddingTop: 10, fontSize: 11, color: 'var(--muted)' }}>
            Deploy opens a new chat session that runs the artifact-deploy skill with the chosen profile.
          </div>
        </Card>
      )}

      {/* Deployments — CardTitle + InfoTip + table-striped */}
      <Card>
        <div className="flex justify-between items-center">
          <CardTitle>
            Deployments ({sites.length + deployedWebapps.length}) <InfoTip text="All deployed assets — static sites published from artifacts and full-stack webapp deployments. Recall = reversible takedown; Destroy = permanent infra removal." />
          </CardTitle>
          <Btn onClick={() => { qc.invalidateQueries({ queryKey: ['deploy-web', 'sites'] }); qc.invalidateQueries({ queryKey: ['deploy-web', 'webapps'] }) }}><RefreshCw size={12} /> Refresh</Btn>
        </div>
        {sites.length + deployedWebapps.length === 0 && (
          <div style={{ fontSize: 13, color: 'var(--muted)' }}>
            No deployments yet — publish an artifact from its page to create one, or open a webapp artifact and click <b style={{ color: 'var(--text)' }}>Deploy</b>.
          </div>
        )}
        {(sites.length + deployedWebapps.length > 0) && (
          <table className="w-full border-collapse table-striped">
            <thead>
              <tr>
                {['Name', 'Type', 'Status', 'Profile', 'URL', 'Cost', 'Actions'].map(h => (
                  <th key={h} className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {/* Static site rows */}
              {sites.map((s) => (
                <tr key={`static-${s.site_id}`} className="hover:bg-bg-hover transition-colors">
                  <td className="px-2.5 py-2 border-b border-border text-sm font-semibold">{s.site_id}</td>
                  <td className="px-2.5 py-2 border-b border-border text-sm"><span style={{ border: '1px solid var(--border)', color: 'var(--muted)', padding: '1px 6px', borderRadius: 9999, fontSize: 10, fontWeight: 500 }}>static</span></td>
                  <td className="px-2.5 py-2 border-b border-border text-sm"><Badge variant={s.status === 'deployed' || s.status === 'live' ? 'ok' : s.status === 'error' ? 'err' : 'warn'}>{s.status || 'unknown'}</Badge></td>
                  <td className="px-2.5 py-2 border-b border-border text-sm">{s.profile ? <span style={chip}>{s.profile}</span> : <span style={{ color: 'var(--muted)' }}>—</span>}</td>
                  <td className="px-2.5 py-2 border-b border-border text-sm" style={{ maxWidth: 200, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{s.url ? (safeHttpUrl(s.url) ? <a href={safeHttpUrl(s.url)!} target="_blank" rel="noreferrer" style={{ color: 'var(--accent)' }}>{s.url}</a> : <span style={{ color: 'var(--muted)' }}>{s.url}</span>) : '—'}</td>
                  <td className="px-2.5 py-2 border-b border-border text-sm text-muted">~$0.00/mo</td>
                  <td className="px-2.5 py-2 border-b border-border text-sm">
                    <span style={{ display: 'flex', gap: 5 }}>
                      <Btn onClick={() => recall(s)}><Undo2 size={11} /> Recall</Btn>
                      <Btn danger onClick={() => destroy(s)}><Trash2 size={11} /> Destroy</Btn>
                    </span>
                  </td>
                </tr>
              ))}
              {/* Webapp rows */}
              {deployedWebapps.map((a) => {
                const m = a.webapp_metadata!
                const url = m.deploy_target?.public_url || ''
                const cost = webappCost(a)
                return (
                  <tr key={`webapp-${a.slug}`} className="hover:bg-bg-hover transition-colors">
                    <td className="px-2.5 py-2 border-b border-border text-sm font-semibold">{a.slug}</td>
                    <td className="px-2.5 py-2 border-b border-border text-sm"><span style={{ background: 'var(--accent-subtle)', color: 'var(--accent)', padding: '1px 6px', borderRadius: 9999, fontSize: 10, fontWeight: 500 }}>webapp</span></td>
                    <td className="px-2.5 py-2 border-b border-border text-sm"><Badge variant={m.lifecycle?.status === 'deployed' || m.lifecycle?.status === 'live' ? 'ok' : m.lifecycle?.status === 'error' ? 'err' : 'warn'}>{m.lifecycle?.status || 'unknown'}</Badge></td>
                    <td className="px-2.5 py-2 border-b border-border text-sm">{m.deploy_target?.profile ? <span style={chip}>{m.deploy_target.profile}</span> : <span style={{ color: 'var(--muted)' }}>—</span>}</td>
                    <td className="px-2.5 py-2 border-b border-border text-sm" style={{ maxWidth: 200, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{url ? (safeHttpUrl(url) ? <a href={safeHttpUrl(url)!} target="_blank" rel="noreferrer" style={{ color: 'var(--accent)' }}>{url}</a> : <span style={{ color: 'var(--muted)' }}>{url}</span>) : '—'}</td>
                    <td className="px-2.5 py-2 border-b border-border text-sm text-muted">{cost > 0 ? `≤$${cost.toFixed(4)}` : '~$0.00'}</td>
                    <td className="px-2.5 py-2 border-b border-border text-sm">
                      <Link to={`/artifacts/${encodeURIComponent(a.slug)}`} style={linkBtn}>
                        <ExternalLink size={11} /> Details
                      </Link>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
        {/* Account-level total */}
        {(sites.length + deployedWebapps.length > 0) && (
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, paddingTop: 12, fontSize: 12.5, fontWeight: 600 }}>
            <span>Estimated total — {sites.length} static site{sites.length === 1 ? '' : 's'} + {deployedWebapps.length} webapp{deployedWebapps.length === 1 ? '' : 's'}:</span>
            <span style={{ color: 'var(--accent)' }}>~${totalWebappUsd.toFixed(4)}</span>
            <span style={{ color: 'var(--muted)', fontWeight: 400, fontSize: 11 }}>
              (worst-case tiers, over each TTL window; estimate — not the AWS bill)
            </span>
          </div>
        )}
      </Card>
      </div>
    </>
  )
}

// ── F6: Pending confirmations component ─────────────────────────────────────

interface PendingEntry {
  id: string
  site_id: string
  artifact_slug: string
  local_dir: string
  profile: string
  region: string
  ttl_hours: number
  scan_summary: string
  override_scan_required?: boolean
  created_at_epoch: number
}

function PendingConfirmations({ qc }: { qc: ReturnType<typeof useQueryClient> }) {
  const { data } = useQuery<{ pending: PendingEntry[] }>({
    queryKey: ['deploy-web', 'pending'],
    queryFn: async () => {
      const r = await fetch(BASE + '/pending', { headers: { 'X-Session-Key': 'dashboard:ui' } })
      return (await r.json()) as { pending: PendingEntry[] }
    },
    refetchInterval: 10000,
  })
  const pending = data?.pending || []

  const confirmMut = useMutation({
    mutationFn: async ({ id, overrideScan }: { id: string; overrideScan?: boolean }) => {
      // R24: entries flagged override_scan_required were blocked by
      // overridable (non-credential) findings — the human's explicit
      // "Deploy anyway" sends override_scan so the backend clears them.
      const res = await fetch(BASE + `/pending/${id}/confirm`, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Session-Key': 'dashboard:ui' }, body: JSON.stringify(overrideScan ? { override_scan: true } : {}) })
      const data = await res.json()
      if (!res.ok) throw new Error(data.error || `Confirm failed (${res.status})`)
      return data
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['deploy-web', 'pending'] })
      qc.invalidateQueries({ queryKey: ['deploy-web', 'sites'] })
    },
  })

  const dismissMut = useMutation({
    mutationFn: async (id: string) => {
      const res = await fetch(BASE + `/pending/${id}/dismiss`, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Session-Key': 'dashboard:ui' } })
      const data = await res.json()
      if (!res.ok) throw new Error(data.error || `Dismiss failed (${res.status})`)
      return data
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['deploy-web', 'pending'] })
    },
  })

  if (!pending.length) return null

  return (
    <Card>
      <CardTitle>
        <Rocket size={15} /> Pending confirmations ({pending.length})
      </CardTitle>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 10, fontSize: 13 }}>
        {pending.map((e) => {
          const age = Math.round((Date.now() / 1000 - e.created_at_epoch) / 60)
          const source = e.artifact_slug || e.local_dir || '(unknown)'
          return (
            <div key={e.id} style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '8px 10px', borderRadius: 6, border: '1px solid var(--border)', background: 'var(--bg)' }}>
                <div style={{ flex: 1 }}>
                  <div style={{ fontWeight: 500 }}>{e.site_id}</div>
                  <div style={{ color: 'var(--muted)', fontSize: 11 }}>
                    Source: {source} &middot; Profile: {e.profile || 'default'} &middot; TTL: {e.ttl_hours}h &middot; Scan: {e.scan_summary} &middot; {age}m ago
                  </div>
                  {e.override_scan_required && (
                    <div style={{ color: 'var(--warn)', fontSize: 11, marginTop: 2 }}>
                      Blocked by non-credential scan findings — review above, then &ldquo;Deploy anyway&rdquo; to override.
                    </div>
                  )}
                </div>
                <Btn danger onClick={() => confirmMut.mutate({ id: e.id, overrideScan: !!e.override_scan_required })} disabled={confirmMut.isPending}>
                  {e.override_scan_required ? 'Deploy anyway' : 'Confirm Deploy'}
                </Btn>
                <Btn onClick={() => dismissMut.mutate(e.id)} disabled={dismissMut.isPending}>
                  Dismiss
                </Btn>
              </div>
              {(confirmMut.isError || dismissMut.isError) && (
                <div style={{ color: 'var(--error, #dc2626)', fontSize: 11, padding: '2px 10px' }}>
                  {(confirmMut.error as Error)?.message || (dismissMut.error as Error)?.message}
                </div>
              )}
            </div>
          )
        })}
      </div>
    </Card>
  )
}
